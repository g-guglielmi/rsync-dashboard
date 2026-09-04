"""
Alert settings storage: environment variables provide the defaults, and the
Settings panel in the UI saves overrides to STATE_DIR/settings.json.

Precedence: a key present in settings.json wins over the environment, even
when its value is empty — so clearing a channel in the GUI really disables it.

Secrets (bot token, webhook URL, SMTP password) are never returned to the
browser; the API only reports whether each one is set.
"""
import hmac
import json
import logging
import os

import alerts
from alerts import VALID_EVENTS, _env, _env_bool, _env_float, parse_events, parse_job_intervals

log = logging.getLogger("rsync-watch.settings")

SECRET_FIELDS = (("telegram", "token"), ("discord", "webhook"), ("smtp", "password"))
CHANNELS = ("telegram", "discord", "smtp")
TLS_MODES = {"", "starttls", "ssl", "none"}


class ValidationError(ValueError):
    pass


def state_dir():
    return _env("STATE_DIR", "/data/state")


def settings_path():
    return os.path.join(state_dir(), "settings.json")


def password():
    """SETTINGS_PASSWORD — when set, changing settings / sending tests needs it."""
    return _env("SETTINGS_PASSWORD")


def check_password(supplied):
    pw = password()
    if not pw:
        return True
    return hmac.compare_digest(str(supplied or ""), pw)


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


def load_saved(path=None):
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s — using environment defaults", path, e)
        return {}


def save(saved, path=None):
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=1)
    os.replace(tmp, path)


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


def apply_update(saved, payload):
    """Returns a new saved-settings dict with `payload` applied.

    Only keys present in the payload change. For secret fields: key absent →
    keep the current value, "" → clear it, any other string → replace it.
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
        new["dashboard_url"] = str(payload["dashboard_url"] or "").strip()
    if "events" in payload:
        events = {str(e).strip().lower() for e in (payload["events"] or []) if str(e).strip()}
        unknown = events - VALID_EVENTS
        if unknown:
            raise ValidationError(f"Unknown alert events: {', '.join(sorted(unknown))}")
        new["events"] = sorted(events)
    if "job_intervals" in payload:
        intervals = {}
        for job, hours in dict(payload["job_intervals"] or {}).items():
            if hours in ("", None):
                continue  # blank = use the default
            intervals[str(job)] = _num(hours, f"Interval for {job}", 0)
        new["job_intervals"] = intervals

    for section in CHANNELS:
        if section not in payload:
            continue
        current = dict(new.get(section, {}))
        for field, value in dict(payload[section] or {}).items():
            if value is None:
                continue
            if section == "smtp" and field == "port":
                port = int(_num(value, "SMTP port", 1))
                if port > 65535:
                    raise ValidationError("SMTP port must be between 1 and 65535")
                current[field] = port
            elif section == "smtp" and field == "tls":
                mode = str(value).strip().lower()
                mode = "" if mode == "auto" else mode
                if mode not in TLS_MODES:
                    raise ValidationError("SMTP TLS must be auto, starttls, ssl or none")
                current[field] = mode
            else:
                current[field] = str(value).strip()
        if section == "telegram" and current.get("thread_id") and not current["thread_id"].isdigit():
            raise ValidationError("Telegram topic ID must be a number")
        new[section] = current

    return new


def channel_settings_for_test(effective_settings, payload):
    """Effective settings with the (possibly unsaved) values from the GUI
    applied on top — used to test a channel before saving."""
    return merge(effective_settings, apply_update({}, payload or {}))
