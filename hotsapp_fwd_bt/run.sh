#!/bin/sh
set -e
echo "RUN.SH BUILD STAMP"

OPTS=/data/options.json

exec python3 /app/hotsapp_fwd_bt.py
