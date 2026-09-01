"""Sandbox test suite.

Two things matter here and neither is "does the code run":

  1. Fidelity. The response SHAPE has to match production, because the promise
     is that you build against the sandbox and change a URL. A key that exists
     here but not there (or vice versa) is the bug that promise fails on.
  2. Honesty. Nothing may report coverage it did not achieve, invent a
     canonical name, or let a simulated prediction pass as a real one.

Run:  python3 -m pytest tests/ -q
"""

import os
import sys

import json
import os

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import corpus, predict, simulate      # noqa: E402
from app.main import app                        # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ── Packs and the canonical dictionary ───────────────────────────────────────

def test_all_five_machines_have_a_pack():
    packs, _, _ = corpus.load()
    for machine, spec in simulate.MACHINES.items():
        assert corpus.get_pack(spec["oem"]) is not None, machine


def test_every_pack_mapping_targets_a_known_canonical():
    """A pack that resolves to a name the dictionary does not carry would make
    the sandbox emit a field nothing downstream can interpret."""
    packs, _, dictionary = corpus.load()
    fields = dictionary["fields"]
    for oem, pack in packs.items():
        unknown = sorted({c for c in pack.mappings.values() if c not in fields})
        assert not unknown, f"{oem} maps to unknown canonicals: {unknown[:10]}"


def test_signal_classifier_only_targets_real_fields():
    # load() raises if this is violated; calling it is the assertion.
    corpus.load()


def test_pwm_scale_is_declared_not_null():
    """Marlin's @: is a 0-127 duty byte. A null unit here is how an agent reads
    95 as "95 percent" when it is really ~75%."""
    _, _, dictionary = corpus.load()
    for field in ("hotend_heater_pwm_output", "heated_bed_heater_pwm_output"):
        assert dictionary["fields"][field]["unit"] == "pwm_0_127"


# ── Resolution ───────────────────────────────────────────────────────────────

def test_exact_corpus_tags_resolve_at_full_confidence():
    data = {"S SPEED (RPM)": 8500, "SP_LOAD_PCT (%)": 84.7, "PART_CNT": 1204}
    norm, mappings, stats, _, _, _, _ = corpus.normalize_row(data, "haas")
    assert norm["spindle_speed_rpm"] == 8500
    assert norm["spindle_load_pct"] == 84.7
    assert norm["part_count"] == 1204
    assert stats["coverage_pct"] == 100.0
    assert all(m["confidence"] == 1.0 and m["match_type"] == "corpus"
               for m in mappings.values())


def test_unit_suffix_variants_fold_to_the_same_mapping():
    for tag in ("S SPEED (RPM)", "s_speed (1/min)", "SRPM [min-1]", "S Speed"):
        norm, _, _, _, _, _, _ = corpus.normalize_row({tag: 8500}, "haas")
        assert "spindle_speed_rpm" in norm, tag


def test_fahrenheit_is_converted_to_celsius():
    norm, _, _, conversions, _, _, _ = corpus.normalize_row(
        {"COOL_TEMP [°F]": 161.8}, "haas")
    assert conversions and conversions[0]["conversion"] == "fahrenheit_to_celsius"
    assert abs(norm["sensor_readings.coolant_temp"] - 72.11) < 0.01


def test_german_sinumerik_tags_resolve():
    """The whole pitch, in one assertion: an agent cannot guess what
    STUECKZAHL means, and does not have to."""
    data = {"STUECKZAHL (pcs)": 842, "Betriebsstunden": 14203.5,
            "SPINDEL_AUSLASTUNG (%)": 63.0, "Kuehlmittel Temp (C)": 22.5}
    norm, _, stats, _, _, _, _ = corpus.normalize_row(data, "siemens")
    assert norm["part_count"] == 842
    assert norm["operating_hours"] == 14203.5
    assert norm["spindle_load_pct"] == 63.0
    assert norm["sensor_readings.coolant_temp"] == 22.5
    assert stats["coverage_pct"] == 100.0


def test_signal_classifier_handles_tags_no_pack_has_seen():
    # These two spellings are in NO pack, which is the point -- the test is
    # about the signal classifier, so it has to use tags the corpus has never
    # seen. It previously used S1Temp/S1Load; those are now real Haas rows and
    # resolve at layer 1, which would have made this assert the opposite of
    # what it means to.
    norm, mappings, _, _, _, _, _ = corpus.normalize_row(
        {"SpindleTemperature": 72.1, "Spindle_Temp_Reading": 84.7}, "haas")
    assert norm["spindle_temperature"] == 72.1
    assert all(m["match_type"] == "signal" for m in mappings.values())
    # A guess must never claim the confidence of a corpus hit.
    assert all(m["confidence"] < 1.0 for m in mappings.values())


def test_common_haas_mdc_tags_resolve_from_the_pack_not_by_inference():
    """S1Temp / S1Load / SP_SPEED are the commonest Haas MDC tags there are.
    Answering them by inference at 0.65 when the vendor's own spelling is
    known is leaving evidence on the table."""
    norm, mappings, _, _, _, _, _ = corpus.normalize_row(
        {"S1Temp": 72.1, "S1Load": 84.7, "SP_SPEED": 8500}, "haas")
    assert norm["spindle_temperature"] == 72.1
    assert norm["spindle_load_pct"] == 84.7
    assert norm["spindle_speed_rpm"] == 8500
    for tag, m in mappings.items():
        assert m["confidence"] == 1.0, (tag, m)


def test_unknown_tags_are_reported_not_invented():
    norm, mappings, stats, _, _, _, _ = corpus.normalize_row(
        {"S SPEED (RPM)": 8500, "ZZZ_PROPRIETARY_BLOB": 42}, "haas")
    assert mappings["ZZZ_PROPRIETARY_BLOB"]["canonical_field"] is None
    assert mappings["ZZZ_PROPRIETARY_BLOB"]["match_type"] == "unknown"
    # Lossless: the value survives under its raw name.
    assert norm["ZZZ_PROPRIETARY_BLOB"] == 42
    assert stats["coverage_pct"] == 50.0


def test_coverage_counts_distinct_canonicals_not_tags():
    """Ten spellings of one quantity is one field covered, not ten. Production
    had this exact bug: it reported 100% on an unseeded corpus."""
    data = {"S SPEED (RPM)": 8500, "SRPM": 8500, "S Speed": 8500,
            "SPINDLE_SPEED_ACT": 8500}
    _, _, stats, _, _, _, _ = corpus.normalize_row(data, "haas")
    assert stats["fields_distinct_canonical"] == 1
    assert stats["coverage_pct"] == 25.0


def test_collisions_are_recorded_and_the_best_mapping_wins():
    data = {"S SPEED (RPM)": 8500, "S1Speed": 9999}
    norm, mappings, _, _, collisions, _, _ = corpus.normalize_row(data, "haas")
    assert "spindle_speed_rpm" in collisions
    # The exact corpus row beats the signal guess.
    assert norm["spindle_speed_rpm"] == 8500
    assert mappings["S1Speed"].get("superseded_by") == "S SPEED (RPM)"


def test_unknown_oem_still_normalizes():
    norm, _, stats, _, _, _, _ = corpus.normalize_row(
        {"spindle_load_pct": 61.0}, "some_vendor_we_never_heard_of")
    assert norm["spindle_load_pct"] == 61.0
    assert stats["layer2_identity"] == 1


# ── Simulators ───────────────────────────────────────────────────────────────

_GAPS = json.load(open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app", "packs", "_registry_gaps.json")))
DOCUMENTED_GAPS = set(_GAPS["all_points"])


@pytest.mark.parametrize("machine", sorted(simulate.MACHINES))
def test_every_simulated_machine_resolves_every_tag_it_can(machine):
    """No simulator may emit a tag that is neither resolvable nor a documented
    registry gap.

    This replaced a flat `coverage_pct >= 80` threshold, which measured the
    wrong thing once the SunSpec devices landed. Their register dumps are 100%
    real, normative point names — AphA, PPVphBC, VA — that the canonical
    registry simply has no field for, so a full model-203 read scores 66% while
    being entirely correct. The threshold would have been satisfied by deleting
    those points from the simulator, which is the opposite of the property
    worth having.

    What actually matters is that every unresolved tag is one we have named and
    justified in point_map.UNMAPPED. A tag outside that list means a simulator
    invented a spelling or a pack lost a row — and that is caught here
    regardless of what the coverage percentage happens to be.
    """
    reading = simulate.reading(machine, seed=1234)
    _, mappings, stats, _, _, _, _ = corpus.normalize_row(
        reading["data"], reading["oem"],
        sunspec_model=reading.get("sunspec_model"))
    unresolved = {t for t, m in mappings.items() if m["match_type"] == "unknown"}
    undocumented = sorted(unresolved - DOCUMENTED_GAPS)
    assert not undocumented, (
        f"{machine}: tags resolved to nothing and are not documented registry "
        f"gaps: {undocumented}")


@pytest.mark.parametrize("machine", sorted(simulate.MACHINES))
def test_every_simulated_machine_has_useful_coverage(machine):
    """Every tag that CAN resolve does resolve.

    Scored as resolved-tags / resolvable-tags, not the kernel's distinct-
    canonicals / total-tags coverage. Both are worth knowing and they answer
    different questions, but only this one is a statement about the pack.

    The distinct-canonical ratio penalises a device for being thorough: a BESS
    advertising SunSpec models 124 and 802 reports state of charge in both
    blocks (ChaState and SoC) and pack voltage in both (InBatV and V), so nine
    perfectly resolved tags collapse to seven canonicals and the device scores
    78% for doing exactly the right thing. The collision machinery already
    records that, and reconciling duplicates is the point of a canonical schema
    rather than a failure of one.
    """
    reading = simulate.reading(machine, seed=1234)
    _, mappings, stats, _, _, _, _ = corpus.normalize_row(
        reading["data"], reading["oem"],
        sunspec_model=reading.get("sunspec_model"))
    resolvable = [t for t in mappings if t not in DOCUMENTED_GAPS]
    resolved = [t for t in resolvable
                if mappings[t]["match_type"] != "unknown"
                and mappings[t]["confidence"] > 0]
    pct = 100.0 * len(resolved) / len(resolvable) if resolvable else 0.0
    assert pct >= 80.0, (
        machine, round(pct, 2),
        sorted(set(resolvable) - set(resolved)), stats)


def test_seed_makes_readings_reproducible():
    a = simulate.reading("haas", seed=99)
    b = simulate.reading("haas", seed=99)
    assert a["data"] == b["data"]


def test_series_is_numeric_and_ordered_oldest_to_newest():
    values = simulate.series("fanuc", "MOTOR_TEMP", points=32, seed=500)
    assert len(values) == 32
    assert all(isinstance(v, float) for v in values)


def test_series_stays_inside_one_shift():
    """A series that wraps past the end of the shift shows the machine cooling
    from 66C back to 28C mid-window, and a forecaster handed that reports a
    confident downward trend on a machine that is actually heating."""
    for seed in (0, 100, 300, 500, 599):
        values = simulate.series("fanuc", "MOTOR_TEMP", points=48, seed=seed)
        # Warm-up is monotone-ish; the giveaway for a wrap is a single-step
        # collapse back toward ambient.
        drops = [a - b for a, b in zip(values, values[1:]) if a - b > 15.0]
        assert not drops, (seed, drops)


def test_series_captures_a_warmup_ramp():
    values = simulate.series("prusa", "hotend_temp", points=48, seed=100)
    assert values[0] < 40.0 and values[-1] > 180.0


def test_series_rejects_non_numeric_fields():
    with pytest.raises(ValueError):
        simulate.series("haas", "PROG NAME", points=20, seed=1)


# ── Prediction ───────────────────────────────────────────────────────────────

def test_breach_response_carries_productions_keys():
    values = [float(60 + i) for i in range(20)]
    out = predict.predict_threshold_breach(values, 95.0)
    for key in ("will_breach", "estimated_steps_to_breach", "confidence",
                "current_value", "threshold", "direction", "forecast_at_breach",
                "breach_window", "point_forecast_summary", "model"):
        assert key in out, key
    assert set(out["breach_window"]) == {"earliest", "latest"}


def test_a_rising_series_breaches_and_a_flat_one_does_not():
    rising = [float(60 + i * 2) for i in range(20)]
    assert predict.predict_threshold_breach(rising, 120.0)["will_breach"]
    flat = [70.0 + (i % 2) * 0.05 for i in range(20)]
    assert not predict.predict_threshold_breach(flat, 120.0)["will_breach"]


def test_direction_below_works():
    falling = [float(100 - i * 3) for i in range(20)]
    out = predict.predict_threshold_breach(falling, 20.0, direction="below")
    assert out["will_breach"] and out["estimated_steps_to_breach"] is not None


def test_breach_window_brackets_the_point_estimate():
    values = [float(60 + i) for i in range(24)]
    out = predict.predict_threshold_breach(values, 110.0)
    steps = out["estimated_steps_to_breach"]
    window = out["breach_window"]
    assert window["earliest"] is not None and window["earliest"] <= steps
    if window["latest"] is not None:
        assert window["latest"] >= steps


def test_predictions_are_flagged_simulated():
    out = predict.predict_threshold_breach([float(i) for i in range(20)], 50.0)
    assert out["simulated"] is True
    assert out["model"] == "sandbox-ols-v1"
    assert "timesfm" not in out["model"].lower()


def test_unscorable_machines_never_count_as_healthy():
    """A machine that could not be scored masquerading as healthy is the worst
    failure a fleet view can have."""
    out = predict.fleet_health([
        {"id": "ok", "canonical_field": "spindle_load_pct",
         "values": [float(50 + i) for i in range(20)], "threshold": 95.0},
        {"id": "too-short", "canonical_field": "spindle_load_pct",
         "values": [1.0, 2.0], "threshold": 95.0},
        {"id": "no-threshold", "canonical_field": "spindle_load_pct",
         "values": [float(i) for i in range(20)]},
    ])
    summary = out["fleet_health"]
    assert summary["unscored"] == 2
    assert summary["coverage_complete"] is False
    assert summary["score_basis"] == "scored_machines_only"
    assert out["risk_distribution"]["healthy"] + \
           sum(v for k, v in out["risk_distribution"].items() if k != "healthy") == 1
    assert "could NOT be scored" in out["recommendation"]


def test_data_hash_is_deterministic():
    payload = {"time_series": [1.0, 2.0], "threshold": 5.0}
    assert predict.data_hash(payload) == predict.data_hash(dict(payload))
    assert predict.data_hash(payload).startswith("sha256:")


# ── HTTP surface ─────────────────────────────────────────────────────────────

def test_health_is_200_on_get_and_head(client):
    assert client.get("/health").status_code == 200
    assert client.head("/health").status_code == 200


def test_health_declares_what_is_missing(client):
    body = client.get("/health").json()
    assert body["simulated"] is True
    assert body["mode"] == "sandbox"
    joined = " ".join(body["not_included"]).lower()
    assert "timesfm" in joined and "llm" in joined and "corpus" in joined


def test_every_response_is_stamped_as_sandbox(client):
    resp = client.get("/health")
    assert resp.headers["X-Forge-Sandbox"] == "true"


def test_normalize_needs_no_auth_header(client):
    resp = client.post("/v1/normalize", json={
        "oem": "haas", "data": {"S SPEED (RPM)": 8500}})
    assert resp.status_code == 200
    assert resp.json()["coverage_pct"] == 100.0


def test_normalize_response_carries_productions_envelope(client):
    resp = client.post("/v1/normalize", json={
        "oem": "haas", "data": {"S SPEED (RPM)": 8500, "SP_LOAD_PCT (%)": 84.7}})
    body = resp.json()
    for key in ("normalized", "field_mappings", "fields_total", "fields_mapped",
                "fields_unknown", "fields_distinct_canonical", "coverage_pct",
                "collisions", "normalization_layers", "unit_conversions",
                "machine_id", "rows", "observed_at", "ingested_at",
                "history_id", "triggers_fired", "guardrails_triggered",
                "timestamp"):
        assert key in body, key


def test_normalize_accepts_csv(client):
    csv_body = "S SPEED (RPM),SP_LOAD_PCT (%)\n8500,84.7\n8600,86.1\n"
    resp = client.post("/v1/normalize", content=csv_body,
                       headers={"Content-Type": "text/csv", "X-OEM": "haas"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["rows"] == 2
    assert body["normalized"][0]["spindle_speed_rpm"] == 8500
    assert body["normalized"][1]["spindle_load_pct"] == 86.1


def test_normalize_rejects_an_empty_payload(client):
    assert client.post("/v1/normalize", json={"data": {}}).status_code == 422


def test_predict_breach_is_stateless_and_says_so(client):
    """{machine_id, field, threshold} with no series is a 422 in production.
    It must be a 422 here too, or you find out after you deploy."""
    resp = client.post("/v1/predict_breach", json={
        "machine_id": "SBX-HAAS-VF2SS-01",
        "canonical_field": "spindle_load_pct", "threshold": 95.0})
    assert resp.status_code == 422


def test_predict_breach_end_to_end(client):
    values = [float(60 + i * 1.5) for i in range(24)]
    resp = client.post("/v1/predict_breach", json={
        "time_series": values, "threshold": 95.0,
        "canonical_field": "spindle_load_pct"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["will_breach"] is True
    assert body["attestation"]["settled"] is False
    assert body["billing"]["charged_usd"] == 0.0


def test_predict_breach_warns_on_a_field_production_would_reject(client):
    resp = client.post("/v1/predict_breach", json={
        "time_series": [float(i) for i in range(20)], "threshold": 100.0,
        "canonical_field": "totally_made_up_field"})
    assert "field_warnings" in resp.json()


def test_predict_breach_warns_on_a_short_series(client):
    resp = client.post("/v1/predict_breach", json={
        "time_series": [1.0, 2.0, 3.0], "threshold": 100.0})
    warnings = " ".join(resp.json()["field_warnings"])
    assert "16" in warnings


def test_fleet_health_end_to_end(client):
    machines = [
        {"id": f"SBX-{i}", "canonical_field": "spindle_load_pct",
         "values": [float(50 + i * 2 + j) for j in range(20)], "threshold": 95.0}
        for i in range(3)]
    resp = client.post("/v1/fleet_health", json={"machines": machines})
    body = resp.json()
    assert resp.status_code == 200
    for key in ("fleet_health", "risk_distribution", "field_analysis",
                "ranked_machines", "maintenance_queue", "recommendation"):
        assert key in body, key
    assert body["fleet_health"]["coverage_complete"] is True


def test_fleet_health_rejects_an_empty_batch(client):
    assert client.post("/v1/fleet_health",
                       json={"machines": []}).status_code == 422


def test_fleet_health_rejects_an_oversized_batch(client):
    machines = [{"id": str(i), "values": [1.0] * 20, "threshold": 5.0}
                for i in range(101)]
    assert client.post("/v1/fleet_health",
                       json={"machines": machines}).status_code == 400


def test_simulate_then_normalize_is_a_closed_loop(client):
    """The advertised first five minutes: pull a reading, normalize it, get
    canonical fields back."""
    for machine in sorted(simulate.MACHINES):
        raw = client.get(f"/v1/simulate/{machine}?seed=7").json()
        payload = {"oem": raw["oem"], "data": raw["data"]}
        if raw.get("sunspec_model"):
            payload["sunspec_model"] = raw["sunspec_model"]
        resp = client.post("/v1/normalize", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["oem_recognized"] is True
        undocumented = sorted(set(body["unresolved_tags"] or []) - DOCUMENTED_GAPS)
        assert not undocumented, (machine, undocumented)


def test_simulate_series_then_predict_is_a_closed_loop(client):
    series = client.get(
        "/v1/simulate/fanuc/series?field=MOTOR_TEMP&points=32&seed=500").json()
    assert series["canonical_field"] == "motor_temperature"
    resp = client.post("/v1/predict_breach", json={
        "time_series": series["values"], "threshold": 75.0,
        "canonical_field": series["canonical_field"]})
    assert resp.status_code == 200
    assert "will_breach" in resp.json()


def test_unknown_machine_is_404_with_the_valid_list(client):
    body = client.get("/v1/simulate/kuka").json()
    assert "known" in body["detail"] and "haas" in body["detail"]["known"]


def test_unknown_series_field_lists_what_is_available(client):
    body = client.get("/v1/simulate/haas/series?field=NOPE").json()
    assert "available" in body["detail"]


def test_coverage_reports_the_oems(client):
    body = client.get("/v1/coverage").json()
    assert set(body["oems"]) == set(simulate.MACHINES)
    assert body["total_mappings"] > 1000


def test_coverage_is_honest_about_an_unknown_oem(client):
    body = client.get("/v1/coverage?oem=kuka").json()
    assert body["recognized"] is False
    assert "known_oems" in body


def test_stateful_endpoints_501_with_a_reason(client):
    resp = client.get("/v1/history")
    assert resp.status_code == 501
    body = resp.json()
    assert body["error"] == "not_in_sandbox"
    assert body["available_in_production"] is True


def test_unknown_v1_endpoint_lists_the_real_ones(client):
    body = client.get("/v1/nonsense").json()
    assert "/v1/normalize" in body["available"]


def test_health_declares_it_is_a_sandbox_subset(client):
    """A prospect's first call is /health. The relationship to the licensed
    engine has to be readable there -- the field counts differ, and someone
    comparing 467 against the sell sheet's 694 must find the reason in the
    response, not conclude the image is short."""
    h = client.get("/health").json()
    assert h["sandbox"] is True
    assert "Evaluation subset" in h["sandbox_note"]
    assert "694" in h["sandbox_note"]


def test_get_on_normalize_is_405_not_404(client):
    """A bare `curl localhost:8000/v1/normalize` sends GET. It used to be
    answered "unknown_endpoint" -- naming, as unknown, the endpoint listed as
    available in the same body. Anyone evaluating the image reads that as
    broken, so the wrong method must never again look like a missing route."""
    resp = client.get("/v1/normalize")
    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST"
    body = resp.json()
    assert body["error"] == "method_not_allowed"
    assert body["allowed"] == ["POST"]
    assert "curl -X POST" in body["hint"]
    # POST is unaffected.
    ok = client.post("/v1/normalize",
                     json={"oem": "haas", "data": {"S1Temp": 72.1}})
    assert ok.status_code == 200
    assert ok.json()["normalized"]["spindle_temperature"] == 72.1


def test_openapi_document_builds(client):
    spec = client.get("/openapi.json").json()
    assert "/v1/normalize" in spec["paths"]
    assert "/v1/predict_breach" in spec["paths"]


# ── MCP ──────────────────────────────────────────────────────────────────────

def test_mcp_answers_on_slash_mcp_without_a_redirect(client):
    """The README, every client config, and `claude mcp add` all use /mcp with
    no trailing slash. A 307 there breaks clients that will not follow a
    redirect on POST."""
    resp = client.post(
        "/mcp",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}},
        follow_redirects=False)
    assert resp.status_code == 200, resp.status_code
    assert "mcp-session-id" in resp.headers
    assert "foundrynet-sandbox" in resp.text


def test_mcp_is_mounted(client):
    from app.main import MCP_APP, MCP_ERROR
    assert MCP_APP is not None, f"MCP failed to mount: {MCP_ERROR}"


@pytest.mark.asyncio
async def test_mcp_exposes_the_expected_tools():
    from app.mcp_tools import mcp
    names = {t.name for t in await mcp.list_tools()}
    assert {"normalize_telemetry", "get_coverage", "predict_breach",
            "fleet_health", "predict_batch", "list_sandbox_machines",
            "get_sandbox_reading", "get_sandbox_series"} <= names


@pytest.mark.asyncio
async def test_mcp_tool_descriptions_mention_the_sandbox():
    """An agent reading the tool list must be told the data is simulated.

    This also guards the failure that shipped once already: a description built
    as `\"""doc\""" + CONST` is not a docstring at all, so the tool arrives with
    an empty description and the agent has nothing to reason about.
    """
    from app.mcp_tools import mcp
    for tool in await mcp.list_tools():
        description = tool.description or ""
        assert "SANDBOX" in description, tool.name
        # Production's five carry substantial guidance; a stub would pass the
        # check above on the appended note alone.
        assert len(description) > 400, (tool.name, len(description))
