#!/bin/sh
set -e
echo "RUN.SH BUILD STAMP"

OPTS=/data/options.json

# Exportera env från options.json (med rimliga defaults)
export MQTT_HOST="$(jq -r '.mqtt_host // "core-mosquitto"' $OPTS)"
export MQTT_PORT="$(jq -r '.mqtt_port // 1883' $OPTS)"
export MQTT_TLS="$(jq -r '( .mqtt_tls // false ) | if . then "1" else "0" end' $OPTS)"
export MQTT_CA_CERT="$(jq -r '.mqtt_ca_cert // empty' $OPTS)"
export MQTT_CERTFILE="$(jq -r '.mqtt_certfile // empty' $OPTS)"
export MQTT_KEYFILE="$(jq -r '.mqtt_keyfile // empty' $OPTS)"
export MQTT_USERNAME="$(jq -r '.mqtt_user // empty' $OPTS)"
export MQTT_PASSWORD="$(jq -r '.mqtt_password // empty' $OPTS)"

export TOPIC="$(jq -r '.topic // "zigbee2mqtt/#"' $OPTS)"
export EXCLUDE_BRIDGE="$(jq -r '( .exclude_bridge // true ) | if . then "1" else "0" end' $OPTS)"
export DROP_RETAINED_GRACE_SEC="$(jq -r '.drop_retained_grace_sec // 3' $OPTS)"

export API_URL="$(jq -r '.api_url // "https://api.exempel.se/measurements"' $OPTS)"
export API_TOKEN="$(jq -r '.api_token // empty' $OPTS)"
export DRY_RUN="$(jq -r '( .dry_run // false ) | if . then "1" else "0" end' $OPTS)"

export RETRY_TOTAL="$(jq -r '.retry_total // 5' $OPTS)"
export RETRY_BACKOFF="$(jq -r '.retry_backoff // 1.0' $OPTS)"

exec python3 /app/hotsapp_fwd.py
