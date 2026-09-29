# Rsync Watch — read-only dashboard + alerting for unRAID rsync backups.
# Copyright (C) 2026 g-guglielmi
# Licensed under the GNU Affero General Public License v3.0; see LICENSE.

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
log = logging.getLogger("rsync-watch")

# Shown in the footer next to the source link. Bump on each release.
APP_VERSION = "0.2.0"

app = Flask(__name__)
# Settings payloads are a few KB; anything bigger is not a settings payload.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_list(name):
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


LOGS_ROOT = os.environ.get("LOGS_ROOT", "/data/logs")
HISTORY_LIMIT = _env_int("HISTORY_LIMIT", 15)
CACHE_SECONDS = _env_int("CACHE_SECONDS", 20)

# Host-header allow-list (defeats DNS rebinding). Off unless TRUSTED_HOSTS is
# set. Loopback is always included so the Docker HEALTHCHECK keeps working.
TRUSTED_HOSTS = _env_list("TRUSTED_HOSTS")
if TRUSTED_HOSTS:
    app.config["TRUSTED_HOSTS"] = sorted(set(TRUSTED_HOSTS) | {"127.0.0.1", "localhost"})

# Who may embed the dashboard in a frame (CSP frame-ancestors). 'self' by
# default; e.g. FRAME_ANCESTORS="https://organizr.home.lan" for a dashboard app.
FRAME_ANCESTORS = os.environ.get("FRAME_ANCESTORS", "").strip() or "'self'"

# Wrong-password throttle, per client address (one worker, so in-memory is enough).
MAX_FAILED_PASSWORDS = 10
FAILED_PASSWORD_WINDOW = 15 * 60

_cache = {"data": None, "ts": 0}
_lock = threading.Lock()
_failed = {}                 # client ip -> (count, window start)
_failed_lock = threading.Lock()


def _initial_config():
    try:
        return settings_store.to_config(settings_store.effective())
    except Exception:
        log.exception("Saved settings are unusable; starting with environment defaults")
        return settings_store.to_config(settings_store.env_settings())


# Alert config = environment defaults + whatever was saved from the Settings panel.
ALERTER = alerts.Alerter(_initial_config())


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


# ------------------------------------------------------------ protection --

def _password_blocked(ip):
    with _failed_lock:
        count, since = _failed.get(ip, (0, 0.0))
        if time.time() - since > FAILED_PASSWORD_WINDOW:
            _failed.pop(ip, None)
            return False
        return count >= MAX_FAILED_PASSWORDS


def _password_failed(ip):
    with _failed_lock:
        count, since = _failed.get(ip, (0, time.time()))
        if time.time() - since > FAILED_PASSWORD_WINDOW:
            count, since = 0, time.time()
        _failed[ip] = (count + 1, since)
    log.warning("Wrong settings password from %s", ip)


def protected(fn):
    """Write endpoints. They need the X-Settings-Password header, and refuse
    to work at all while no password is configured (unless opted out)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if settings_store.writes_locked():
            return jsonify({"error": "Alert settings are read-only until SETTINGS_PASSWORD is set on the "
                                     "container (or ALLOW_UNPROTECTED_SETTINGS=true to accept the risk).",
                            "setup_required": True}), 403
        ip = request.remote_addr or "?"
        if _password_blocked(ip):
            return jsonify({"error": "Too many wrong passwords; try again in 15 minutes"}), 429
        supplied = request.headers.get("X-Settings-Password")
        if not settings_store.check_password(supplied):
            if supplied is not None:      # a wrong guess counts; merely opening the panel doesn't
                _password_failed(ip)
            return jsonify({"error": "Settings password required", "password_required": True}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
        f"base-uri 'none'; form-action 'self'; frame-ancestors {FRAME_ANCESTORS}")
    if FRAME_ANCESTORS == "'self'":
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    elif FRAME_ANCESTORS == "'none'":
        resp.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "Request body too large"}), 413


# ----------------------------------------------------------------- pages --

@app.route("/")
def index():
    return render_template("index.html", version=APP_VERSION)


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


@app.route("/api/alerts/test", methods=["POST"])
@protected
def api_alerts_test():
    """Sends a test message.

    {"channel": "telegram", "settings": {...}} tests ONE channel using the
    given (possibly unsaved) values on top of the current config — what the
    Settings panel's "Send test" buttons do. Without a channel, every
    configured channel gets a test. POST only: it has side effects.
    """
    body = request.get_json(silent=True) or {}
    channel = body.get("channel")
    if channel:
        try:
            eff = settings_store.channel_settings_for_test(settings_store.effective(), body.get("settings"))
        except (settings_store.ValidationError, TypeError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"results": {channel: alerts.send_test_for(settings_store.to_config(eff), channel)}})
    if not ALERTER.enabled:
        return jsonify({"error": "No alert channel configured"}), 400
    return jsonify({"results": ALERTER.send_test()})


@app.route("/api/alerts/check", methods=["POST"])
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
        "config_error": settings_store.load_error(),
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
    except (settings_store.ValidationError, TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    try:
        settings_store.save(new_saved)
    except OSError as e:
        return jsonify({"error": f"Could not write settings file: {e}. Is the State Folder mounted and writable?"}), 500
    ALERTER.reconfigure(settings_store.to_config(settings_store.effective()))
    return jsonify(_settings_payload())


# --------------------------------------------------------------- startup --

def _check_state_dir():
    """Log a clear hint if saved settings/alert state can't be written, and
    make sure files written by an older version aren't world-readable."""
    d = settings_store.state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        log.warning("State dir %s is not writable (%s): alert settings and sent-alert memory will not "
                    "persist. Check the State Folder mapping and PUID/PGID.", d, e)
    for name in ("settings.json", "alerts.json"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass


_check_state_dir()
for _msg in settings_store.password_advice():
    log.warning(_msg)
if TRUSTED_HOSTS:
    log.info("Trusted hosts: %s", ", ".join(app.config["TRUSTED_HOSTS"]))

# The checker thread is always running; it idles until a channel is configured
# (env or Settings panel). One gunicorn worker (see Dockerfile) keeps it single.
alerts.start_background(ALERTER, get_cached_data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8686)
