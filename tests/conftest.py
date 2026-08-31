"""Shared fixtures for the demo-readiness suites.

These suites exist because the named test batteries -- final boss, SunSpec 103,
evaluator impersonation, relief valve, own-pack -- lived only as Markdown
reports on a Desktop. Every sweep rebuilt the harness from prose, ran it once,
and threw it away, so a regression between sweeps was invisible: on 2026-08-31
the demo had been failing for five days while the source suite was green.

They run in-process against `app.main:app`, so `pytest tests/` needs no docker
and no network. The one exception is `test_demo_check.py`, which drives the real
container and skips itself when docker is not available.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app                        # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def normalize(client):
    """POST /v1/normalize and return the parsed body.

    Asserts 200 on the way through: every suite here treats a non-200 as a
    failure of the suite, never as a result to be inspected.
    """
    def _norm(oem, data, **kw):
        body = {"oem": oem, "data": data}
        body.update(kw)
        r = client.post("/v1/normalize", json=body)
        assert r.status_code == 200, f"{oem}: HTTP {r.status_code} {r.text[:300]}"
        return r.json()
    return _norm


def canon(out, tag):
    """Canonical field a raw tag resolved to, or None."""
    return ((out.get("field_mappings") or {}).get(tag) or {}).get("canonical_field")


def resolved(out, tag):
    return canon(out, tag) not in (None, "unknown")
