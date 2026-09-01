"""ISA-95 field categorisation, and the opt-in `field_context` enrichment.

Two properties carry most of the weight here:

  * Every canonical field has exactly one category, and the two canonical-field
    files agree about it. They are separate files with different field counts
    and they have drifted before; a category assigned in one and missing from
    the other is the same class of bug wearing a new hat.

  * `field_context` is OPT-IN. An integration that does not ask for it must get
    a byte-identical response to the one it got before the feature existed.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.main import app

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"
REGISTRY = APP_DIR / "canonical_fields.json"
SERVING = APP_DIR / "packs" / "_canonical_fields.json"

VALID = {
    "equipment_performance", "equipment_state", "production_performance",
    "energy_consumption", "electrical_measurement", "equipment_condition",
    "environmental", "storage", "safety", "production_quality", "general",
}


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _fields(path):
    return json.loads(path.read_text())["fields"]


# ---------------------------------------------------------------------------
# Task 1 — ISA-95 categorisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [REGISTRY, SERVING], ids=["registry", "serving"])
def test_every_field_has_exactly_one_valid_category(path):
    fields = _fields(path)
    missing = [n for n, s in fields.items() if "isa95_category" not in s]
    assert not missing, f"{len(missing)} field(s) with no category: {missing[:10]}"
    bad = {n: s["isa95_category"] for n, s in fields.items()
           if s["isa95_category"] not in VALID}
    assert not bad, f"invalid categories: {bad}"


def test_the_two_field_files_agree_on_every_shared_field():
    """They are separate files that have drifted before. A category present in
    one and different in the other is a silent contradiction."""
    reg, srv = _fields(REGISTRY), _fields(SERVING)
    shared = set(reg) & set(srv)
    assert shared, "the two files share no fields at all — something is wrong"
    disagree = {n: (reg[n]["isa95_category"], srv[n]["isa95_category"])
                for n in shared
                if reg[n]["isa95_category"] != srv[n]["isa95_category"]}
    assert not disagree, f"{len(disagree)} disagreement(s): {list(disagree.items())[:5]}"


def test_the_stated_tie_breaks_hold():
    """The two judgement calls the specification made by name. If a later rule
    change flips one of these, it should fail here and not in a customer's UNS."""
    srv = _fields(SERVING)
    # health, not output
    assert srv["motor_temperature"]["isa95_category"] == "equipment_condition"
    # a power-quality metric, not a raw electrical reading
    assert srv["power_factor"]["isa95_category"] == "energy_consumption"


@pytest.mark.parametrize("field,category", [
    ("spindle_speed_rpm", "equipment_performance"),
    ("spindle_temperature", "equipment_condition"),   # temperature beats spindle_
    ("ambient_temperature_c", "environmental"),       # environmental beats temperature
    ("battery_voltage_v", "storage"),                 # storage beats voltage
    ("dc_voltage_v", "electrical_measurement"),
    ("energy_kwh", "energy_consumption"),
    ("part_count", "production_performance"),
    ("estop_state", "safety"),                        # safety beats _state
    ("cutting_time_hours", "equipment_state"),
    ("vibration_mm_s", "equipment_condition"),
])
def test_ordering_sensitive_assignments(field, category):
    srv = _fields(SERVING)
    if field not in srv:
        pytest.skip(f"{field} not in this build's schema")
    assert srv[field]["isa95_category"] == category


def test_categorisation_is_metadata_only(client):
    """No pipeline behaviour may change. Same payload, same answers."""
    body = {"data": {"spindle_speed": 12000, "S1Temp": 42.5}, "oem": "haas"}
    r = client.post("/v1/normalize", json=body)
    assert r.status_code == 200
    b = r.json()
    assert b["normalized"]["spindle_speed_rpm"] == 12000
    assert b["field_mappings"]["S1Temp"]["canonical_field"] == "spindle_temperature"
    assert b["coverage_pct"] == 100.0


# ---------------------------------------------------------------------------
# Task 2 — field_context
# ---------------------------------------------------------------------------
BODY = {"data": {"spindle_speed": 12000, "S1Temp": 42.5, "NoSuchTag": 1},
        "oem": "haas", "machine_id": "CNC-01"}


def test_field_context_is_absent_by_default(client):
    assert "field_context" not in client.post("/v1/normalize", json=BODY).json()
    assert "field_context" not in client.post(
        "/v1/normalize?include_context=false", json=BODY).json()


def test_field_context_adds_nothing_else_to_the_response(client):
    """Backwards compatibility, stated as a test: the flag may add one key and
    change no other."""
    plain = client.post("/v1/normalize", json=BODY).json()
    rich = client.post("/v1/normalize?include_context=true", json=BODY).json()
    assert set(rich) - set(plain) == {"field_context"}
    volatile = {"timestamp", "ingested_at", "observed_at"}
    for k in set(plain) - volatile:
        assert json.dumps(plain[k], sort_keys=True, default=str) == \
               json.dumps(rich[k], sort_keys=True, default=str), f"{k} changed"


def test_field_context_carries_all_five_declared_keys(client):
    ctx = client.post("/v1/normalize?include_context=true",
                      json=BODY).json()["field_context"]
    spec = ctx["spindle_temperature"]
    assert spec["unit"] == "C"
    assert spec["physical_quantity"] == "temperature"
    assert spec["isa95_category"] == "equipment_condition"
    assert spec["confidence"] == 1.0
    assert spec["match_type"] == "corpus"
    assert spec["source_tag"] == "S1Temp"


def test_field_context_covers_every_emitted_field_including_passthroughs(client):
    """An unresolved tag still appears in `normalized`, so it must appear in
    the context too -- reported as unknown, not omitted and not nulled."""
    b = client.post("/v1/normalize?include_context=true", json=BODY).json()
    assert set(b["field_context"]) == set(b["normalized"])
    passthrough = b["field_context"]["NoSuchTag"]
    assert passthrough["match_type"] == "unknown"
    assert passthrough["confidence"] == 0.0
    assert passthrough["isa95_category"] is None


def test_field_context_is_true_only_for_explicit_truthy_values(client):
    for value in ("true", "1", "yes", "on", "TRUE"):
        r = client.post(f"/v1/normalize?include_context={value}", json=BODY)
        assert "field_context" in r.json(), value
    for value in ("false", "0", "no", "", "maybe"):
        r = client.post(f"/v1/normalize?include_context={value}", json=BODY)
        assert "field_context" not in r.json(), value
