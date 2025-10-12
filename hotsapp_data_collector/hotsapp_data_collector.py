#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hotsapp_data_collector.py — HA states (sensor+switch) -> HTTP forward (aggregate every N seconds)

- Auths to Home Assistant REST API via Supervisor token
- Optionally maps entities to Areas via the REST registries
- Collects current states for selected domains and sends ONE aggregate payload every send_interval_sec
- No Bluetooth, no /sensors code

Körs av run.sh
"""

import os
import json
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ---------- Konfiguration ----------
ADDON_OPTIONS_PATH = os.getenv("ADDON_OPTIONS_PATH", "/data/options.json")
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")
HA_API_BASE = "http://supervisor/core/api"  # Supervisor proxy

def _read_options(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

_options = _read_options(ADDON_OPTIONS_PATH)

def opt(name: str, default: Any = None) -> Any:
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
API_URL           = opt("api_url",  "https://api.exempel.se/measurements")
API_TOKEN         = (opt("api_token") or "").strip()
DRY_RUN           = as_bool(opt("dry_run", "0"))
RETRY_TOTAL       = int(opt("retry_total", 5))
RETRY_BACKOFF     = float(opt("retry_backoff", 1.0))
SEND_INTERVAL_SEC = float(opt("send_interval_sec", 10.0))

# ---- Övrigt ----
CLIENT_NAME       = opt("client_name", "hotsapp_data_collector")
VERBOSE           = as_bool(opt("verbose", "1"))
CLIENT_ID_OVERRIDE = (opt("client_id_override", "") or "").strip()

# ------ CLIENT ID -------
import uuid, pathlib
HA_CORE_UUID_PATH = "/config/.storage/core.uuid"   # needs map: config:ro
ADDON_CLIENT_ID_PATH = "/data/client_id"

def get_client_id() -> str:
    if CLIENT_ID_OVERRIDE:
        _log(f"[CLIENT_ID] using override from options")
        return CLIENT_ID_OVERRIDE
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
        allowed_methods=None,   # retry även på POST
    )
)
session.mount("http://", adapter)
session.mount("https://", adapter)

def _ha_headers() -> Dict[str, str]:
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN saknas (krävs i add-ons).")
    return {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}

# ---------- Helpers: area lookup via REST ----------
def _build_area_lookup_rest(_requests_session, token: str) -> Dict[str, str]:
    """entity_id -> area_name via HA:s REST-register."""
    base = f"{HA_API_BASE}/config"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        areas   = _requests_session.get(f"{base}/area_registry/list",   headers=headers, timeout=10).json()
        devices = _requests_session.get(f"{base}/device_registry/list", headers=headers, timeout=10).json()
        ents    = _requests_session.get(f"{base}/entity_registry/list", headers=headers, timeout=10).json()
    except Exception as e:
        _log(f"[HA] registry fetch failed: {e}")
        return {}

    area_id_to_name = {}
    for a in (areas or []):
        aid = a.get("id") or a.get("area_id")
        if aid:
            area_id_to_name[aid] = a.get("name") or aid

    device_to_area = {}
    for d in (devices or []):
        did = d.get("id")
        if did:
            device_to_area[did] = d.get("area_id")

    entity_to_area_name = {}
    for e in (ents or []):
        ent_id = e.get("entity_id")
        if not ent_id:
            continue
        area_id = e.get("area_id") or device_to_area.get(e.get("device_id"))
        if area_id and area_id in area_id_to_name:
            entity_to_area_name[ent_id] = area_id_to_name[area_id]
    return entity_to_area_name

# ---------- State collection ----------
def _collect_from_ha_states(_requests_session, token: str, include_domains=("sensor", "switch")) -> list[Dict[str, Any]]:
    """Hämta nuvarande HA-states för angivna domäner och gör en event-lista (en per entitet)."""
    api_url = f"{HA_API_BASE}/states"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = _requests_session.get(api_url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        _log(f"[HA] states fetch failed: {e}")
        return []

    now_iso = datetime.now(timezone.utc).isoformat()
    area_by_entity = _build_area_lookup_rest(_requests_session, token)
    _log(f"[AREA] mapped entities: {len(area_by_entity)}")

    events: list[Dict[str, Any]] = []
    for s in data:
        ent_id = s.get("entity_id") or ""
        if not any(ent_id.startswith(d + ".") for d in include_domains):
            continue
        attrs = s.get("attributes") or {}
        state_raw = s.get("state")

        value_num = None
        try:
            value_num = float(state_raw)
        except Exception:
            pass

        ev = {
            "entity_id": ent_id,
            "name": attrs.get("friendly_name") or ent_id,
            "state_raw": state_raw,
            "value_num": value_num,
            "unit": attrs.get("unit_of_measurement"),
            "time_iso": s.get("last_changed") or now_iso,
            "source": "ha_state",
            "ha": {
                "attributes": attrs,
                "last_changed": s.get("last_changed"),
                "last_updated": s.get("last_updated"),
            },
        }

        area_name = area_by_entity.get(ent_id)
        if area_name:
            ev["area"] = area_name

        # keep any common address-like attribute if present, but we no longer depend on it
        addr = attrs.get("mac") or attrs.get("mac_address") or attrs.get("address")
        if addr:
            ev["address"] = addr

        events.append(ev)

    return events

# ---------- HTTP forward ----------
def _post_batch(events: list[Dict[str, Any]]) -> None:
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "x-client-id": CLIENT_ID,
    }
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    body = {
        "source": "ha_state_aggregate",
        "client": CLIENT_NAME,
        "received_utc": datetime.now(timezone.utc).isoformat(),
        "data": events,
    }

    if DRY_RUN:
        _log(f"[DRY_RUN] Would POST (batch={len(events)}) to {API_URL}: {json.dumps(body)[:600]}")
        return

    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    r = session.post(API_URL, headers=headers, data=body_bytes, timeout=20)
    ct = r.headers.get("Content-Type", "")
    snippet = (r.text or "")[:800]
    if r.ok:
        _log(f"[HTTP] OK {r.status_code} ({ct}) batch={len(events)}")
        if snippet.strip():
            _log(f"[HTTP] resp: {snippet}")
    else:
        _log(f"[HTTP] ERR {r.status_code} {r.reason} ({ct}); resp: {snippet}")
        r.raise_for_status()

def _flush_once() -> None:
    events = _collect_from_ha_states(session, SUPERVISOR_TOKEN, include_domains=("sensor", "switch"))
    if not events:
        _log("[AGG] nothing to send")
        return

    # pack into a single aggregate entry to keep backward compatibility with your receiver
    agg = {"sensors": {}, "switches": {}}
    for ev in events:
        ent_id = ev.get("entity_id", "")
        domain = ent_id.split(".", 1)[0] if "." in ent_id else ""
        bucket = "sensors" if domain == "sensor" else ("switches" if domain == "switch" else None)
        if not bucket:
            continue
        entry = {
            "entity_id": ent_id,
            "name": ev.get("name"),
            "state_raw": ev.get("state_raw"),
            "value_num": ev.get("value_num"),
            "unit": ev.get("unit"),
            "time_iso": ev.get("time_iso"),
            "attributes": (ev.get("ha") or {}).get("attributes") if isinstance(ev.get("ha"), dict) else None,
        }
        if ev.get("address"):
            entry["address"] = ev["address"]
        if ev.get("area"):
            entry["area"] = ev["area"]
        agg[bucket][ent_id] = entry

    _post_batch([{
        "aggregate": agg,
        "time_iso": datetime.now(timezone.utc).isoformat(),
        "source": "ha_state_aggregate",
    }])
    _log(f"[AGG] sent aggregate (sensors={len(agg['sensors'])}, switches={len(agg['switches'])})")

def main():
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN saknas (krävs i add-ons).")

    _log("[TX] startup: immediate send")
    try:
        _flush_once()
    except Exception as e:
        _log(f"[ERR] initial flush failed: {e}\n{traceback.format_exc()}")

    while True:
        try:
            time.sleep(SEND_INTERVAL_SEC)
            _flush_once()
        except Exception as e:
            _log(f"[ERR] periodic flush failed: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()
