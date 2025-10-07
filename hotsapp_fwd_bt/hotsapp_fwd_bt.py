#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hotsapp_fwd_bt.py — HA WebSocket -> Bluetooth advertisements -> HTTP forward (temperaturer var 5 s)

- Kopplar upp mot HA WebSocket via supervisor-proxy (ws://supervisor/core/websocket)
- Autentiserar med SUPERVISOR_TOKEN
- Abonnerar på bluetooth/subscribe_advertisements
- Dekodar ThermoBeacon temperatur
- Samlar senaste temperatur per MAC och skickar i EN batch var 5:e sekund

Körs av run.sh
"""

import os
import json
import time
import ssl as _ssl
import traceback
from datetime import datetime, timezone
from typing import Any, Dict

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from websocket import create_connection, WebSocketConnectionClosedException, WebSocketTimeoutException

from sensors.thermobeacon import decode as decode_thermobeacon


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

def _log(msg: str) -> None:
    print(msg, flush=True)

# ---- HTTP-forward inställningar ----
API_URL         = opt("api_url",  "https://api.exempel.se/measurements")
API_TOKEN       = opt("api_token")
DRY_RUN         = as_bool(opt("dry_run", "0"))
RETRY_TOTAL     = int(opt("retry_total", 5))
RETRY_BACKOFF   = float(opt("retry_backoff", 1.0))

# ---- Övrigt ----
CLIENT_NAME     = opt("client_name", "hotsapp_fwd_bt")
VERBOSE         = as_bool(opt("verbose", "1"))

# ------ CLIENT ID -------
import uuid, pathlib
HA_CORE_UUID_PATH = "/config/.storage/core.uuid"   # needs map: config:ro
ADDON_CLIENT_ID_PATH = "/data/client_id"           # persistent fallback
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
        status_forcelist=(500, 502, 503, 504, 429),
        allowed_methods=None,   # retry även på POST (alla metoder)
    )
)
session.mount("http://", adapter)
session.mount("https://", adapter)

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




def _decode_thermobeacon(manufacturer_data: Any, address: str) -> dict[str, Any] | None:
    """
    Tolerant dekoder för ThermoBeacon (company id 16/17) som:
    - testar flera offsets (ankare: omvänd MAC; ±2; samt 0 och 2),
    - accepterar temperatur ensam om den är rimlig,
    - väger in kontinuitet mot senast känd temperatur för samma MAC.
    """
    payload = _md_payload(manufacturer_data, 16) or _md_payload(manufacturer_data, 17)
    if not payload or len(payload) < 6:
        return None

    # Föregående temp (om vi har skickat något för denna MAC tidigare)
    prev_t = None
    try:
        prev_t = (_last_temp_by_addr.get(address, {}) or {}).get("temperature_c")
        if prev_t is not None:
            prev_t = float(prev_t)
    except Exception:
        prev_t = None

    # Hitta ankare: omvänd MAC i payload, annars fallback
    mac_rev = b""
    if address:
        try:
            mac_rev = bytes.fromhex(address.replace(":", ""))[::-1]
        except Exception:
            pass

    candidates: list[int] = []
    if mac_rev and mac_rev in payload:
        base = payload.index(mac_rev) + len(mac_rev)
        candidates += [base, base - 2, base + 2]
    candidates += [0, 2]

    best: dict[str, Any] | None = None
    best_score = -1

    def parse_at(off: int) -> tuple[int, dict[str, Any] | None]:
        if off < 0 or off + 6 > len(payload):
            return -1, None
        batt_raw = _le_u16(payload, off)
        t_raw    = _le_s16(payload, off + 2)
        h_raw    = _le_u16(payload, off + 4)

        cand: dict[str, Any] = {}
        score = 0

        # Temperatur
        temp_ok = False
        if t_raw is not None:
            temp_c = t_raw / 16.0
            if -40.0 <= temp_c <= 85.0:
                temp_ok = True
                temp_c = round(temp_c, 2)
                cand["temperature_c"] = temp_c
                score += 10  # temperatur väger tyngst

                # Kontinuitet mot föregående temp (om finns)
                if prev_t is not None:
                    delta = abs(temp_c - prev_t)
                    if   delta <= 0.5:  score += 5
                    elif delta <= 2.0:  score += 3
                    elif delta >= 8.0:  score -= 6  # stora hopp → misstro

        # Luftfuktighet (sekundär)
        if h_raw is not None:
            hum = h_raw / 16.0
            if 0.0 <= hum <= 100.0:
                cand["humidity_percent"] = round(hum, 2)
                score += 1

        # Batteri (sekundär)
        if batt_raw is not None:
            batt_mv = batt_raw if batt_raw > 1000 else batt_raw * 10
            batt_v  = batt_mv / 1000.0
            if 2.0 <= batt_v <= 4.5:
                cand["battery_v"] = round(batt_v, 3)
                cand["battery_mv"] = int(batt_mv)
                score += 1

        # Vi kräver åtminstone en rimlig temp
        return (score, cand) if temp_ok else (-1, None)

    seen: set[int] = set()
    for off in candidates:
        if off in seen:
            continue
        seen.add(off)
        s, cand = parse_at(off)
        if s > best_score and cand:
            best_score = s
            best = cand

    return best




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
        try:
            manufacturer_data = {int(k): v for k, v in manufacturer_data}
        except Exception:
            pass

    service_data = e.get("service_data") or e.get("serviceData")
    tstamp = e.get("time") or e.get("timestamp")

    time_iso = None
    try:
        if isinstance(tstamp, (int, float)):
            time_iso = datetime.fromtimestamp(tstamp, tz=timezone.utc).isoformat()
    except Exception:
        pass

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
        "time_iso": time_iso,
    }

    prev_t = None
    try:
        prev_t = (_last_temp_by_addr.get(address, {}) or {}).get("temperature_c")
    except Exception:
        prev_t = None

    extra = decode_thermobeacon(manufacturer_data, address, prev_t)

    if extra:
        out.update(extra)
    return out

# --- Hårdkodat sändintervall & temp-buffer ---
SEND_INTERVAL_SEC = 10.0  # skicka var 5:e sekund
_last_temp_by_addr: Dict[str, Dict[str, Any]] = {}
_seen_in_window = 0
_decoded_in_window = 0

def _post_batch(events: list[Dict[str, Any]]) -> None:
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
        "data": events,  # [{"address": "...", "temperature_c": 23.5, "time_iso": "..."}]
    }

    if DRY_RUN:
        _log(f"[DRY_RUN] Would POST (batch={len(events)}) to {API_URL}: {json.dumps(body)[:600]}")
        return

    r = session.post(API_URL, headers=headers, json=body, timeout=15)
    ct = r.headers.get("Content-Type", "")
    snippet = (r.text or "")[:800]
    if r.ok:
        _log(f"[HTTP] OK {r.status_code} ({ct}) batch={len(events)}")
        if snippet.strip():
            _log(f"[HTTP] resp: {snippet}")
    else:
        _log(f"[HTTP] ERR {r.status_code} {r.reason} ({ct}); resp: {snippet}")
        r.raise_for_status()

def _flush_pending() -> None:
    """Skicka senaste kända temperatur per MAC i EN batch och logga även 0-fall."""
    global _seen_in_window, _decoded_in_window
    if not _last_temp_by_addr:
        _log(f"[AGG] flush: 0 temps (seen={_seen_in_window}, decoded={_decoded_in_window})")
        _seen_in_window = 0
        _decoded_in_window = 0
        return

    events = []
    for addr, st in _last_temp_by_addr.items():
        t = st.get("temperature_c")
        if t is None:
            continue
        events.append({
            "address": addr,
            "temperature_c": float(t),
            "time_iso": st.get("time_iso") or datetime.now(timezone.utc).isoformat(),
        })

    _last_temp_by_addr.clear()

    if not events:
        _log(f"[AGG] flush: 0 temps (seen={_seen_in_window}, decoded={_decoded_in_window})")
        _seen_in_window = 0
        _decoded_in_window = 0
        return

    try:
        _post_batch(events)
    finally:
        _log(f"[AGG] flush: sent {len(events)} temps (seen={_seen_in_window}, decoded={_decoded_in_window})")
        _seen_in_window = 0
        _decoded_in_window = 0

# ---------- WebSocket ----------
def _auth_and_subscribe(ws):
    # 1) auth_required
    msg = json.loads(ws.recv())
    if msg.get("type") != "auth_required":
        raise RuntimeError(f"Expected auth_required, got {msg.get('type')}")
    # 2) auth
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN saknas (krävs i add-ons).")
    ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
    msg = json.loads(ws.recv())
    if msg.get("type") != "auth_ok":
        raise RuntimeError(f"Auth failed: {msg}")
    _log("[WS] Auth OK")
    # 3) subscribe
    sub = {"id": 1, "type": "bluetooth/subscribe_advertisements"}
    ws.send(json.dumps(sub))
    _log("[WS] -> bluetooth/subscribe_advertisements skickad")

def _event_loop(ws):
    global _seen_in_window, _decoded_in_window
    try:
        ws.settimeout(1.0)  # gör recv icke-blockerande nog för periodic flush
    except Exception:
        pass

    last_flush = time.monotonic()

    while True:
        try:
            raw = ws.recv()
            msg = json.loads(raw)

            if msg.get("type") == "event" and "event" in msg:
                ev = msg["event"] or {}
                # Batch: add/update/remove
                if any(isinstance(ev.get(k), list) for k in ("add", "update", "remove")):
                    for kind in ("add", "update"):
                        for rec in (ev.get(kind) or []):
                            _seen_in_window += 1
                            e_norm = _normalize_adv(rec)
                            addr = (e_norm.get("address") or "").upper()
                            if not addr:
                                continue
                            if "temperature_c" in e_norm and e_norm["temperature_c"] is not None:
                                _decoded_in_window += 1
                                _last_temp_by_addr[addr] = {
                                    "temperature_c": e_norm["temperature_c"],
                                    "time_iso": e_norm.get("time_iso"),
                                }
                else:
                    # Singel-event
                    e = ev or {}
                    if "address" not in e and isinstance(e.get("data"), dict):
                        e = e["data"]
                    _seen_in_window += 1
                    e_norm = _normalize_adv(e)
                    addr = (e_norm.get("address") or "").upper()
                    if addr and ("temperature_c" in e_norm) and (e_norm["temperature_c"] is not None):
                        _decoded_in_window += 1
                        _last_temp_by_addr[addr] = {
                            "temperature_c": e_norm["temperature_c"],
                            "time_iso": e_norm.get("time_iso"),
                        }

        except WebSocketTimeoutException:
            # ingen data just nu
            pass

        now = time.monotonic()
        if now - last_flush >= SEND_INTERVAL_SEC:
            _flush_pending()
            last_flush = now

def main():
    # Reconnect-loop
    backoff = 1.0
    while True:
        try:
            ws = create_connection(
                HA_WS_URL,
                header=[f"Authorization: Bearer {SUPERVISOR_TOKEN}"],
                sslopt={"cert_reqs": _ssl.CERT_NONE},  # proxy är lokalt; undvik cert-stök
                timeout=30,
            )
            _log(f"[WS] Connected to {HA_WS_URL}")
            _auth_and_subscribe(ws)
            backoff = 1.0  # reset efter lyckad auth
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
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

if __name__ == "__main__":
    main()
