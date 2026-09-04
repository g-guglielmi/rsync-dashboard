FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python deps first so Docker can cache this layer between builds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy the app itself
COPY app.py log_parser.py alerts.py settings.py ./
COPY templates/ templates/
COPY static/ static/

# Where the container expects your logs to be mounted (read-only) — see unraid-template.xml
ENV LOGS_ROOT=/data/logs
ENV HISTORY_LIMIT=15
ENV CACHE_SECONDS=20
# Where alert state is persisted (mount a volume here to survive image updates)
ENV STATE_DIR=/data/state

# The app only reads logs and serves HTTP — no reason to run it as root.
# entrypoint.sh starts as root only to chown the state folder, then drops to
# this user re-mapped to PUID/PGID (default 99/100 = unRAID's nobody:users).
RUN useradd --create-home --shell /usr/sbin/nologin dashboard \
    && mkdir -p /data/state && chown dashboard /data/state
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV PUID=99 PGID=100

EXPOSE 8686

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8686/healthz', timeout=4)"]

ENTRYPOINT ["/entrypoint.sh"]
# One worker on purpose: the alert checker runs as a background thread inside
# the app process, and a second worker would send every notification twice.
CMD ["gunicorn", "--bind", "0.0.0.0:8686", "--workers", "1", "--threads", "4", "app:app"]
