FROM python:3.11-slim
ARG BUILD_NOCACHE=manual_1_0_2
RUN apt-get update && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hotsapp_fwd.py .
COPY run.sh /run.sh
CMD ["/run.sh"]