"""Evidence gate + pack-conversion regression tests."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import corpus
from app.evidence_gate import score_resolution, apply_threshold
from scripts.audit_pack_conversions import find_double_conversions


def _map(tag, oem):
    _, fm, _, _, _, _, _ = corpus.normalize_row({tag: 1}, oem)
    return fm[tag]


# ── P0-1: OPC UA node-ID syntax is not vocabulary ────────────────────────────
def test_opc_node_id_does_not_collapse_temperatures_onto_spindle():
    leaves = {
        "CoolantTemp": "sensor_readings.coolant_temp",
        "OilTemp": "oil_temperature",
        "BearingTemp": "bearing_temperature",
        "MotorTemp": "motor_temperature",
        "AmbientTemp": "ambient_temperature",
    }
    got = {}
    for leaf, expected in leaves.items():
        rec = _map(f"ns=2;s=Channel1.Device1.{leaf}", "haas")
        assert rec["canonical_field"] == expected, (leaf, rec)
        got[leaf] = rec["canonical_field"]
    # Five distinct measurements must stay five distinct fields.
    assert len(set(got.values())) == 5, got


def test_bare_leaf_and_opc_wrapped_leaf_agree():
    for leaf in ("CoolantTemp", "OilTemp", "BearingTemp", "MotorTemp"):
        bare = _map(leaf, "haas")["canonical_field"]
        wrapped = _map(f"ns=2;s=Channel1.Device1.{leaf}", "haas")["canonical_field"]
        assert bare == wrapped, (leaf, bare, wrapped)


def test_haas_numbered_spindle_still_resolves():
    assert _map("S1Temp", "haas")["canonical_field"] == "spindle_temperature"
    assert _map("S2Load", "haas")["canonical_field"] == "spindle_load_pct"
    assert _map("SP_SPEED", "haas")["canonical_field"] == "spindle_speed_rpm"


# ── P0-2: scale factor is the conversion; the pack must not re-apply one ─────
def test_power_factor_is_not_double_scaled():
    for raw, sf, want in ((9730, -4, 0.973), (9950, -4, 0.995), (10000, -4, 1.0)):
        norm, _, _, _, _, _, _ = corpus.normalize_row(
            {"PF": raw, "PF_SF": sf}, "sunspec", sunspec_model=103)
        assert abs(norm["power_factor"] - want) < 1e-9, (raw, sf, norm)


def test_no_pack_declares_a_unit_a_scale_factor_already_resolved():
    findings = find_double_conversions()
    assert findings == [], findings


# ── P0-3: an unrecognised OEM degrades, it does not disable ─────────────────
def test_unknown_oem_still_resolves_at_lower_confidence():
    known = _map("SpindleSpeed", "haas")
    assert known["canonical_field"] == "spindle_speed_rpm"
    for oem in ("kepware", "unknown_vendor", "", None):
        rec = _map("SpindleSpeed", oem)
        assert rec["canonical_field"] == "spindle_speed_rpm", (oem, rec)
        assert rec["confidence"] <= known["confidence"], (oem, rec)


# ── P1-4 / P1-5 ─────────────────────────────────────────────────────────────
def test_fanuc_is_cnc_and_focas_tags_resolve():
    assert corpus.domain_from_oem("fanuc") == "cnc"
    assert corpus.get_pack("fanuc").vertical == "cnc"
    for tag, want in (("ABSOLUTE_X", "axes.x_position_actual"),
                      ("ACTUAL_FEEDRATE", "feed_rate_actual"),
                      ("PARTS_MADE", "part_count"),
                      ("SERVO_LOAD_1", "axes.x_load_pct")):
        assert _map(tag, "fanuc")["canonical_field"] == want, tag


def test_diesel_particulate_status_is_not_a_machine_execution_state():
    rec = _map("DPF_Status", "j1939")
    assert rec["canonical_field"] is None, rec
    assert rec["match_type"] == "insufficient_evidence", rec


def test_s7_connection_status_is_not_a_machine_execution_state():
    rec = _map("S7_Connection_1.PLC_1.Status", "siemens")
    assert rec["canonical_field"] is None, rec


# ── The gate itself ─────────────────────────────────────────────────────────
def test_exact_corpus_bypasses_the_gate_at_full_confidence():
    ev = score_resolution("W", "inverter_output_kw", "exact_corpus", {},
                          "energy", {"unit": "kW"}, "energy")
    assert ev["resolve"] and ev["confidence"] == 1.0
    assert ev["evidence_class"] == "exact"


def test_domain_and_physics_conflict_sinks_a_plausible_match():
    ev = score_resolution(
        "Flow_GPM", "filament_flow_rate", "pack_match",
        {"keyword_overlap": {"flow"}, "tag_unit": "GPM",
         "pint_compatible": False, "matched_oem": "prusa"},
        "industrial", {"unit": "mm3/s"}, "3dp")
    assert not ev["resolve"], ev
    assert ev["confidence"] == 0.0


def test_thresholds_are_monotonic():
    from app.evidence_gate import CONFIDENCE_THRESHOLDS as T
    assert apply_threshold(T["resolve_full"])[1] == 1.0
    assert apply_threshold(T["resolve_high"])[1] == 0.85
    assert apply_threshold(T["resolve_medium"])[1] == 0.65
    assert apply_threshold(T["resolve_medium"] - 1)[0] is False
    # Monotonic: more evidence never buys less confidence.
    conf = [apply_threshold(n)[1] for n in range(-50, 101)]
    assert conf == sorted(conf)


def test_a_refused_match_reports_what_it_would_have_matched():
    rec = _map("DPF_Status", "j1939")
    ev = rec.get("evidence") or {}
    assert ev.get("refused_candidate"), rec
    assert ev.get("signals"), rec
    assert "insufficient_evidence" in rec["match_type"]
