import os
import time
import threading

from flask import Flask, jsonify, render_template, abort

from log_parser import get_dashboard_data, discover_jobs, get_job_runs

app = Flask(__name__)

LOGS_ROOT = os.environ.get("LOGS_ROOT", "/data/logs")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "15"))
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "20"))

_cache = {"data": None, "ts": 0}
_lock = threading.Lock()


def get_cached_data():
    now = time.time()
    with _lock:
        if _cache["data"] is None or (now - _cache["ts"]) > CACHE_SECONDS:
            _cache["data"] = get_dashboard_data(LOGS_ROOT, history_limit=HISTORY_LIMIT)
            _cache["ts"] = now
        return _cache["data"]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
def api_dashboard():
    data = get_cached_data()
    data["logs_root_found"] = os.path.isdir(LOGS_ROOT)
    return jsonify(data)


@app.route("/api/jobs/<server>/<category>/runs")
def api_job_runs(server, category):
    jobs = discover_jobs(LOGS_ROOT)
    match = next((j for j in jobs if j["server"] == server and j["category"] == category), None)
    if not match:
        abort(404)
    runs = get_job_runs(match["path"], limit=100)
    return jsonify({"server": server, "category": category, "runs": runs})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8686)
