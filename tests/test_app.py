"""HTTP-layer hardening: locked-by-default settings, throttling, headers,
request limits, trusted hosts. Uses the real Flask app with a temp state dir."""
import pytest

PW = "correct-horse-battery-staple"


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SETTINGS_PASSWORD", PW)
    monkeypatch.delenv("ALLOW_UNPROTECTED_SETTINGS", raising=False)
    import app as m
    m.app.config["TESTING"] = True
    m._failed.clear()
    yield m
    m._failed.clear()


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def auth(pw=PW):
    return {"X-Settings-Password": pw}


# ---------------------------------------------------------------- locking --

def test_settings_are_read_only_without_a_password(client, monkeypatch):
    monkeypatch.delenv("SETTINGS_PASSWORD")
    r = client.get("/api/settings")
    assert r.status_code == 403
    assert r.get_json()["setup_required"] is True
    assert client.put("/api/settings", json={"overdue_hours": 1}).status_code == 403
    assert client.post("/api/alerts/test", json={"channel": "discord"}).status_code == 403
    # viewing is unaffected
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/alerts").status_code == 200
    assert client.get("/").status_code == 200


def test_password_flow(client):
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers=auth("wrong")).status_code == 401
    assert client.get("/api/settings", headers=auth()).status_code == 200


def test_non_ascii_header_is_a_401_not_a_500(client):
    assert client.get("/api/settings", headers=auth("perché")).status_code == 401


def test_wrong_passwords_are_throttled(client, app_module):
    for _ in range(app_module.MAX_FAILED_PASSWORDS):
        assert client.get("/api/settings", headers=auth("nope")).status_code == 401
    r = client.get("/api/settings", headers=auth())          # even the right one
    assert r.status_code == 429
    assert "15 minutes" in r.get_json()["error"]
    app_module._failed.clear()
    assert client.get("/api/settings", headers=auth()).status_code == 200


def test_side_effect_endpoints_are_post_only(client):
    assert client.get("/api/alerts/test").status_code == 405
    assert client.get("/api/alerts/check").status_code == 405
    assert client.get("/api/alerts/test", headers=auth()).status_code == 405


# ---------------------------------------------------------------- limits --

def test_oversized_body_is_rejected(client):
    r = client.put("/api/settings", data="x" * (70 * 1024), content_type="application/json", headers=auth())
    assert r.status_code == 413
    assert r.get_json()["error"]


def test_malformed_types_are_400(client):
    for payload in ({"job_intervals": 5}, {"telegram": "abc"}, {"events": 5}, {"discord": {"webhook": "http://x"}}):
        r = client.put("/api/settings", json=payload, headers=auth())
        assert r.status_code == 400, payload
        assert r.get_json()["error"]


# --------------------------------------------------------------- headers --

def test_security_headers(client):
    r = client.get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "Cache-Control" not in r.headers or "no-store" not in r.headers["Cache-Control"]
    api = client.get("/api/dashboard")
    assert api.headers["Cache-Control"] == "no-store"


def test_footer_shows_source_link_and_version(client, app_module):
    html = client.get("/").get_data(as_text=True)
    assert f"v{app_module.APP_VERSION}" in html
    assert "github.com/g-guglielmi/rsync-dashboard" in html


# --------------------------------------------------------- trusted hosts --

def test_trusted_hosts_reject_unknown_host_header(client, app_module):
    app_module.app.config["TRUSTED_HOSTS"] = ["dash.lan", "127.0.0.1", "localhost"]
    try:
        assert client.get("/healthz", base_url="http://dash.lan").status_code == 200
        assert client.get("/healthz", base_url="http://localhost").status_code == 200   # healthcheck
        assert client.get("/healthz", base_url="http://evil.example").status_code == 400
    finally:
        app_module.app.config["TRUSTED_HOSTS"] = None


def test_missing_header_does_not_count_toward_throttle(client, app_module):
    # Opening the panel before entering a password always yields one 401;
    # that must never lock a user out.
    for _ in range(app_module.MAX_FAILED_PASSWORDS + 2):
        assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers=auth()).status_code == 200
