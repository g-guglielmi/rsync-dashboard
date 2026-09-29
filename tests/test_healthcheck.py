import io
import json

import healthcheck


class FakeResp(io.BytesIO):
    def __init__(self, status, payload):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def test_healthy(monkeypatch):
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda url, timeout: FakeResp(200, {"status": "ok"}))
    ok, reason = healthcheck.check("http://x/healthz", 1)
    assert ok and reason.startswith("ok")


def test_bad_status_and_unreachable(monkeypatch):
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda url, timeout: FakeResp(200, {"status": "degraded"}))
    ok, reason = healthcheck.check("http://x/healthz", 1)
    assert not ok and "degraded" in reason

    def boom(url, timeout): raise ConnectionRefusedError("refused")
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", boom)
    ok, reason = healthcheck.check("http://x/healthz", 1)
    assert not ok and "unreachable" in reason and "refused" in reason


def test_main_exit_codes(monkeypatch):
    monkeypatch.setattr(healthcheck, "check", lambda: (True, "ok"))
    assert healthcheck.main() == 0
    monkeypatch.setattr(healthcheck, "check", lambda: (False, "nope"))
    assert healthcheck.main() == 1
