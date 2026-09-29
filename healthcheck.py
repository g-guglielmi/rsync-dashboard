#!/usr/bin/env python3
# Rsync Watch — read-only dashboard + alerting for unRAID rsync backups.
# Copyright (C) 2026 g-guglielmi
# Licensed under the GNU Affero General Public License v3.0; see LICENSE.

"""Docker HEALTHCHECK for the dashboard.

Exits 0 when the app answers /healthz with {"status": "ok"}, 1 otherwise,
printing a one-line reason (Docker keeps the last few in `docker inspect`).
Also handy by hand:

    docker exec rsync-dashboard /app/healthcheck.py

HEALTHCHECK_URL and HEALTHCHECK_TIMEOUT (seconds) override the defaults.
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("HEALTHCHECK_URL", "http://127.0.0.1:8686/healthz")
TIMEOUT = float(os.environ.get("HEALTHCHECK_TIMEOUT", "4") or 4)


def check(url=URL, timeout=TIMEOUT):
    """Returns (healthy: bool, reason: str)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # connection refused, timeout, bad JSON, ...
        return False, f"unhealthy: {url} unreachable ({type(e).__name__}: {e})"
    if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
        return True, f"ok: {url} answered"
    return False, f"unhealthy: {url} returned HTTP {status} {body}"


def main():
    healthy, reason = check()
    print(reason)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
