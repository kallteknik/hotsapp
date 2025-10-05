
#!/usr/bin/env python3
# Egen testkommentar2
import os, json, ssl, sys
from datetime import datetime, timezone
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import paho.mqtt.client as mqtt
import json

with open("/data/options.json") as f:
    o = json.load(f)
def opt(name, default=None):
    return o.get(name, default)


def as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1","true","yes","on"}

# ---- opt()-varianter (lowercase nycklar i options.json) ----
MQTT_HOST     = opt("mqtt_host", "core-mosquitto")
MQTT_PORT     = int(opt("mqtt_port", 1883))
MQTT_TLS      = as_bool(opt("mqtt_tls", "0"))
MQTT_CA_CERT  = opt("mqtt_ca_cert")
MQTT_CERTFILE = opt("mqtt_certfile")
MQTT_KEYFILE  = opt("mqtt_keyfile")
MQTT_USERNAME = opt("mqtt_username")
MQTT_PASSWORD = opt("mqtt_password")

DEBUG_ONLY_TOPIC = opt("debug_only_topic", "")
DEBUG_ONLY_NAME  = opt("debug_only_name", "")

TOPIC                  = opt("topic", "zigbee2mqtt/#")
EXCLUDE_BRIDGE         = as_bool(opt("exclude_bridge", "1"))
DROP_RETAINED_GRACE_SEC= int(opt("drop_retained_grace_sec", 3))

API_URL   = opt("api_url", "https://api.exempel.se/measurements")
API_TOKEN = opt("api_token")

DRY_RUN = as_bool(opt("dry_run", "0"))
raw     = opt("dry_run", "")
RETRY_TOTAL   = int(opt("retry_total", 5))
RETRY_BACKOFF = float(opt("retry_backoff", 1.0))


start_ts = datetime.now(timezone.utc)

session = requests.Session()
retry = Retry(
    total=RETRY_TOTAL,
    backoff_factor=RETRY_BACKOFF,
    status_forcelist=[429,500,502,503,504],
    allowed_methods=["POST"]
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)

headers = {"Content-Type":"application/json"}
if API_TOKEN:
    headers["Authorization"] = f"Bearer {API_TOKEN}"

def to_iso_z(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def payload_for(topic, msg_obj, qos, retain):
    return {
        "topic": topic,
        "message": msg_obj,
        "received_at": to_iso_z(datetime.now(timezone.utc)),
        "qos": qos,
        "retain": retain
    }

def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[INFO] Connected to MQTT ({MQTT_HOST}:{MQTT_PORT}) rc={reason_code}", flush=True)
    client.subscribe(TOPIC, qos=0)
    print(f"[INFO] Subscribed to: {TOPIC}", flush=True)

def on_message(client, userdata, message):
    from json import loads, JSONDecodeError
    topic = message.topic
    if EXCLUDE_BRIDGE and topic.startswith("zigbee2mqtt/bridge"):
        return
    # Släpp retained en stund vid uppstart
    if message.retain and DROP_RETAINED_GRACE_SEC > 0:
        delta = (datetime.now(timezone.utc) - start_ts).total_seconds()
        if delta < DROP_RETAINED_GRACE_SEC:
            return
    # Debug-filter: tillåt endast en specifik sensor om satt
    print(DEBUG_ONLY_NAME)
    print(topic.rsplit("/", 1)[-1])
    print(topic)
    if DEBUG_ONLY_TOPIC or DEBUG_ONLY_NAME:
        allow = False
        if DEBUG_ONLY_TOPIC:
            allow = (topic == DEBUG_ONLY_TOPIC)  # exakt topic-match
        if not allow and DEBUG_ONLY_NAME:
            last_seg = topic.rsplit("/", 1)[-1]  # sista segmentet efter '/'
            allow = (last_seg == DEBUG_ONLY_NAME)
        if not allow:
            print(f"[ERROR] Debug stop: {DEBUG_ONLY_NAME}", flush=True)
            return


    raw = message.payload.decode("utf-8", errors="replace")
    try:
        msg_obj = loads(raw)
    except JSONDecodeError:
        msg_obj = raw

    body = payload_for(topic, msg_obj, message.qos, message.retain)

    if DRY_RUN:
        print(f"[DRY_RUN] Would POST to {API_URL}: {json.dumps(body)[:500]}", flush=True)
        return
    try:
        r = session.post(API_URL, headers=headers, data=json.dumps(body), timeout=15)
        if r.status_code >= 300:
            print(f"[WARN] HTTP {r.status_code}: {r.text[:300]}", flush=True)
        else:
            print(f"[OK] Posted {topic}", flush=True)
    except Exception as e:
        print(f"[ERROR] POST failed: {e}", flush=True)

def build_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    if MQTT_TLS:
        import ssl as _ssl
        ctx = _ssl.create_default_context(cafile=MQTT_CA_CERT) if MQTT_CA_CERT else _ssl.create_default_context()
        if MQTT_CERTFILE and MQTT_KEYFILE:
            ctx.load_cert_chain(MQTT_CERTFILE, MQTT_KEYFILE)
        client.tls_set_context(ctx)
    client.on_connect = on_connect
    client.on_message = on_message
    return client

def main():
    client = build_mqtt()
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        print(f"[FATAL] MQTT connect failed: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    client.loop_forever()

if __name__ == "__main__":
    main()
