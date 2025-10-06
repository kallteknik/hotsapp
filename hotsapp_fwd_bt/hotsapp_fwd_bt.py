#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hotsapp_fwd_bt.py — HA WebSocket -> Bluetooth advertisements -> HTTP forward

- Kopplar upp mot HA WebSocket via supervisor-proxy (ws://supervisor/core/websocket)
- Autentiserar med SUPERVISOR_TOKEN
- Abonnerar på bluetooth/subscribe_advertisements
- Filtrerar/av-dedupar enligt options och forwardar utvalda datapunkter med HTTP POST

Körs av run.sh
"""

import os
import json
import time
import ssl as _ssl
import traceback
from datetime import datetime, timezone
from typing import Any, Dict
import uuid
import pathlib

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from websocket import create_connection, WebSocketConnectionClosedException, WebSocketTimeoutException





# ---------- Konfiguration från miljö och options.json ----------
ADDON_OPTIONS_PATH = os.getenv("ADDON_OPTIONS_PATH", "/data/options.json")
HA_WS_URL = os.getenv("HA_WS_URL", "ws://supervisor/core/websocket")
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")

def _read_options(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

_options = _read_options(ADDON_OPTIONS_PATH)

def opt(name: str, default: Any = None) -> Any:
    # options-nycklar kommer ofta i lowercase
    return _options.get(name, default)

def as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"1","true","yes","y","on"}

# ---- HTTP-forward inställningar ----
API_URL         = opt("api_url",  "https://api.exempel.se/measurements")
API_TOKEN       = opt("api_token")
DRY_RUN         = as_bool(opt("dry_run", "0"))
RETRY_TOTAL     = int(opt("retry_total", 5))
RETRY_BACKOFF   = float(opt("retry_backoff", 1.0))

# ---- BT-filter/inställningar från options ----
# Du kan ange en eller flera av dessa för att begränsa trafiken
FILTER_ADDRESS          = opt("bt_address")                  # ex: "AA:BB:CC:DD:EE:FF"
FILTER_SERVICE_UUIDS    = set(opt("bt_service_uuids", []) or [])   # lista av UUID-strängar
FILTER_MANUFACTURER_IDS = set(opt("bt_manufacturer_ids", []) or [])# lista av int
RSSI_MIN                = int(opt("rssi_min", -999))         # t.ex. -85
INCLUDE_CONNECTABLE_ONLY= as_bool(opt("include_connectable_only", "0"))
DEDUP_WINDOW_SEC        = float(opt("dedup_window_sec", 0.5)) # tidsfönster för av-dedup (per address)

# ---- Övrigt ----
CLIENT_NAME     = opt("client_name", "hotsapp_fwd_bt")
VERBOSE         = as_bool(opt("verbose", "1"))

# ------ CLIENT ID -------
HA_CORE_UUID_PATH = "/config/.storage/core.uuid"   # needs map: config:ro
ADDON_CLIENT_ID_PATH = "/data/client_id"           # persistent fallback


def _log(msg: str) -> None:
    print(msg, flush=True)


# Optional override via options.json
CLIENT_ID_OVERRIDE = (opt("client_id_override", "") or "").strip()

def get_client_id() -> str:
    # 0) explicit override
    if CLIENT_ID_OVERRIDE:
        _log(f"[CLIENT_ID] using override from options")
        return CLIENT_ID_OVERRIDE

    # 1) try HA core uuid
    try:
        if os.path.exists(HA_CORE_UUID_PATH):
            with open(HA_CORE_UUID_PATH, "r", encoding="utf-8") as f:
                j = json.load(f)
            ha_uuid = j.get("data", {}).get("uuid")
            if ha_uuid:
                _log(f"[CLIENT_ID] using HA core UUID from {HA_CORE_UUID_PATH}")
                return ha_uuid
            else:
                _log(f"[CLIENT_ID] core.uuid found but no 'data.uuid' field")
    except Exception as e:
        _log(f"[CLIENT_ID] failed reading HA core UUID: {e}")

    # 2) persistent fallback under /data
    try:
        p = pathlib.Path(ADDON_CLIENT_ID_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            cid = p.read_text(encoding="utf-8").strip()
            if cid:
                _log(f"[CLIENT_ID] using persistent fallback from {ADDON_CLIENT_ID_PATH}")
                return cid
        new_id = str(uuid.uuid4())
        p.write_text(new_id + "\n", encoding="utf-8")
        _log(f"[CLIENT_ID] created persistent fallback at {ADDON_CLIENT_ID_PATH}")
        return new_id
    except Exception as e:
        _log(f"[CLIENT_ID] fallback write failed: {e}")

    # 3) last resort (non-persistent for this process)
    cid = str(uuid.uuid4())
    _log("[CLIENT_ID] WARNING: using ephemeral UUID (no core.uuid and /data not writable)")
    return cid

CLIENT_ID = get_client_id()



# ---------- HTTP session med retries ----------
session = requests.Session()
adapter = HTTPAdapter(
    max_retries=Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=None,   # retry även på POST (alla metoder)
    )
)
session.mount("http://", adapter)
session.mount("https://", adapter)

# ---------- Hjälpare ----------
_last_sent_ts: Dict[str, float] = {}  # per address för dedup-fönster



def _should_forward(evt: Dict[str, Any]) -> bool:
    """
    Event-struktur (enligt HA WebSocket BT-annonser):
    {
      "id": <int>, "type": "event", "event": {
        "address": "AA:BB:..", "rssi": -60, "connectable": true/false,
        "manufacturer_data": {"76": "base64..." }  # eller dict<int, bytes> beroende på serialisering
        "service_data": { "uuid": "base64..." },
        "service_uuids": ["uuid1", "uuid2"],
        "tx_power": -4, "time": "ISO8601" ...
      }
    }
    """
    e = evt.get("event") or {}
    addr = (e.get("address") or "").upper()
    if not addr:
        return False
    rssi = int(e.get("rssi") or -999)
    connectable = bool(e.get("connectable"))

    if FILTER_ADDRESS and addr != FILTER_ADDRESS.upper():
        return False
    if INCLUDE_CONNECTABLE_ONLY and not connectable:
        return False
    if rssi < RSSI_MIN:
        return False

    # service_uuids
    if FILTER_SERVICE_UUIDS:
        su = set((e.get("service_uuids") or []))
        if not (su & FILTER_SERVICE_UUIDS):
            return False

    # manufacturer ids
    if FILTER_MANUFACTURER_IDS:
        md = e.get("manufacturer_data") or {}
        # nyare HA serialiserar nycklar som str, äldre som int — hantera båda
        m_ids = {int(k) for k in md.keys()} if md else set()
        if not (m_ids & FILTER_MANUFACTURER_IDS):
            return False

    # dedup per address
    now = time.monotonic()
    last = _last_sent_ts.get(addr, 0)
    if now - last < DEDUP_WINDOW_SEC:
        return False
    _last_sent_ts[addr] = now
    return True

def _post_measurement(event_payload: Dict[str, Any]) -> None:
    # Basheaders inkl. klient-id
    headers = {
        "Content-Type": "application/json",
        "x-client-id": CLIENT_ID,
    }
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    body = {
        "source": "ha_bt",
        "client": CLIENT_NAME,
        "received_utc": datetime.now(timezone.utc).isoformat(),
        "data": event_payload,
    }

    if DRY_RUN:
        _log(f"[DRY_RUN] Would POST to {API_URL}: {json.dumps(body)[:600]}")
        return

    try:
        _log(f"[HTTP] POST {API_URL} with x-client-id={CLIENT_ID}")
        r = session.post(API_URL, headers=headers, json=body, timeout=15)
    except Exception as ex:
        _log(f"[HTTP] request error before response: {ex}")
        raise

    # Logga status + (truncerad) svarskropp
    resp_text = (r.text or "")
    snippet = resp_text[:800]
    ct = r.headers.get("Content-Type", "")
    if r.ok:
        _log(f"[HTTP] OK {r.status_code} ({ct})")
        if snippet.strip():
            _log(f"[HTTP] resp: {snippet}")
    else:
        _log(f"[HTTP] ERR {r.status_code} {r.reason} ({ct}); resp: {snippet}")
        r.raise_for_status()



# --- ThermoBeacon decoder (manufacturer_data id 16/17) ---
def _le_u16(buf: bytes, off: int) -> int | None:
    return int.from_bytes(buf[off:off+2], "little") if off + 2 <= len(buf) else None

def _le_s16(buf: bytes, off: int) -> int | None:
    if off + 2 > len(buf):
        return None
    v = int.from_bytes(buf[off:off+2], "little", signed=False)
    if v >= 0x8000:
        v -= 0x10000
    return v

def _md_payload(md: Any, company_id: int) -> bytes | None:
    """HA ger manufacturer_data som {company_id(int/str): bytes/hexstr}."""
    if not md:
        return None
    for k, v in md.items():
        try:
            if int(k) != company_id:
                continue
        except Exception:
            continue
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        if isinstance(v, str):
            try:
                return bytes.fromhex(v)
            except Exception:
                return None
    return None



# Decode temperatur
def _decode_thermobeacon(manufacturer_data: Any, address: str) -> dict[str, Any] | None:
    # Prova company id 16 (0x0010) och 17 (0x0011)
    payload = _md_payload(manufacturer_data, 16) or _md_payload(manufacturer_data, 17)
    if not payload or len(payload) < 12:
        return None

    mac_rev = b""
    if address:
        try:
            mac_rev = bytes.fromhex(address.replace(":", ""))[::-1]
        except Exception:
            pass

    # Ankare: positionen precis efter omvänd MAC om den finns, annars 0/2 som fallback
    candidates = []
    if mac_rev and mac_rev in payload:
        base = payload.index(mac_rev) + len(mac_rev)
        candidates += [base, base-2, base+2]
    candidates += [0, 2]
    tried = set()

    for off in candidates:
        if off in tried or off < 0 or off+6 > len(payload):
            continue
        tried.add(off)
        batt_raw = _le_u16(payload, off)
        t_raw    = _le_s16(payload, off+2)
        h_raw    = _le_u16(payload, off+4)
        if batt_raw is None or t_raw is None or h_raw is None:
            continue

        temp_c = t_raw / 16.0
        hum    = h_raw / 16.0
        # Batteri: vissa varianter kodar i mV (t.ex. 2856), andra i 0.01V (t.ex. 373 => 3.73V)
        batt_mv = batt_raw if batt_raw > 1000 else batt_raw * 10
        batt_v  = batt_mv / 1000.0

        # Rimlighetskontroll
        if -40.0 <= temp_c <= 85.0 and 0.0 <= hum <= 100.0 and 2.0 <= batt_v <= 4.5:
            return {
                "temperature_c": round(temp_c, 2),
                "humidity_percent": round(hum, 2),
                "battery_v": round(batt_v, 3),
                "battery_mv": int(batt_mv),
            }

    return None




def _normalize_adv(e: Dict[str, Any]) -> Dict[str, Any]:
    """Normalisera en annons-post från HA (stöd för olika fältvarianter)."""
    device = e.get("device") or {}
    address = (e.get("address") or device.get("address") or device.get("id") or "").upper()
    rssi = e.get("rssi")
    tx_power = e.get("tx_power")
    connectable = e.get("connectable")
    name = e.get("name")

    service_uuids = e.get("service_uuids") or e.get("uuids") or []

    manufacturer_data = e.get("manufacturer_data")
    if isinstance(manufacturer_data, list):
        # Om formatet är [[id, val], ...] -> gör om till {id: val}
        try:
            manufacturer_data = {int(k): v for k, v in manufacturer_data}
        except Exception:
            pass

    service_data = e.get("service_data") or e.get("serviceData")
    tstamp = e.get("time") or e.get("timestamp")

    out = {
        "name": name,
        "address": address,
        "rssi": rssi,
        "tx_power": tx_power,
        "connectable": connectable,
        "service_uuids": service_uuids,
        "manufacturer_data": manufacturer_data,
        "service_data": service_data,
        "time": tstamp,
    }

    # Försök tolka ThermoBeacon och lägg till nycklar om vi hittar något
    extra = _decode_thermobeacon(manufacturer_data, address)
    if extra:
        out.update(extra)
    return out

# ---------- WebSocket loop ----------
def _auth_and_subscribe(ws):
    # 1) server skickar auth_required
    msg = json.loads(ws.recv())
    if VERBOSE:
        _log(f"[WS] <- {msg.get('type')}")

    if msg.get("type") != "auth_required":
        raise RuntimeError("Expected auth_required from HA WebSocket")

    # 2) skicka auth
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN saknas (krävs i add-ons).")

    ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
    msg = json.loads(ws.recv())
    if msg.get("type") != "auth_ok":
        raise RuntimeError(f"Auth failed: {msg}")

    if VERBOSE:
        _log("[WS] Auth OK")

    # 3) subscribe till Bluetooth-annonser
    #    (WebSocket-kommando 'bluetooth/subscribe_advertisements')
    #    Filtren skickas inte här — vi filtrerar lokalt för maximal flexibilitet.
    sub = {"id": 1, "type": "bluetooth/subscribe_advertisements"}
    ws.send(json.dumps(sub))
    if VERBOSE:
        _log("[WS] -> bluetooth/subscribe_advertisements skickad")


# --- Hårdkodat sändintervall & buffer ---
SEND_INTERVAL_SEC = 5.0  # skicka var 5:e sekund, hårdkodat

_latest_by_addr: Dict[str, Dict[str, Any]] = {}  # senaste e_norm per MAC

def _flush_pending() -> None:
    """Skicka senaste observation per MAC och töm bufferten."""
    if not _latest_by_addr:
        return
    items = list(_latest_by_addr.items())
    for addr, e_norm in items:
        try:
            _post_measurement(e_norm)
        except Exception as ex:
            _log(f"[HTTP] POST failed: {ex}")
    _latest_by_addr.clear()
    _log(f"[AGG] Flushed {len(items)} events")


    
def _event_loop(ws):
    # Gör recv icke-blockerande länge nog så vi kan flusha periodiskt
    try:
        ws.settimeout(1.0)  # 1 s polling för att kunna flusha var 5 s
    except Exception:
        pass

    last_flush = time.monotonic()

    while True:
        try:
            raw = ws.recv()
            msg = json.loads(raw)

            if msg.get("type") == "event" and "event" in msg:
                ev = msg["event"] or {}

                # Ny form: listor under add/update/remove
                if any(isinstance(ev.get(k), list) for k in ("add", "update", "remove")):
                    for kind in ("add", "update"):
                        recs = ev.get(kind) or []
                        for rec in recs:
                            e_norm = _normalize_adv(rec)
                            addr = (e_norm.get("address") or "").upper()
                            if not addr:
                                continue
                            _latest_by_addr[addr] = e_norm
                else:
                    # Fallback: äldre/singel event-format
                    e = ev or {}
                    if "address" not in e and isinstance(e.get("data"), dict):
                        e = e["data"]
                    e_norm = _normalize_adv(e)
                    addr = (e_norm.get("address") or "").upper()
                    if addr:
                        _latest_by_addr[addr] = e_norm

        except WebSocketTimeoutException:
            # ingen data just nu → gå vidare till flush-kontroll
            pass

        # Flush var 5:e sekund (hårdkodat)
        now = time.monotonic()
        if now - last_flush >= SEND_INTERVAL_SEC:
            _flush_pending()
            last_flush = now






def main():
    # Reconnect-loop
    backoff = 1.0
    while True:
        try:
            # OBS: i add-ons fungerar ws://supervisor/core/websocket utan extra cert-hantering
            ws = create_connection(
                HA_WS_URL,
                header=[f"Authorization: Bearer {SUPERVISOR_TOKEN}"],
                sslopt={"cert_reqs": _ssl.CERT_NONE},  # proxy är lokalt; undvik cert-stök
                timeout=30,
            )
            _log(f"[WS] Connected to {HA_WS_URL}")
            _auth_and_subscribe(ws)
            backoff = 1.0  # reset backoff efter lyckad auth
            _event_loop(ws)
        except (WebSocketConnectionClosedException, ConnectionError) as e:
            _log(f"[WS] connection dropped: {e}")
        except Exception as e:
            _log(f"[WS] error: {e}\n{traceback.format_exc()}")
        finally:
            try:
                ws.close()
            except Exception:
                pass
            # Exponentiell backoff vid reconnect
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

if __name__ == "__main__":
    main()
