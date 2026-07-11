FROM python:3.12-slim

WORKDIR /app

# Install Python deps first so Docker can cache this layer between builds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy the app itself
COPY app.py log_parser.py ./
COPY templates/ templates/
COPY static/ static/

# Where the container expects your logs to be mounted (read-only) — see docker-compose.yml
ENV LOGS_ROOT=/data/logs
ENV HISTORY_LIMIT=15
ENV CACHE_SECONDS=20

EXPOSE 8686

CMD ["gunicorn", "--bind", "0.0.0.0:8686", "--workers", "2", "--threads", "2", "app:app"]
