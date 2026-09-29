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


TOKEN_A = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123"
TOKEN_B = "123456789:AAFzyxwvutsrqponmlkjihgfedcba9876"
WEBHOOK = "https://discord.com/api/webhooks/1234567890/abcDEF_ghi-JKL"


def test_apply_update_secret_semantics():
    saved = {"telegram": {"token": TOKEN_A, "chat_id": "1"}}
    # absent -> keep
    assert apply_update(saved, {"telegram": {"chat_id": "2"}})["telegram"]["token"] == TOKEN_A
    # "" -> clear
    assert apply_update(saved, {"telegram": {"token": ""}})["telegram"]["token"] == ""
    # value -> replace
    assert apply_update(saved, {"telegram": {"token": TOKEN_B}})["telegram"]["token"] == TOKEN_B
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
    # wrong JSON types must be a clean 400, never a 500
    ({"job_intervals": 5}, "must be an object"),
    ({"telegram": "abc"}, "must be an object"),
    ({"telegram": ["ab"]}, "must be an object"),
    ({"events": 5}, "must be a list"),
    ({"smtp": {"host": {"a": 1}}}, "must be text"),
    # bounded lengths
    ({"smtp": {"host": "x" * 300}}, "too long"),
    ({"job_intervals": {f"s/j{i}": 1 for i in range(501)}}, "Too many"),
    # only real Discord webhooks over https (anything else is SSRF)
    ({"discord": {"webhook": "http://discord.com/api/webhooks/1/a"}}, "https://discord.com"),
    ({"discord": {"webhook": "https://evil.example/api/webhooks/1/a"}}, "https://discord.com"),
    ({"discord": {"webhook": "https://discord.com/other/1/a"}}, "https://discord.com"),
    ({"discord": {"webhook": "https://discord.com.evil.example/api/webhooks/1/a"}}, "https://discord.com"),
    ({"telegram": {"token": "not-a-token"}}, "bot token"),
    ({"telegram": {"bogus": 1}}, "Unknown telegram field"),
])
def test_apply_update_validation(payload, fragment):
    with pytest.raises(ValidationError) as ei:
        apply_update({}, payload)
    assert fragment in str(ei.value)


def test_valid_webhook_and_token_are_accepted():
    new = apply_update({}, {"discord": {"webhook": WEBHOOK}, "telegram": {"token": TOKEN_A}})
    assert new["discord"]["webhook"] == WEBHOOK and new["telegram"]["token"] == TOKEN_A
    for host in ("discordapp.com", "ptb.discord.com", "canary.discord.com"):
        apply_update({}, {"discord": {"webhook": f"https://{host}/api/webhooks/1/a"}})


def test_smtp_password_is_not_reused_for_a_different_server():
    saved = {"smtp": {"host": "mail.good", "port": 587, "tls": "", "user": "me", "password": "s3cret", "to": "a@x"}}
    # same server, password left blank ("keep") -> fine
    apply_update(saved, {"smtp": {"host": "mail.good", "port": "587", "tls": "auto", "user": "me", "to": "a@x"}})
    # host changed without retyping the password -> refused, for each transport field
    for change in ({"host": "mail.evil"}, {"port": "25"}, {"tls": "none"}):
        with pytest.raises(ValidationError) as ei:
            apply_update(saved, {"smtp": change})
        assert "re-enter" in str(ei.value)
    # host changed WITH a new password, or clearing it, is allowed
    assert apply_update(saved, {"smtp": {"host": "mail.new", "password": "other"}})["smtp"]["host"] == "mail.new"
    assert apply_update(saved, {"smtp": {"host": "mail.new", "password": ""}})["smtp"]["password"] == ""
    # no stored password at all -> nothing to protect
    apply_update({"smtp": {"host": "relay", "password": ""}}, {"smtp": {"host": "relay2"}})


def test_smtp_guard_uses_effective_settings_for_channel_tests(monkeypatch):
    # The password may come from the environment, not the saved file.
    eff = merge(BASE, {"smtp": {"host": "mail.good", "password": "s3cret", "to": "a@x"}})
    with pytest.raises(ValidationError):
        st.channel_settings_for_test(eff, {"smtp": {"host": "mail.evil", "to": "a@x"}})
    out = st.channel_settings_for_test(eff, {"smtp": {"host": "mail.good", "to": "b@y"}})
    assert out["smtp"]["to"] == "b@y" and out["smtp"]["password"] == "s3cret"


def test_to_config_smtp_recipients_and_from_fallback():
    cfg = to_config(merge(BASE, {"smtp": {"host": "mail", "user": "me@x", "to": "a@x; b@y"}}))
    assert cfg.smtp_enabled and cfg.smtp_to == ["a@x", "b@y"] and cfg.smtp_from == "me@x"


def test_password_check(monkeypatch):
    monkeypatch.delenv("SETTINGS_PASSWORD", raising=False)
    monkeypatch.delenv("ALLOW_UNPROTECTED_SETTINGS", raising=False)
    assert st.writes_locked() is True                  # no password -> read-only by default
    assert st.check_password(None) is False
    monkeypatch.setenv("ALLOW_UNPROTECTED_SETTINGS", "true")
    assert st.writes_locked() is False                 # explicit opt-out -> open
    assert st.check_password(None) is True
    monkeypatch.setenv("SETTINGS_PASSWORD", "hunter2-hunter2")
    assert st.writes_locked() is False
    assert st.check_password("hunter2-hunter2") is True
    assert st.check_password("nope") is False
    assert st.check_password(None) is False


def test_password_check_survives_non_ascii(monkeypatch):
    # hmac.compare_digest on str raises for non-ASCII; the app must not 500.
    monkeypatch.setenv("SETTINGS_PASSWORD", "hunter2-hunter2")
    assert st.check_password("perché") is False
    monkeypatch.setenv("SETTINGS_PASSWORD", "perché")
    assert st.check_password("perché") is True
    assert any("non-ASCII" in m for m in st.password_advice())


def test_password_advice(monkeypatch):
    monkeypatch.delenv("ALLOW_UNPROTECTED_SETTINGS", raising=False)
    monkeypatch.setenv("SETTINGS_PASSWORD", "short")
    assert any("characters" in m for m in st.password_advice())
    monkeypatch.setenv("SETTINGS_PASSWORD", "long-enough-password")
    assert st.password_advice() == []
    monkeypatch.delenv("SETTINGS_PASSWORD")
    assert any("read-only" in m for m in st.password_advice())
    monkeypatch.setenv("ALLOW_UNPROTECTED_SETTINGS", "1")
    assert any("anyone" in m for m in st.password_advice())


def test_unusable_saved_file_is_ignored_not_fatal(tmp_path):
    p = str(tmp_path / "settings.json")
    p_bad = tmp_path / "settings.json"
    p_bad.write_text('{"overdue_hours": "abc", "telegram": "nope"}', encoding="utf-8")
    assert st.load_saved(p) == {}
    assert "ignored" in st.load_error()
    to_config(st.effective(p))                          # must not raise
    p_bad.write_text('{"overdue_hours": 30}', encoding="utf-8")
    assert st.load_saved(p) == {"overdue_hours": 30}
    assert st.load_error() is None


def test_save_and_load_roundtrip(tmp_path):
    p = str(tmp_path / "settings.json")
    st.save({"overdue_hours": 12}, p)
    assert st.load_saved(p) == {"overdue_hours": 12}
    assert st.load_saved(str(tmp_path / "missing.json")) == {}


# ------------------------------------------------------------ HTTP layer --

@pytest.fixture
def client(tmp_path, monkeypatch):
    # These tests exercise the pre-0.2 "open" mode explicitly; test_app.py
    # covers the locked-by-default behaviour.
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SETTINGS_PASSWORD", raising=False)
    monkeypatch.setenv("ALLOW_UNPROTECTED_SETTINGS", "true")
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module._failed.clear()
    return app_module.app.test_client()


def test_settings_roundtrip_via_api(client, tmp_path):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.get_json()["password_required"] is False

    r = client.put("/api/settings", json={"telegram": {"token": TOKEN_A, "chat_id": "42"}, "overdue_hours": 30})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["settings"]["telegram"]["token_set"] is True
    assert TOKEN_A not in json.dumps(body)                     # secret never echoed
    assert body["channels"] == ["telegram"]                    # alerter reconfigured live
    assert json.load(open(tmp_path / "settings.json"))["telegram"]["token"] == TOKEN_A

    r = client.put("/api/settings", json={"overdue_hours": "x"})
    assert r.status_code == 400 and "number" in r.get_json()["error"]


def test_password_guard_via_api(client, monkeypatch):
    monkeypatch.setenv("SETTINGS_PASSWORD", "pw")
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings", json={}).status_code == 401
    assert client.post("/api/alerts/check").status_code == 401
    assert client.get("/api/settings", headers={"X-Settings-Password": "wrong"}).status_code == 401
    assert client.get("/api/settings", headers={"X-Settings-Password": "pw"}).status_code == 200
    assert client.get("/api/alerts").status_code == 200        # non-secret summary stays open


def test_single_channel_test_endpoint_reports_missing_config(client):
    r = client.post("/api/alerts/test", json={"channel": "discord", "settings": {"discord": {"webhook": ""}}})
    assert r.status_code == 200
    assert "required" in r.get_json()["results"]["discord"]
    r = client.post("/api/alerts/test", json={"channel": "pager"})
    assert "Unknown channel" in r.get_json()["results"]["pager"]
