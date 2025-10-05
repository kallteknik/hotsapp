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
import sys
import json
import time
import ssl as _ssl
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from websocket import create_connection, WebSocketConnectionClosedException

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

# ---------- HTTP session med retries ----------
session = requests.Session()
adapter = HTTPAdapter(
    max_retries=Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=False,  # retry även på POST
    )
)
session.mount("http://", adapter)
session.mount("https://", adapter)

# ---------- Hjälpare ----------
_last_sent_ts: Dict[str, float] = {}  # per address för dedup-fönster

def _log(msg: str) -> None:
    print(msg, flush=True)

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
    headers = {"Content-Type": "application/json"}
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

    r = session.post(API_URL, headers=headers, json=body, timeout=15)
    r.raise_for_status()

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

def _event_loop(ws):
    while True:
        raw = ws.recv()
        msg = json.loads(raw)

        if msg.get("type") == "event" and "event" in msg:
            e = msg["event"] or {}

            # Vissa versioner lägger allt under event["data"]
            if "address" not in e and isinstance(e.get("data"), dict):
                e = e["data"]

            # Vissa versioner skickar device-objekt
            device = e.get("device") or {}
            address = (e.get("address") or device.get("address") or device.get("id") or "")
            rssi = e.get("rssi")
            tx_power = e.get("tx_power")
            connectable = e.get("connectable")

            # service_uuids kan ligga direkt eller under advertisement/service
            service_uuids = e.get("service_uuids") or e.get("uuids") or []

            # manufacturer_data kan vara dict { "76": "base64..." } eller lista/tupler
            manufacturer_data = e.get("manufacturer_data")
            if isinstance(manufacturer_data, list):
                # om formatet är [[id, bytes/base64], ...] -> gör om till {id: val}
                try:
                    manufacturer_data = {int(k): v for k, v in manufacturer_data}
                except Exception:
                    pass

            service_data = e.get("service_data") or e.get("serviceData")

            # vissa payloads har explicit tidsfält, annars fyll i nu
            tstamp = e.get("time") or e.get("timestamp")

            # Uppdatera e med harmoniserade fält så att filtren funkar
            e_norm = {
                "address": address,
                "rssi": rssi,
                "tx_power": tx_power,
                "connectable": connectable,
                "service_uuids": service_uuids,
                "manufacturer_data": manufacturer_data,
                "service_data": service_data,
                "time": tstamp,
            }

            # Om formen fortfarande saknar address/rssi – logga första gången för felsökning
            if VERBOSE and not e_norm.get("address") and not e_norm.get("rssi"):
                _log(f"[WS] Oväntad event-form, visar rå event en gång:\n{json.dumps(msg, ensure_ascii=False)[:1200]}")

            # Använd filtren på den normaliserade formen
            msg_for_filter = {"event": e_norm}
            if _should_forward(msg_for_filter):
                try:
                    _post_measurement(e_norm)
                except Exception as ex:
                    _log(f"[HTTP] POST failed: {ex}")

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
