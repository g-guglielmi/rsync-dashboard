# Rsync Watch — read-only dashboard + alerting for unRAID rsync backups.
# Copyright (C) 2026 g-guglielmi
# Licensed under the GNU Affero General Public License v3.0; see LICENSE.

"""
Alert settings storage: environment variables provide the defaults, and the
Settings panel in the UI saves overrides to STATE_DIR/settings.json.

Precedence: a key present in settings.json wins over the environment, even
when its value is empty — so clearing a channel in the GUI really disables it.

Secrets (bot token, webhook URL, SMTP password) are never returned to the
browser; the API only reports whether each one is set.

Changing settings requires SETTINGS_PASSWORD. Without one the settings are
read-only (ALLOW_UNPROTECTED_SETTINGS=true restores the old open behaviour).
"""
import hmac
import json
import logging
import os
import re
from urllib.parse import urlsplit

import alerts
from alerts import VALID_EVENTS, _env, _env_bool, _env_float, parse_events, parse_job_intervals

log = logging.getLogger("rsync-watch.settings")

SECRET_FIELDS = (("telegram", "token"), ("discord", "webhook"), ("smtp", "password"))
CHANNELS = ("telegram", "discord", "smtp")
CHANNEL_FIELDS = {
    "telegram": {"token", "chat_id", "thread_id"},
    "discord": {"webhook"},
    "smtp": {"host", "port", "tls", "user", "password", "from", "to"},
}
TLS_MODES = {"", "starttls", "ssl", "none"}

# Discord only serves webhooks from these hosts. Accepting any other URL would
# make the container POST to an arbitrary server on your network (SSRF).
DISCORD_WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{20,}$")

# Upper bounds for free-text fields, in characters. Generous, but bounded.
MAX_LEN = {"webhook": 2048, "dashboard_url": 2048, "to": 1024}
DEFAULT_MAX_LEN = 256
MAX_JOB_INTERVALS = 500
MIN_PASSWORD_LEN = 12

# Set by load_saved() when settings.json exists but can't be used.
_load_error = None


class ValidationError(ValueError):
    pass


def state_dir():
    return _env("STATE_DIR", "/data/state")


def settings_path():
    return os.path.join(state_dir(), "settings.json")


# --------------------------------------------------------------------------
# Password
# --------------------------------------------------------------------------

def password():
    """SETTINGS_PASSWORD — required to change settings / send tests."""
    return _env("SETTINGS_PASSWORD")


def allow_unprotected():
    """ALLOW_UNPROTECTED_SETTINGS=true: settings editable by anyone who can
    reach the port when no password is set (the pre-0.2 behaviour)."""
    return _env_bool("ALLOW_UNPROTECTED_SETTINGS", False)


def writes_locked():
    """True when settings can't be changed at all: no password, no opt-out."""
    return not password() and not allow_unprotected()


def check_password(supplied):
    pw = password()
    if not pw:
        return allow_unprotected()
    # Compare bytes: compare_digest() on str raises for non-ASCII input.
    return hmac.compare_digest(str(supplied or "").encode("utf-8"), pw.encode("utf-8"))


def password_advice():
    """Start-up warnings about the password configuration (list of strings)."""
    pw = password()
    out = []
    if pw and not pw.isascii():
        out.append("SETTINGS_PASSWORD contains non-ASCII characters; browsers cannot send "
                   "those in a header, so the Settings panel will never unlock. Use ASCII only.")
    if pw and len(pw) < MIN_PASSWORD_LEN:
        out.append(f"SETTINGS_PASSWORD is only {len(pw)} characters; use {MIN_PASSWORD_LEN}+ random ones.")
    if not pw and allow_unprotected():
        out.append("ALLOW_UNPROTECTED_SETTINGS is on: anyone who can reach this port can change "
                   "alert settings, including where alerts and the SMTP password are sent.")
    if not pw and not allow_unprotected():
        out.append("No SETTINGS_PASSWORD set: alert settings are read-only until you set one.")
    return out


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def env_settings():
    """The settings dict as defined by environment variables alone."""
    return {
        "overdue_hours": _env_float("OVERDUE_HOURS", 26.0),
        "job_intervals": parse_job_intervals(_env("JOB_INTERVALS")),
        "events": sorted(parse_events(_env("ALERT_EVENTS"))),
        "recovery": _env_bool("ALERT_RECOVERY", True),
        "check_minutes": max(1.0, _env_float("ALERT_CHECK_MINUTES", 5.0)),
        "dashboard_url": _env("DASHBOARD_URL"),
        "telegram": {
            "token": _env("TELEGRAM_BOT_TOKEN"),
            "chat_id": _env("TELEGRAM_CHAT_ID"),
            "thread_id": _env("TELEGRAM_THREAD_ID"),
        },
        "discord": {"webhook": _env("DISCORD_WEBHOOK_URL")},
        "smtp": {
            "host": _env("SMTP_HOST"),
            "port": int(_env_float("SMTP_PORT", 587)),
            "user": _env("SMTP_USER"),
            "password": _env("SMTP_PASSWORD"),
            "from": _env("SMTP_FROM"),
            "to": _env("SMTP_TO"),
            "tls": _env("SMTP_TLS").lower(),
        },
    }


def load_error():
    """Why the saved settings file is being ignored, or None."""
    return _load_error


def load_saved(path=None):
    """The saved overrides, or {} if there are none or the file is unusable.

    A file that parses as JSON but has the wrong shape (hand-edited) is
    ignored rather than crashing the app; the reason is kept for the UI."""
    global _load_error
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        _load_error = None
        return {}
    except (OSError, ValueError) as e:
        msg = f"{os.path.basename(path)} could not be read ({e}); using environment defaults."
        if msg != _load_error:   # log once per distinct problem, not per request
            log.warning("Could not read %s: %s — using environment defaults", path, e)
        _load_error = msg
        return {}
    problem = None
    if not isinstance(data, dict):
        problem = "not a JSON object"
    else:
        try:
            eff = merge(env_settings(), data)
            to_config(eff)
            masked(eff)
        except Exception as e:  # wrong types / missing sections after a hand edit
            problem = f"{type(e).__name__}: {e}"
    if problem:
        msg = (f"{os.path.basename(path)} has an unexpected shape and is being ignored; "
               "environment defaults are in use. Saving here will overwrite it.")
        if msg != _load_error:
            log.error("%s is unusable (%s); ignoring it. Saving from the Settings panel overwrites it.",
                      path, problem)
        _load_error = msg
        return {}
    _load_error = None
    return data


def save(saved, path=None):
    alerts.write_private_json(path or settings_path(), saved)


def merge(base, override):
    """Saved settings over env defaults. Channel sections merge per field;
    everything else (including job_intervals) is replaced wholesale."""
    out = dict(base)
    for key, value in override.items():
        if key in CHANNELS and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def effective(path=None):
    return merge(env_settings(), load_saved(path))


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------

def to_config(s):
    smtp = s["smtp"]
    to = [a.strip() for a in str(smtp.get("to", "")).replace(";", ",").split(",") if a.strip()]
    return alerts.Config(
        overdue_hours=float(s["overdue_hours"]),
        job_intervals={k: float(v) for k, v in dict(s["job_intervals"]).items()},
        events=set(s["events"]),
        check_minutes=float(s["check_minutes"]),
        recovery=bool(s["recovery"]),
        state_file=os.path.join(state_dir(), "alerts.json"),
        dashboard_url=s["dashboard_url"],
        telegram_token=s["telegram"]["token"],
        telegram_chat_id=s["telegram"]["chat_id"],
        telegram_thread_id=s["telegram"]["thread_id"],
        discord_webhook=s["discord"]["webhook"],
        smtp_host=smtp["host"],
        smtp_port=int(smtp["port"]),
        smtp_user=smtp["user"],
        smtp_password=smtp["password"],
        smtp_from=smtp["from"] or smtp["user"],
        smtp_to=to,
        smtp_tls=smtp["tls"],
    )


def masked(s):
    """Copy safe to send to the browser: secrets replaced by <field>_set flags."""
    out = json.loads(json.dumps(s))
    for section, field in SECRET_FIELDS:
        out[section][f"{field}_set"] = bool(out[section].pop(field, ""))
    return out


# --------------------------------------------------------------------------
# Updates from the GUI
# --------------------------------------------------------------------------

def _num(value, name, minimum):
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a number")
    if f < minimum:
        raise ValidationError(f"{name} must be at least {minimum:g}")
    return f


def _text(value, name, max_len=DEFAULT_MAX_LEN):
    if isinstance(value, (dict, list)):
        raise ValidationError(f"{name} must be text")
    s = str(value if value is not None else "").strip()
    if len(s) > max_len:
        raise ValidationError(f"{name} is too long (max {max_len} characters)")
    return s


def _obj(value, name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    return value


def _list(value, name):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be a list")
    return value


def _validate_webhook(url):
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or host not in DISCORD_WEBHOOK_HOSTS or not u.path.startswith("/api/webhooks/"):
        raise ValidationError("Discord webhook must look like https://discord.com/api/webhooks/...")


def _guard_smtp_password_reuse(current, payload_fields, before):
    """A stored SMTP password must never be sent to a different server. If the
    host, port or TLS mode changed and no new password came with it, the user
    has to type the password again (or clear it)."""
    if "password" in payload_fields or not before.get("password"):
        return
    changed = any(str(current.get(k, before.get(k))) != str(before.get(k)) for k in ("host", "port", "tls"))
    if changed:
        raise ValidationError("SMTP server settings changed: re-enter the SMTP password to confirm "
                              "(or clear it if the new server needs none)")


def apply_update(saved, payload, baseline=None):
    """Returns a new saved-settings dict with `payload` applied.

    Only keys present in the payload change. For secret fields: key absent →
    keep the current value, "" → clear it, any other string → replace it.
    `baseline` is what the settings look like before this update (defaults
    to env + saved); it's what the SMTP password-reuse guard compares to.
    """
    new = json.loads(json.dumps(saved))
    if not isinstance(payload, dict):
        raise ValidationError("Settings must be a JSON object")

    if "overdue_hours" in payload:
        new["overdue_hours"] = _num(payload["overdue_hours"], "Overdue hours", 0)
    if "check_minutes" in payload:
        new["check_minutes"] = _num(payload["check_minutes"], "Check interval (minutes)", 1)
    if "recovery" in payload:
        new["recovery"] = bool(payload["recovery"])
    if "dashboard_url" in payload:
        new["dashboard_url"] = _text(payload["dashboard_url"], "Dashboard link", MAX_LEN["dashboard_url"])
    if "events" in payload:
        events = {str(e).strip().lower() for e in _list(payload["events"], "events") if str(e).strip()}
        unknown = events - VALID_EVENTS
        if unknown:
            raise ValidationError(f"Unknown alert events: {', '.join(sorted(unknown))}")
        new["events"] = sorted(events)
    if "job_intervals" in payload:
        items = _obj(payload["job_intervals"], "job_intervals")
        if len(items) > MAX_JOB_INTERVALS:
            raise ValidationError(f"Too many job intervals (max {MAX_JOB_INTERVALS})")
        intervals = {}
        for job, hours in items.items():
            if hours in ("", None):
                continue  # blank = use the default
            intervals[_text(job, "Job name")] = _num(hours, f"Interval for {job}", 0)
        new["job_intervals"] = intervals

    for section in CHANNELS:
        if section not in payload:
            continue
        current = dict(new.get(section, {}))
        fields = _obj(payload[section], section)
        for field, value in fields.items():
            if field not in CHANNEL_FIELDS[section]:
                raise ValidationError(f"Unknown {section} field '{field}'")
            if value is None:
                continue
            if section == "smtp" and field == "port":
                port = int(_num(value, "SMTP port", 1))
                if port > 65535:
                    raise ValidationError("SMTP port must be between 1 and 65535")
                current[field] = port
            elif section == "smtp" and field == "tls":
                mode = _text(value, "SMTP TLS").lower()
                mode = "" if mode == "auto" else mode
                if mode not in TLS_MODES:
                    raise ValidationError("SMTP TLS must be auto, starttls, ssl or none")
                current[field] = mode
            elif section == "discord" and field == "webhook":
                v = _text(value, "Discord webhook", MAX_LEN["webhook"])
                if v:
                    _validate_webhook(v)
                current[field] = v
            elif section == "telegram" and field == "token":
                v = _text(value, "Telegram bot token")
                if v and not TELEGRAM_TOKEN_RE.match(v):
                    raise ValidationError("That doesn't look like a Telegram bot token (expected 123456789:AAxx...)")
                current[field] = v
            else:
                current[field] = _text(value, f"{section} {field}", MAX_LEN.get(field, DEFAULT_MAX_LEN))
        if section == "telegram" and current.get("thread_id") and not current["thread_id"].isdigit():
            raise ValidationError("Telegram topic ID must be a number")
        if section == "smtp":
            before = (baseline if baseline is not None else merge(env_settings(), saved))["smtp"]
            _guard_smtp_password_reuse(current, fields, before)
        new[section] = current

    return new


def channel_settings_for_test(effective_settings, payload):
    """Effective settings with the (possibly unsaved) values from the GUI
    applied on top — used to test a channel before saving."""
    return merge(effective_settings, apply_update({}, payload or {}, baseline=effective_settings))
