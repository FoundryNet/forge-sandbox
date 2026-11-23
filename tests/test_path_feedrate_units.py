"""PATH_FEEDRATE is MILLIMETER/SECOND (MTConnect), converted into the mm/min field.

2026-11-23 cross-vertical run: the same Haas `path_feedrate = 32.5` came back as
32.5 from this sandbox and 1950.0 from the forge-prod kernel. The kernel is right:
the MTConnect standard fixes PathFeedrate at mm/s whatever the control's G20/G21
mode (forge-prod haas_ngc pack, docs/decisions/2026-11-18-tier-test-corpus-fixes.md).
The sandbox had no Haas row, borrowed the mtconnect row cross-OEM, and that row
declared mm/min.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _norm(data, oem):
    return client.post("/v1/normalize", json={"data": data, "oem": oem}).json()


@pytest.mark.parametrize("tag", ["path_feedrate", "PathFeedrate"])
def test_haas_path_feedrate_matches_the_kernel(tag):
    r = _norm({tag: 32.5}, "haas")
    assert r["field_mappings"][tag]["canonical_field"] == "cnc.path.feedrate"   # same field as forge-prod
    assert r["normalized"]["cnc.path.feedrate"] == pytest.approx(1950.0)
    conv = r["unit_conversions"][0]
    assert (conv["from"], conv["to"]) == ("mm/s", "mm/min")


def test_mtconnect_path_feedrate_is_mm_per_second():
    r = _norm({"PATH_FEEDRATE": 32.5}, "mtconnect")
    assert r["normalized"]["feed_rate_actual"] == pytest.approx(1950.0)
