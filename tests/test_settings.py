import json

import pytest

import settings as st
from settings import ValidationError, apply_update, masked, merge, to_config


BASE = {
    "overdue_hours": 26.0, "job_intervals": {}, "events": ["interrupted", "overdue"],
    "recovery": True, "check_minutes": 5.0, "dashboard_url": "",
    "telegram": {"token": "env-token", "chat_id": "111", "thread_id": ""},
    "discord": {"webhook": ""},
    "smtp": {"host": "", "port": 587, "user": "", "password": "", "from": "", "to": "", "tls": ""},
}


def test_env_defaults_when_nothing_set(monkeypatch):
    for v in ("OVERDUE_HOURS", "TELEGRAM_BOT_TOKEN", "SMTP_HOST", "ALERT_EVENTS"):
        monkeypatch.delenv(v, raising=False)
    s = st.env_settings()
    assert s["overdue_hours"] == 26.0 and s["events"] == ["interrupted", "overdue"]
    assert to_config(s).channels == []


def test_saved_wins_over_env_even_when_empty():
    saved = {"telegram": {"token": ""}, "overdue_hours": 30}
    eff = merge(BASE, saved)
    assert eff["telegram"] == {"token": "", "chat_id": "111", "thread_id": ""}  # per-field merge
    assert eff["overdue_hours"] == 30
    assert to_config(eff).telegram_enabled is False  # cleared in GUI disables despite env


def test_masked_never_contains_secrets():
    m = masked(merge(BASE, {"smtp": {"password": "s3cret", "host": "h", "to": "a@b"}}))
    dumped = json.dumps(m)
    assert "env-token" not in dumped and "s3cret" not in dumped
    assert m["telegram"]["token_set"] is True
    assert m["smtp"]["password_set"] is True
    assert m["discord"]["webhook_set"] is False


def test_apply_update_secret_semantics():
    saved = {"telegram": {"token": "old", "chat_id": "1"}}
    # absent -> keep
    assert apply_update(saved, {"telegram": {"chat_id": "2"}})["telegram"]["token"] == "old"
    # "" -> clear
    assert apply_update(saved, {"telegram": {"token": ""}})["telegram"]["token"] == ""
    # value -> replace
    assert apply_update(saved, {"telegram": {"token": "new"}})["telegram"]["token"] == "new"
    # untouched sections are not added
    assert "smtp" not in apply_update(saved, {"telegram": {"chat_id": "3"}})


def test_apply_update_general_fields_and_intervals():
    new = apply_update({}, {
        "overdue_hours": "48", "check_minutes": 10, "recovery": False, "dashboard_url": " http://x ",
        "events": ["Overdue", "failed"],
        "job_intervals": {"s/Docker": "168", "s/Scratch": 0, "s/Blank": ""},
        "smtp": {"port": "465", "tls": "AUTO", "to": "a@x, b@y"},
    })
    assert new["overdue_hours"] == 48.0 and new["check_minutes"] == 10.0
    assert new["recovery"] is False and new["dashboard_url"] == "http://x"
    assert new["events"] == ["failed", "overdue"]
    assert new["job_intervals"] == {"s/Docker": 168.0, "s/Scratch": 0.0}   # blank dropped
    assert new["smtp"]["port"] == 465 and new["smtp"]["tls"] == ""          # auto -> ""


@pytest.mark.parametrize("payload,fragment", [
    ({"overdue_hours": "abc"}, "number"),
    ({"check_minutes": 0}, "at least 1"),
    ({"events": ["bogus"]}, "Unknown alert events"),
    ({"smtp": {"port": 70000}}, "65535"),
    ({"smtp": {"tls": "tls1"}}, "TLS"),
    ({"telegram": {"thread_id": "abc"}}, "topic ID"),
    ("not a dict", "JSON object"),
])
def test_apply_update_validation(payload, fragment):
    with pytest.raises(ValidationError) as ei:
        apply_update({}, payload)
    assert fragment in str(ei.value)


def test_to_config_smtp_recipients_and_from_fallback():
    cfg = to_config(merge(BASE, {"smtp": {"host": "mail", "user": "me@x", "to": "a@x; b@y"}}))
    assert cfg.smtp_enabled and cfg.smtp_to == ["a@x", "b@y"] and cfg.smtp_from == "me@x"


def test_password_check(monkeypatch):
    monkeypatch.delenv("SETTINGS_PASSWORD", raising=False)
    assert st.check_password(None) is True            # no password configured -> open
    monkeypatch.setenv("SETTINGS_PASSWORD", "hunter2")
    assert st.check_password("hunter2") is True
    assert st.check_password("nope") is False
    assert st.check_password(None) is False


def test_save_and_load_roundtrip(tmp_path):
    p = str(tmp_path / "settings.json")
    st.save({"overdue_hours": 12}, p)
    assert st.load_saved(p) == {"overdue_hours": 12}
    assert st.load_saved(str(tmp_path / "missing.json")) == {}


# ------------------------------------------------------------ HTTP layer --

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SETTINGS_PASSWORD", raising=False)
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_settings_roundtrip_via_api(client, tmp_path):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.get_json()["password_required"] is False

    r = client.put("/api/settings", json={"telegram": {"token": "abc", "chat_id": "42"}, "overdue_hours": 30})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["settings"]["telegram"]["token_set"] is True
    assert "abc" not in json.dumps(body)                       # secret never echoed
    assert body["channels"] == ["telegram"]                    # alerter reconfigured live
    assert json.load(open(tmp_path / "settings.json"))["telegram"]["token"] == "abc"

    r = client.put("/api/settings", json={"overdue_hours": "x"})
    assert r.status_code == 400 and "number" in r.get_json()["error"]


def test_password_guard_via_api(client, monkeypatch):
    monkeypatch.setenv("SETTINGS_PASSWORD", "pw")
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings", json={}).status_code == 401
    assert client.get("/api/alerts/check").status_code == 401
    assert client.get("/api/settings", headers={"X-Settings-Password": "wrong"}).status_code == 401
    assert client.get("/api/settings", headers={"X-Settings-Password": "pw"}).status_code == 200
    assert client.get("/api/alerts").status_code == 200        # non-secret summary stays open


def test_single_channel_test_endpoint_reports_missing_config(client):
    r = client.post("/api/alerts/test", json={"channel": "discord", "settings": {"discord": {"webhook": ""}}})
    assert r.status_code == 200
    assert "required" in r.get_json()["results"]["discord"]
    r = client.post("/api/alerts/test", json={"channel": "pager"})
    assert "Unknown channel" in r.get_json()["results"]["pager"]
