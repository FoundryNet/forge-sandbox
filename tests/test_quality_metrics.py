"""The quality half of the satellite heartbeat.

The counters existed and were wired into the heartbeat, but nothing tested
them, which is the same failure mode the rest of this directory was created to
fix: a metric that silently stops incrementing looks exactly like a fleet with
nothing to report. Every assertion here drives real traffic through
/v1/normalize and reads the counters back, so a regression in the pipeline
shows up as a dead metric rather than as a quiet zero.
"""

import pytest

from app.satellite import COUNTERS


@pytest.fixture
def counters():
    """Drain the shared counters so each test measures only its own traffic.

    snapshot() is the reset -- the same call the heartbeat makes -- so this
    also exercises the drain path the daemon relies on.
    """
    COUNTERS.snapshot()
    yield COUNTERS
    COUNTERS.snapshot()


# ── the four metrics the dashboard asked for ─────────────────────────────────

def test_evidence_gate_refusal_is_counted_and_explained(counters, normalize):
    """A refusal is a product event, not an error: the reason has to survive."""
    normalize("j1939", {"DPF_Status": "ACTIVE"})
    q = counters.quality_snapshot()

    assert q["evidence_refusals"] == 1
    assert q["confidence_distribution"].get("refused") == 1
    reason = q["evidence_refusal_reasons"][0]
    assert reason["tag"] == "DPF_Status"
    # The candidate it REFUSED is the useful half -- it names the wrong answer
    # the gate stopped, which is what makes a refusal auditable.
    assert reason["candidate"] == "execution_state"


def test_physics_violation_is_counted_against_its_field(counters, normalize):
    out = normalize("generic_iot", {"power_consumption_kw": 999_999_999})

    assert out["normalized"]["power_consumption_kw"] is None
    q = counters.quality_snapshot()
    assert q["physics_violations"] == 1
    assert q["physics_violation_fields"]["power_consumption_kw"] == 1


def test_sentinel_catch_is_counted(counters, normalize):
    normalize("generic_iot", {"temperature_c": 32767})
    assert counters.quality_snapshot()["sentinel_catches"] == 1


def test_confidence_distribution_separates_resolved_from_unresolved(
        counters, normalize):
    normalize("fanuc", {"SPINDLE SPEED": 1200, "NO_SUCH_TAG_XYZZY": 5})
    q = counters.quality_snapshot()

    assert q["confidence_distribution"].get("1.0") == 1
    assert q["confidence_distribution"].get("unresolved") == 1


def test_coverage_is_reported_per_oem_as_raw_counts(counters, normalize):
    """Raw mapped/total, not just a percentage.

    Averaging percentages across engines is arithmetically wrong -- two engines
    at 100% and 50% are not 75% unless they saw the same number of fields -- so
    the control plane needs the numerator and denominator.
    """
    normalize("fanuc", {"SPINDLE SPEED": 1200, "NO_SUCH_TAG_XYZZY": 5})
    normalize("generic_iot", {"power_consumption_kw": 12.5})
    q = counters.quality_snapshot()

    assert q["coverage_by_oem"]["fanuc"] == {"mapped": 1, "total": 2, "pct": 50.0}
    assert q["coverage_by_oem"]["generic_iot"] == {
        "mapped": 1, "total": 1, "pct": 100.0}
    assert q["coverage_pct"] == pytest.approx(66.67, abs=0.01)
    assert set(q["oems_seen"]) == {"fanuc", "generic_iot"}


# ── the endpoint ─────────────────────────────────────────────────────────────

def test_quality_endpoint_does_not_drain_the_counters(counters, client,
                                                      normalize):
    """Monitoring must not destroy what it measures.

    /v1/quality calls quality_snapshot(), never snapshot(). If it ever calls
    snapshot(), polling the dashboard would consume the metrics before the
    heartbeat could report them, and the control plane would go blind in
    proportion to how closely the operator was watching.
    """
    normalize("j1939", {"DPF_Status": "ACTIVE"})

    first = client.get("/v1/quality")
    assert first.status_code == 200
    assert first.json()["quality"]["evidence_refusals"] == 1
    assert first.json()["resets_on_read"] is False

    second = client.get("/v1/quality")
    assert second.json()["quality"]["evidence_refusals"] == 1


# ── the failed-beat path ─────────────────────────────────────────────────────

def test_restore_gives_quality_signal_back_after_a_failed_beat(counters,
                                                               normalize):
    """A dropped beat must not erase the quality signal.

    The blind spot this closes is the worst-timed one available: heartbeats
    fail when the fleet is unhealthy, which is exactly when the refusal and
    violation counts matter most.
    """
    normalize("j1939", {"DPF_Status": "ACTIVE"})
    normalize("generic_iot", {"power_consumption_kw": 999_999_999})

    beat = counters.snapshot()                    # the beat leaves the engine
    assert counters.quality_snapshot()["evidence_refusals"] == 0   # drained

    counters.restore(beat)                        # ...and the POST failed
    back = counters.quality_snapshot()
    assert back["evidence_refusals"] == 1
    assert back["physics_violations"] == 1
    assert back["physics_violation_fields"]["power_consumption_kw"] == 1
    assert back["coverage_by_oem"]["generic_iot"]["total"] == 1
