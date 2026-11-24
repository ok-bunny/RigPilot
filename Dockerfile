FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RIGPILOT_DATA_DIR=/data

WORKDIR /app

COPY server/requirements.txt /app/server/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates dpkg gpgv \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r /app/server/requirements.txt \
    && addgroup --system --gid 10001 rigpilot \
    && adduser --system --uid 10001 --ingroup rigpilot rigpilot \
    && mkdir -p /data \
    && chown -R rigpilot:rigpilot /data /app

COPY --chown=rigpilot:rigpilot server /app/server

USER rigpilot
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2)"

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
