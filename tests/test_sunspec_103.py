"""SunSpec Model 103 -- every register, every scale factor.

Model 103 is a three-phase inverter. Its scale factors are SHARED: one `A_SF`
governs A, AphA, AphB and AphC; one `V_SF` governs six voltage points; one
`Tmp_SF` governs four temperatures. That sharing is only visible in the
published model definition, which is why `sunspec_model` is part of the request
and not something the engine can infer from a flat register dict.

The distinction that makes this schema worth anything to an inverter evaluator:
480 V line-to-line and 277 V line-to-neutral land in DIFFERENT fields.

Expected values are from FULL_SWEEP_RESULTS.md (2026-08-30).
"""

import pytest

from conftest import canon

# Raw integer registers exactly as a device presents them, plus their scale
# factors. Nothing here is pre-scaled; if the engine stops applying a shared
# factor these values move by 10x and the assertions below catch it.
MODEL_103 = {
    "A": 152, "A_SF": -1,
    "AphA": 51, "AphB": 50, "AphC": 51,
    "PPVphAB": 4801, "PPVphBC": 4798, "PPVphCA": 4803,
    "PhVphA": 2772, "PhVphB": 2770, "PhVphC": 2773, "V_SF": -1,
    "W": 7200, "W_SF": 0,
    "Hz": 6001, "Hz_SF": -2,
    "VA": 7400, "VA_SF": 0,
    "VAr": -1200, "VAr_SF": 0,
    "PF": 973, "PF_SF": -3,
    "WH": 48750000, "WH_SF": 0,
    "DCA": 184, "DCA_SF": -1,
    "DCV": 7821, "DCV_SF": -1,
    "DCW": 14389, "DCW_SF": 0,
    "TmpCab": 472, "TmpSnk": 451, "TmpTrns": 438, "TmpOt": 312, "Tmp_SF": -1,
    "St": 4, "StVnd": 0,
    "Evt1": 0, "Evt2": 0, "EvtVnd1": 0,
}

VALUE_REGISTERS = [r for r in MODEL_103 if not r.endswith("_SF")]

EXPECTED = {
    "line_current_a": 15.2,
    "ac_current_phase_a": 5.1, "ac_current_phase_b": 5.0, "ac_current_phase_c": 5.1,
    "ac_voltage_ll_ab": 480.1, "ac_voltage_ll_bc": 479.8, "ac_voltage_ll_ca": 480.3,
    "ac_voltage_phase_a": 277.2, "ac_voltage_phase_b": 277.0, "ac_voltage_phase_c": 277.3,
    "inverter_output_kw": 7.2, "dc_power_w": 14389, "apparent_power_va": 7400,
    "reactive_power_kvar": -1.2, "power_factor": 0.973, "grid_frequency_hz": 60.01,
    "energy_delivered_kwh": 48750.0, "dc_current_a": 18.4, "dc_voltage_v": 782.1,
    "inverter_cabinet_temp_c": 47.2, "inverter_heatsink_temp_c": 45.1,
    "inverter_transformer_temp_c": 43.8, "inverter_other_temp_c": 31.2,
}


@pytest.fixture(scope="module")
def out(client):
    r = client.post("/v1/normalize", json={"oem": "sunspec_inverter",
                                           "sunspec_model": 103,
                                           "data": MODEL_103})
    assert r.status_code == 200, r.text[:400]
    return r.json()


def test_the_register_set_is_still_28_points():
    assert len(VALUE_REGISTERS) == 28


def test_every_register_maps(out):
    missing = [t for t in VALUE_REGISTERS if not canon(out, t)]
    assert not missing, f"unresolved Model 103 registers: {missing}"


def test_coverage_is_total(out):
    assert out["coverage_pct"] == 100.0


def test_no_collisions(out):
    assert not out.get("collisions")


def test_no_invariant_violations(out):
    assert not out.get("_invariant_violations")


def test_nothing_was_nulled(out):
    assert not out.get("null_states")


@pytest.mark.parametrize("field,expected", sorted(EXPECTED.items()))
def test_scaled_value(out, field, expected):
    got = (out["normalized"] or {}).get(field)
    assert got is not None, f"{field} missing"
    assert abs(got - expected) < 0.051, f"{field}: got {got}, expected {expected}"


def test_line_to_line_and_line_to_neutral_are_different_fields(out):
    n = out["normalized"]
    assert n["ac_voltage_ll_ab"] > 400          # 480 V L-L
    assert n["ac_voltage_phase_a"] < 300        # 277 V L-N
    assert n["ac_voltage_ll_ab"] != n["ac_voltage_phase_a"]


def test_enum_state_is_decoded(out):
    assert (out["normalized"] or {}).get("inverter_state") == "mppt"


def test_shared_scale_factors_are_applied_to_every_sibling(out):
    """A_SF governs four current points, not just A. This is the assertion that
    fails if shared-factor resolution regresses to per-point lookup."""
    n = out["normalized"]
    for f in ("ac_current_phase_a", "ac_current_phase_b", "ac_current_phase_c"):
        assert n[f] < 10, f"{f}={n[f]} looks unscaled (A_SF not applied)"
    for f in ("inverter_cabinet_temp_c", "inverter_heatsink_temp_c",
              "inverter_transformer_temp_c", "inverter_other_temp_c"):
        assert n[f] < 100, f"{f}={n[f]} looks unscaled (Tmp_SF not applied)"


def test_omitting_sunspec_model_does_not_silently_ship_10x_values(out, client):
    """Documented limitation, pinned so it cannot get worse.

    Without `sunspec_model` the shared factors are unreachable and the phase
    values come out 10x high -- and INSIDE their physics bounds, so nothing
    downstream catches them. A real register read carries the model ID; a
    hand-assembled payload may not. If this ever starts passing, the engine
    learned to infer the model and the integration guide can drop the warning.
    """
    r = client.post("/v1/normalize", json={"oem": "sunspec_inverter",
                                           "data": MODEL_103})
    undeclared = r.json()["normalized"]
    declared = out["normalized"]
    assert declared["ac_current_phase_a"] == 5.1
    assert undeclared.get("ac_current_phase_a") == 51, (
        "undeclared-model behaviour changed -- re-check the integration guide "
        "note about passing sunspec_model")
