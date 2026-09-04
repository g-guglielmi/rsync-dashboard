import logging
import os
import time
import threading

from flask import Flask, jsonify, render_template, abort

import alerts
from log_parser import get_dashboard_data, discover_jobs, get_job_runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


LOGS_ROOT = os.environ.get("LOGS_ROOT", "/data/logs")
HISTORY_LIMIT = _env_int("HISTORY_LIMIT", 15)
CACHE_SECONDS = _env_int("CACHE_SECONDS", 20)

_cache = {"data": None, "ts": 0}
_lock = threading.Lock()

ALERT_CFG = alerts.Config.from_env()
ALERTER = alerts.Alerter(ALERT_CFG)


def get_cached_data():
    now = time.time()
    with _lock:
        if _cache["data"] is not None and (now - _cache["ts"]) <= CACHE_SECONDS:
            return _cache["data"]
    # Parse outside the lock so a slow refresh doesn't block other requests;
    # two threads refreshing at once just do the same cheap work twice.
    data = get_dashboard_data(LOGS_ROOT, history_limit=HISTORY_LIMIT)
    with _lock:
        _cache["data"] = data
        _cache["ts"] = now
    return data


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/dashboard")
def api_dashboard():
    # annotate() returns a copy, so the shared cached dict is never mutated.
    data = alerts.annotate(get_cached_data(), ALERT_CFG)
    return jsonify({**data, "logs_root_found": os.path.isdir(LOGS_ROOT)})


@app.route("/api/jobs/<server>/<category>/runs")
def api_job_runs(server, category):
    jobs = discover_jobs(LOGS_ROOT)
    match = next((j for j in jobs if j["server"] == server and j["category"] == category), None)
    if not match:
        abort(404)
    runs = get_job_runs(match["path"], limit=100)
    return jsonify({"server": server, "category": category, "runs": runs})


@app.route("/api/alerts")
def api_alerts():
    """Alerting configuration summary (never includes secrets) and state."""
    return jsonify({
        **ALERT_CFG.summary(),
        "enabled": ALERTER.enabled,
        "active": ALERTER.state.get("active", {}),
        "last_check": ALERTER.state.get("last_check"),
    })


@app.route("/api/alerts/test", methods=["GET", "POST"])
def api_alerts_test():
    """Sends a test message to every configured channel (GET allowed so it
    can be triggered from a browser address bar)."""
    if not ALERTER.enabled:
        return jsonify({"error": "No alert channel configured"}), 400
    return jsonify({"results": ALERTER.send_test()})


@app.route("/api/alerts/check", methods=["GET", "POST"])
def api_alerts_check():
    """Runs an alert evaluation right now instead of waiting for the timer."""
    sent = ALERTER.run_once(get_cached_data())
    return jsonify({"sent": [subject for subject, _ in sent], "active": ALERTER.state.get("active", {})})


if ALERTER.enabled:
    # Requires a single gunicorn worker (see Dockerfile) so this runs once.
    alerts.start_background(ALERTER, get_cached_data)
else:
    logging.getLogger("rsync-watch").info(
        "Alerting disabled (no TELEGRAM_*, DISCORD_WEBHOOK_URL or SMTP_* configured)")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8686)
