import logging
import os
import time
import threading
from functools import wraps

from flask import Flask, jsonify, render_template, abort, request

import alerts
import settings as settings_store
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

# Alert config = environment defaults + whatever was saved from the Settings panel.
ALERTER = alerts.Alerter(settings_store.to_config(settings_store.effective()))


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


def protected(fn):
    """Requires the X-Settings-Password header when SETTINGS_PASSWORD is set."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not settings_store.check_password(request.headers.get("X-Settings-Password")):
            return jsonify({"error": "Settings password required", "password_required": True}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/dashboard")
def api_dashboard():
    # annotate() returns a copy, so the shared cached dict is never mutated.
    data = alerts.annotate(get_cached_data(), ALERTER.cfg)
    return jsonify({**data, "logs_root_found": os.path.isdir(LOGS_ROOT)})


@app.route("/api/jobs/<server>/<category>/runs")
def api_job_runs(server, category):
    jobs = discover_jobs(LOGS_ROOT)
    match = next((j for j in jobs if j["server"] == server and j["category"] == category), None)
    if not match:
        abort(404)
    runs = get_job_runs(match["path"], limit=100)
    return jsonify({"server": server, "category": category, "runs": runs})


# ---------------------------------------------------------------- alerts --

@app.route("/api/alerts")
def api_alerts():
    """Alerting summary (never includes secrets) and currently active conditions."""
    return jsonify({
        **ALERTER.cfg.summary(),
        "enabled": ALERTER.enabled,
        "active": ALERTER.state.get("active", {}),
        "last_check": ALERTER.state.get("last_check"),
    })


@app.route("/api/alerts/test", methods=["GET", "POST"])
@protected
def api_alerts_test():
    """Sends a test message.

    POST {"channel": "telegram", "settings": {...}} tests ONE channel using the
    given (possibly unsaved) values on top of the current config — what the
    Settings panel's "Send test" buttons do. Without a channel, every
    configured channel gets a test (GET works too, for the address bar).
    """
    body = request.get_json(silent=True) or {}
    channel = body.get("channel") or request.args.get("channel")
    if channel:
        try:
            eff = settings_store.channel_settings_for_test(settings_store.effective(), body.get("settings"))
        except settings_store.ValidationError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"results": {channel: alerts.send_test_for(settings_store.to_config(eff), channel)}})
    if not ALERTER.enabled:
        return jsonify({"error": "No alert channel configured"}), 400
    return jsonify({"results": ALERTER.send_test()})


@app.route("/api/alerts/check", methods=["GET", "POST"])
@protected
def api_alerts_check():
    """Runs an alert evaluation right now instead of waiting for the timer."""
    sent = ALERTER.run_once(get_cached_data())
    return jsonify({"sent": [subject for subject, _ in sent], "active": ALERTER.state.get("active", {})})


# -------------------------------------------------------------- settings --

def _settings_payload():
    eff = settings_store.effective()
    return {
        "password_required": bool(settings_store.password()),
        "settings": settings_store.masked(eff),
        "channels": ALERTER.cfg.channels,
        "jobs": [f"{j['server']}/{j['category']}" for j in get_cached_data()["jobs"]],
    }


@app.route("/api/settings", methods=["GET"])
@protected
def api_settings_get():
    return jsonify(_settings_payload())


@app.route("/api/settings", methods=["PUT"])
@protected
def api_settings_put():
    try:
        new_saved = settings_store.apply_update(settings_store.load_saved(), request.get_json(silent=True))
    except settings_store.ValidationError as e:
        return jsonify({"error": str(e)}), 400
    try:
        settings_store.save(new_saved)
    except OSError as e:
        return jsonify({"error": f"Could not write settings file: {e}. Is the State Folder mounted and writable?"}), 500
    ALERTER.reconfigure(settings_store.to_config(settings_store.effective()))
    return jsonify(_settings_payload())


def _check_state_dir_writable():
    """Log a clear hint at start-up if saved settings/alert state can't be written."""
    d = settings_store.state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        logging.getLogger("rsync-watch").warning(
            "State dir %s is not writable (%s): alert settings and sent-alert memory will not "
            "persist. Check the State Folder mapping and PUID/PGID.", d, e)


_check_state_dir_writable()

# The checker thread is always running; it idles until a channel is configured
# (env or Settings panel). One gunicorn worker (see Dockerfile) keeps it single.
alerts.start_background(ALERTER, get_cached_data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8686)
