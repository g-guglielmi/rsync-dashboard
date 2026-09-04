"""
Optional alerting for the dashboard.

Sends a notification (Telegram, Discord and/or e-mail — each optional) when a
job is OVERDUE (didn't produce a run when it should have), INTERRUPTED, or —
if enabled — FAILED / WARNING. Every condition is alerted once, then a
recovery message is sent when it clears. State lives in a small JSON file so
container restarts don't re-send everything.

Overdue is the important one: a backup script that never ran can't report
anything, so only the dashboard can notice it's missing.

Configuration is entirely via environment variables; see README "Alerts".
"""
import json
import logging
import os
import smtplib
import ssl
import threading
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage

log = logging.getLogger("rsync-watch.alerts")

VALID_EVENTS = {"overdue", "interrupted", "failed", "warning"}
DEFAULT_EVENTS = {"overdue", "interrupted"}
STATUS_EVENTS = {"interrupted", "failed", "warning"}


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _env_float(name, default):
    try:
        return float(_env(name))
    except ValueError:
        return default


def _env_bool(name, default):
    v = _env(name).lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def parse_job_intervals(spec):
    """'server/category=168; other/job=0' -> {'server/category': 168.0, 'other/job': 0.0}

    Hours between expected runs, per job. 0 disables overdue checks for that job.
    """
    out = {}
    for part in spec.replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            log.warning("JOB_INTERVALS: ignoring '%s' (expected server/category=hours)", part)
            continue
        key, val = part.rsplit("=", 1)
        try:
            out[key.strip()] = float(val)
        except ValueError:
            log.warning("JOB_INTERVALS: ignoring '%s' (hours must be a number)", part)
    return out


def parse_events(spec):
    if not spec.strip():
        return set(DEFAULT_EVENTS)
    events = {e.strip().lower() for e in spec.split(",") if e.strip()}
    unknown = events - VALID_EVENTS
    if unknown:
        log.warning("ALERT_EVENTS: ignoring unknown event(s) %s", ", ".join(sorted(unknown)))
    return events & VALID_EVENTS


class Config:
    def __init__(self, **kw):
        self.overdue_hours = kw.get("overdue_hours", 26.0)
        self.job_intervals = kw.get("job_intervals", {})
        self.events = kw.get("events", set(DEFAULT_EVENTS))
        self.check_minutes = kw.get("check_minutes", 5.0)
        self.recovery = kw.get("recovery", True)
        self.state_file = kw.get("state_file", "/data/state/alerts.json")
        self.dashboard_url = kw.get("dashboard_url", "")
        self.telegram_token = kw.get("telegram_token", "")
        self.telegram_chat_id = kw.get("telegram_chat_id", "")
        self.telegram_thread_id = kw.get("telegram_thread_id", "")
        self.discord_webhook = kw.get("discord_webhook", "")
        self.smtp_host = kw.get("smtp_host", "")
        self.smtp_port = kw.get("smtp_port", 587)
        self.smtp_user = kw.get("smtp_user", "")
        self.smtp_password = kw.get("smtp_password", "")
        self.smtp_from = kw.get("smtp_from", "")
        self.smtp_to = kw.get("smtp_to", [])
        self.smtp_tls = kw.get("smtp_tls", "")  # starttls | ssl | none | "" (auto by port)

    @classmethod
    def from_env(cls):
        return cls(
            overdue_hours=_env_float("OVERDUE_HOURS", 26.0),
            job_intervals=parse_job_intervals(_env("JOB_INTERVALS")),
            events=parse_events(_env("ALERT_EVENTS")),
            check_minutes=max(1.0, _env_float("ALERT_CHECK_MINUTES", 5.0)),
            recovery=_env_bool("ALERT_RECOVERY", True),
            state_file=os.path.join(_env("STATE_DIR", "/data/state"), "alerts.json"),
            dashboard_url=_env("DASHBOARD_URL"),
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            telegram_thread_id=_env("TELEGRAM_THREAD_ID"),
            discord_webhook=_env("DISCORD_WEBHOOK_URL"),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=int(_env_float("SMTP_PORT", 587)),
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            smtp_from=_env("SMTP_FROM") or _env("SMTP_USER"),
            smtp_to=[a.strip() for a in _env("SMTP_TO").replace(";", ",").split(",") if a.strip()],
            smtp_tls=_env("SMTP_TLS").lower(),
        )

    @property
    def telegram_enabled(self):
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def discord_enabled(self):
        return bool(self.discord_webhook)

    @property
    def smtp_enabled(self):
        return bool(self.smtp_host and self.smtp_to)

    @property
    def channels(self):
        return [n for n, on in (("telegram", self.telegram_enabled),
                                ("discord", self.discord_enabled),
                                ("email", self.smtp_enabled)) if on]

    @property
    def enabled(self):
        return bool(self.channels)

    def summary(self):
        """Safe-to-expose config summary (no secrets)."""
        return {
            "channels": self.channels,
            "events": sorted(self.events),
            "overdue_hours": self.overdue_hours,
            "job_intervals": self.job_intervals,
            "check_minutes": self.check_minutes,
            "recovery": self.recovery,
        }


# --------------------------------------------------------------------------
# Evaluation (pure functions — easy to test)
# --------------------------------------------------------------------------

def job_key(job):
    return f"{job['server']}/{job['category']}"


def job_label(job):
    return f"{job['server']} / {job['category']}"


def expected_hours(job, cfg):
    return cfg.job_intervals.get(job_key(job), cfg.overdue_hours)


def overdue_info(job, cfg, now):
    """Returns None if the job is on schedule (or unmonitored), else a dict
    describing how late it is."""
    hours = expected_hours(job, cfg)
    if hours <= 0:
        return None
    latest = job.get("latest")
    if latest and latest.get("status") == "running":
        return None
    if latest and latest.get("start_time"):
        start = datetime.fromisoformat(latest["start_time"])
        age_h = (now - start).total_seconds() / 3600
        if age_h <= hours:
            return None
        return {"hours": hours, "age_hours": age_h, "ref": latest["filename"],
                "last_start": latest["start_time"], "last_status": latest["status"]}
    # No runs at all (never ran, or every log aged out of retention)
    return {"hours": hours, "age_hours": None, "ref": "none", "last_start": None, "last_status": None}


def annotate(data, cfg, now=None):
    """Returns a copy of the dashboard payload with per-job `overdue` info and
    an `overdue` count in the overview. Never mutates the (cached) input."""
    now = now or datetime.now()
    jobs = []
    count = 0
    for j in data["jobs"]:
        info = overdue_info(j, cfg, now)
        hours = expected_hours(j, cfg)
        jobs.append({
            **j,
            "overdue": info is not None,
            "overdue_age_hours": round(info["age_hours"], 1) if info and info["age_hours"] is not None else None,
            "expected_every_hours": hours if hours > 0 else None,
        })
        count += info is not None
    return {**data, "jobs": jobs, "overview": {**data["overview"], "overdue": count}}


def _fmt_hours(h):
    if h is None:
        return "—"
    if h < 48:
        return f"{h:.0f}h"
    return f"{h / 24:.1f} days"


def _fmt_time(iso):
    if not iso:
        return "never"
    return iso.replace("T", " ")[:16]


def active_conditions(data, cfg, now):
    """{job_key: {kind: (ref, message)}} for every condition that should be
    alerting right now, according to cfg.events."""
    out = {}
    for j in data["jobs"]:
        conds = {}
        latest = j.get("latest")
        if "overdue" in cfg.events:
            info = overdue_info(j, cfg, now)
            if info:
                if info["last_start"]:
                    detail = (f"Last run: {_fmt_time(info['last_start'])} ({info['last_status']}), "
                              f"{_fmt_hours(info['age_hours'])} ago — expected every {_fmt_hours(info['hours'])}.")
                else:
                    detail = f"No runs found at all — expected every {_fmt_hours(info['hours'])}."
                conds["overdue"] = (f"overdue:{info['ref']}",
                                    f"🚨 OVERDUE — {job_label(j)}\n{detail}")
        if latest and latest.get("status") in STATUS_EVENTS and latest["status"] in cfg.events:
            status = latest["status"]
            icon = {"failed": "❌", "warning": "⚠️", "interrupted": "⛔"}[status]
            lines = [f"{icon} {status.upper()} — {job_label(j)}",
                     f"Run started {_fmt_time(latest.get('start_time'))}."]
            errs = latest.get("errors") or []
            if errs:
                lines.append("Last error lines:")
                lines.extend(f"  {e}" for e in errs[-3:])
            conds[status] = (f"{status}:{latest['filename']}", "\n".join(lines))
        if conds:
            out[job_key(j)] = conds
    return out


# --------------------------------------------------------------------------
# Senders
# --------------------------------------------------------------------------

def _post_json(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "rsync-watch"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


def send_telegram(cfg, text):
    payload = {"chat_id": cfg.telegram_chat_id, "text": text, "disable_web_page_preview": True}
    if cfg.telegram_thread_id:
        payload["message_thread_id"] = int(cfg.telegram_thread_id)
    _post_json(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage", payload)


def send_discord(cfg, text):
    _post_json(cfg.discord_webhook, {"content": text[:1900]})


def send_email(cfg, subject, text):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from
    msg["To"] = ", ".join(cfg.smtp_to)
    msg.set_content(text)
    mode = cfg.smtp_tls or ("ssl" if cfg.smtp_port == 465 else "starttls")
    if mode == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
    with server:
        if mode == "starttls":
            server.starttls(context=ssl.create_default_context())
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


# --------------------------------------------------------------------------
# Alerter: state + dispatch
# --------------------------------------------------------------------------

class Alerter:
    def __init__(self, cfg, senders=None):
        self.cfg = cfg
        # senders: {"telegram": fn(text), "discord": fn(text), "email": fn(subject, text)}
        self.senders = senders if senders is not None else self._default_senders()
        self.state = {"active": {}}
        self._load_state()

    def _default_senders(self):
        s = {}
        if self.cfg.telegram_enabled:
            s["telegram"] = lambda text: send_telegram(self.cfg, text)
        if self.cfg.discord_enabled:
            s["discord"] = lambda text: send_discord(self.cfg, text)
        if self.cfg.smtp_enabled:
            s["email"] = lambda subject, text: send_email(self.cfg, subject, text)
        return s

    @property
    def enabled(self):
        return bool(self.senders)

    # -- state -------------------------------------------------------------

    def _load_state(self):
        try:
            with open(self.cfg.state_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("active"), dict):
                self.state = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("Could not read alert state %s: %s", self.cfg.state_file, e)

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.cfg.state_file), exist_ok=True)
            tmp = self.cfg.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.cfg.state_file)
        except OSError as e:
            log.warning("Alert state not persisted (%s): %s — alerts may repeat after a restart",
                        self.cfg.state_file, e)

    # -- dispatch ----------------------------------------------------------

    def _footer(self):
        return f"\n{self.cfg.dashboard_url}" if self.cfg.dashboard_url else ""

    def notify(self, subject, text):
        """Sends to every configured channel; returns {channel: 'ok' | error}."""
        results = {}
        body = text + self._footer()
        for name, fn in self.senders.items():
            try:
                if name == "email":
                    fn(subject, body)
                else:
                    fn(body)
                results[name] = "ok"
            except Exception as e:  # one broken channel must not block the others
                results[name] = f"{type(e).__name__}: {e}"
                log.warning("Alert via %s failed: %s", name, results[name])
        return results

    def send_test(self):
        return self.notify("Rsync Watch — test alert",
                           "✅ Test alert from Rsync Watch. If you can read this, notifications work.")

    def run_once(self, data, now=None):
        """Evaluates conditions, sends alerts for new ones and recoveries for
        cleared ones, updates state. Returns the list of (subject, message) sent."""
        now = now or datetime.now()
        active = active_conditions(data, self.cfg, now)
        prev = self.state.get("active", {})
        sent = []

        for jk, conds in active.items():
            for kind, (ref, msg) in conds.items():
                if prev.get(jk, {}).get(kind) != ref:
                    sent.append((f"Rsync Watch — {kind} — {jk}", msg))

        if self.cfg.recovery:
            jobs_by_key = {job_key(j): j for j in data["jobs"]}
            for jk, kinds in prev.items():
                for kind in kinds:
                    if kind in active.get(jk, {}):
                        continue
                    j = jobs_by_key.get(jk)
                    if j and j.get("latest"):
                        latest = j["latest"]
                        detail = (f"Latest run {_fmt_time(latest.get('start_time'))} "
                                  f"({latest.get('status')}).")
                    else:
                        detail = "Job no longer present in the logs folder."
                    sent.append((f"Rsync Watch — recovered — {jk}",
                                 f"✅ RECOVERED from {kind} — {jk.replace('/', ' / ')}\n{detail}"))

        self.state["active"] = {jk: {k: ref for k, (ref, _) in conds.items()}
                                for jk, conds in active.items()}
        self.state["last_check"] = now.isoformat(timespec="seconds")
        self._save_state()

        for subject, msg in sent:
            self.notify(subject, msg)
        return sent


def start_background(alerter, load_data, initial_delay=30):
    """Runs alerter.run_once() every cfg.check_minutes in a daemon thread."""
    def loop():
        time.sleep(initial_delay)
        while True:
            try:
                alerter.run_once(load_data())
            except Exception:
                log.exception("Alert check failed")
            time.sleep(alerter.cfg.check_minutes * 60)

    t = threading.Thread(target=loop, name="alerts", daemon=True)
    t.start()
    log.info("Alerting enabled via %s; checking every %g min; events: %s",
             ", ".join(alerter.cfg.channels), alerter.cfg.check_minutes,
             ", ".join(sorted(alerter.cfg.events)))
    return t
