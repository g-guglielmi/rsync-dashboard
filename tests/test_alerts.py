from datetime import datetime

import alerts
from alerts import Alerter, Config, active_conditions, annotate, parse_events, parse_job_intervals


NOW = datetime(2026, 9, 4, 12, 0, 0)


def run(start, status="success", filename=None, errors=None):
    return {"start_time": start, "status": status,
            "filename": filename or f"rsync_{start.replace(':', '').replace('-', '')}.log",
            "size_transferred_bytes": 0, "errors": errors or []}


def job(server, category, latest):
    return {"server": server, "category": category, "latest": latest,
            "runs": [latest] if latest else []}


def data(*jobs):
    return {"jobs": list(jobs), "overview": {}}


def cfg(**kw):
    # Tests that instantiate an Alerter must pass an explicit tmp state_file;
    # this default only matters for pure evaluation helpers.
    kw.setdefault("state_file", "unused/alerts.json")
    return Config(**kw)


# ---------------- config parsing ----------------

def test_parse_job_intervals():
    assert parse_job_intervals("srv/Docker=168; srv/Media=24,srv/Skip=0") == {
        "srv/Docker": 168.0, "srv/Media": 24.0, "srv/Skip": 0.0}
    assert parse_job_intervals("garbage") == {}
    assert parse_job_intervals("a/b=notanumber") == {}


def test_parse_events_defaults_and_filtering():
    assert parse_events("") == {"overdue", "interrupted"}
    assert parse_events("overdue, failed, bogus") == {"overdue", "failed"}


def test_channels_detection(monkeypatch):
    for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL", "SMTP_HOST", "SMTP_TO"):
        monkeypatch.delenv(v, raising=False)
    assert Config.from_env().channels == []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("SMTP_HOST", "mail.example")
    monkeypatch.setenv("SMTP_TO", "a@x, b@y")
    c = Config.from_env()
    assert c.channels == ["telegram", "email"]
    assert c.smtp_to == ["a@x", "b@y"]
    assert "telegram_token" not in c.summary()  # summary must never leak secrets


# ---------------- overdue evaluation ----------------

def test_on_schedule_job_is_not_overdue():
    d = annotate(data(job("s", "Backup", run("2026-09-04T05:30:00"))), cfg(), NOW)
    assert d["jobs"][0]["overdue"] is False
    assert d["overview"]["overdue"] == 0


def test_stale_job_is_overdue_with_default_26h():
    d = annotate(data(job("s", "Backup", run("2026-09-02T05:30:00"))), cfg(), NOW)
    j = d["jobs"][0]
    assert j["overdue"] is True
    assert j["overdue_age_hours"] == 54.5
    assert d["overview"]["overdue"] == 1


def test_per_job_interval_override_and_disable():
    weekly = job("s", "Docker", run("2026-08-30T04:15:00"))   # 5 days old
    skipped = job("s", "Scratch", run("2026-08-01T00:00:00"))  # ancient
    c = cfg(job_intervals={"s/Docker": 168, "s/Scratch": 0})
    d = annotate(data(weekly, skipped), c, NOW)
    assert d["jobs"][0]["overdue"] is False        # within 7 days
    assert d["jobs"][1]["overdue"] is False        # monitoring disabled
    assert d["jobs"][1]["expected_every_hours"] is None


def test_running_job_is_never_overdue():
    d = annotate(data(job("s", "Big", run("2026-09-01T00:00:00", "running"))), cfg(), NOW)
    assert d["jobs"][0]["overdue"] is False


def test_job_with_no_runs_is_overdue():
    d = annotate(data(job("s", "Empty", None)), cfg(), NOW)
    assert d["jobs"][0]["overdue"] is True


def test_annotate_does_not_mutate_input():
    src = data(job("s", "Backup", run("2026-09-02T05:30:00")))
    annotate(src, cfg(), NOW)
    assert "overdue" not in src["jobs"][0]
    assert "overdue" not in src["overview"]


# ---------------- conditions ----------------

def test_active_conditions_respect_event_selection():
    failed = job("s", "Photos", run("2026-09-04T06:05:00", "failed", errors=["rsync error: boom"]))
    c = cfg()  # default events: overdue, interrupted -> failed NOT alerted
    assert active_conditions(data(failed), c, NOW) == {}
    c2 = cfg(events={"failed"})
    conds = active_conditions(data(failed), c2, NOW)
    ref, msg = conds["s/Photos"]["failed"]
    assert ref.startswith("failed:")
    assert "rsync error: boom" in msg


def test_interrupted_is_alerted_by_default():
    conds = active_conditions(data(job("s", "Arc", run("2026-09-04T03:00:00", "interrupted"))), cfg(), NOW)
    assert "interrupted" in conds["s/Arc"]


# ---------------- alerter: dedup, recovery, channel failure isolation ----------------

class Capture:
    def __init__(self):
        self.msgs = []

    def senders(self, fail=None):
        def tg(text): self.msgs.append(("telegram", text))
        def dc(text):
            if fail == "discord":
                raise RuntimeError("webhook 404")
            self.msgs.append(("discord", text))
        def em(subject, text): self.msgs.append(("email", subject))
        return {"telegram": tg, "discord": dc, "email": em}


def test_alert_sent_once_then_recovery(tmp_path):
    cap = Capture()
    a = Alerter(cfg(state_file=str(tmp_path / "state.json")), senders=cap.senders())
    stale = data(job("s", "Backup", run("2026-09-02T05:30:00")))

    sent = a.run_once(stale, NOW)
    assert len(sent) == 1 and "OVERDUE" in sent[0][1]
    assert len(cap.msgs) == 3            # every channel

    assert a.run_once(stale, NOW) == []  # same condition -> no repeat
    assert (tmp_path / "state.json").exists()

    fresh = data(job("s", "Backup", run("2026-09-04T05:30:00")))
    sent = a.run_once(fresh, NOW)
    assert len(sent) == 1 and "RECOVERED" in sent[0][1]
    assert a.run_once(fresh, NOW) == []


def test_state_survives_restart(tmp_path):
    sf = str(tmp_path / "state.json")
    cap = Capture()
    Alerter(cfg(state_file=sf), senders=cap.senders()).run_once(
        data(job("s", "Backup", run("2026-09-02T05:30:00"))), NOW)
    # "restart": a new Alerter loads the persisted state and must not re-alert
    a2 = Alerter(cfg(state_file=sf), senders=cap.senders())
    assert a2.run_once(data(job("s", "Backup", run("2026-09-02T05:30:00"))), NOW) == []


def test_new_failure_after_failure_is_a_new_alert(tmp_path):
    cap = Capture()
    a = Alerter(cfg(state_file=str(tmp_path / "s.json"), events={"failed"}), senders=cap.senders())
    a.run_once(data(job("s", "P", run("2026-09-03T06:05:00", "failed", filename="a.log"))), NOW)
    sent = a.run_once(data(job("s", "P", run("2026-09-04T06:05:00", "failed", filename="b.log"))), NOW)
    assert len(sent) == 1 and "FAILED" in sent[0][1]   # different run -> alert again, no recovery


def test_one_broken_channel_does_not_block_others(tmp_path):
    cap = Capture()
    a = Alerter(cfg(state_file=str(tmp_path / "s.json")), senders=cap.senders(fail="discord"))
    results = a.send_test()
    assert results["telegram"] == "ok" and results["email"] == "ok"
    assert results["discord"].startswith("RuntimeError")


def test_unwritable_state_file_is_tolerated(tmp_path):
    # A path *underneath a regular file* can't be created on any OS.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    cap = Capture()
    a = Alerter(cfg(state_file=str(blocker / "alerts.json")), senders=cap.senders())
    assert len(a.run_once(data(job("s", "B", run("2026-09-02T05:30:00"))), NOW)) == 1
    assert not (blocker / "alerts.json").exists()
