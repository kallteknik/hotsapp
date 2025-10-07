FROM python:3.11-slim

# Bumpa denna när du vill forsa om-bygge i CI
ARG BUILD_NOCACHE=manual_0_0_21

# Basala verktyg (jq kan vara smidigt vid felsökning), inga onödiga rekommenderade paket
RUN apt-get update \
 && apt-get install -y --no-install-recommends jq \
 && rm -rf /var/lib/apt/lists/*

# Miljöflaggor: snabbare bygg och tydligare loggar
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Installera beroenden först för bättre layer-cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-kod och startscript
COPY hotsapp_fwd_bt.py .
COPY run.sh /run.sh
COPY sensors ./sensors

# Se till att startscriptet är körbart
RUN chmod +x /run.sh

# Kör via vårt startscript (sätter upp env och startar python)
CMD ["/run.sh"]
