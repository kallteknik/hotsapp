#!/bin/sh
set -e

echo "[run.sh] start"

# O-buffrade Python-loggar → syns direkt i Supervisor-loggen
export PYTHONUNBUFFERED=1

# Home Assistant WebSocket via Supervisor-proxy (kan skrivas över i addon options/env)
export HA_WS_URL="${HA_WS_URL:-ws://supervisor/core/websocket}"

# Token tillhandahålls av Supervisor när homeassistant_api: true i config.yaml
# (Python-koden läser samma env)
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"

# Pekare till options som Supervisor genererar från config.yaml
export ADDON_OPTIONS_PATH="/data/options.json"

# Små hjälploggar för felsökning
echo "[run.sh] HA_WS_URL=${HA_WS_URL}"
if [ -z "${SUPERVISOR_TOKEN}" ]; then
  echo "[run.sh] VARNING: SUPERVISOR_TOKEN saknas. Autentisering mot HA kommer att misslyckas."
fi
if [ ! -f "${ADDON_OPTIONS_PATH}" ]; then
  echo "[run.sh] OBS: ${ADDON_OPTIONS_PATH} finns inte ännu. Det skapas av Supervisor vid körning."
fi

exec python3 /app/hotsapp_fwd_bt.py
