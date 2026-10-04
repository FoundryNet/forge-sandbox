"""The pack compiler's refusals, each with a positive control beside it.

A refusal that nobody has watched fire is a refusal that may not fire. Every
test here asserts BOTH that the bad pack is refused with the right class AND
that `clean.json` — identical in shape, different only in the defect — still
compiles, so a green test cannot be an artefact of the fixture.

The fixtures are in tests/pack_fixtures/ and are synthetic: they are not real
OEMs and must never be moved into app/packs/.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import unit_converter as uc              # noqa: E402
from tools import compile_pack as cp              # noqa: E402

FIX = os.path.join(ROOT, "tests", "pack_fixtures")


def compile_one(fixture):
    ok, refused = cp.compile_packs([os.path.join(FIX, fixture)])
    return ok, refused


def test_the_clean_fixture_compiles():
    """The positive control. If this ever fails, every refusal below is suspect."""
    ok, refused = compile_one("clean.json")
    assert refused == [], [str(r) for r in refused]
    assert len(ok) == 1 and ok[0]["mapping_count"] == 4


@pytest.mark.parametrize("fixture,cls,must_mention", [
    ("malformed.json", "malformed", "protocol"),
    ("dimension_mismatch.json", "dimension_mismatch", "powerusage_kw"),
    ("collision.json", "tag_collision", "SP_SPEED"),
    ("missing_decoder.json", "missing_decoder", "TOOL_CHANGE"),
    # Not one of the four the gate named, but the same harm class and found by
    # driving a real payload: `%` declared onto a field whose bounds are [0, 1].
    ("scale_mismatch.json", "dimension_mismatch", "robot.speed_scaling"),
])
def test_each_negative_control_is_refused(fixture, cls, must_mention):
    ok, refused = compile_one(fixture)
    assert ok == [], f"{fixture} compiled when it must not"
    assert len(refused) == 1, [str(r) for r in refused]
    assert refused[0].cls == cls, refused[0].cls
    assert must_mention in refused[0].detail, refused[0].detail
    # the reason must be actionable, not a bare class name
    assert len(refused[0].detail) > 60, refused[0].detail
    # and it must be one of the declared blocking classes, not a new one
    assert cls in cp.BLOCKING


def test_the_compiler_exits_nonzero_on_a_refusal():
    assert cp.main([os.path.join(FIX, "clean.json")]) == 0
    for bad in ("malformed.json", "dimension_mismatch.json", "collision.json",
                "missing_decoder.json", "scale_mismatch.json"):
        assert cp.main([os.path.join(FIX, bad)]) == 1, bad


def test_dimension_is_derived_from_the_existing_tables_not_restated():
    """unit_dimension must equal the inline idiom unit_converter uses itself.

    The converter's own cross-dimension refusal computes
    `DIMENSION.get(QUANTITY.get(u), QUANTITY.get(u))` inline
    (app/unit_converter.py, the unit_quantity_mismatch branch). prod has that
    folded into a private `_si_dimension`; the sandbox does not, so this test
    pins the compiler to the live tables rather than to a copy of them. If a
    quantity is ever added to QUANTITY, this catches a compiler that did not
    learn about it.
    """
    def inline(u):
        q = uc.QUANTITY.get(u)
        if q is None:
            q = uc.QUANTITY.get(uc.UNIT_ALIASES.get(u, ""))
        return uc.DIMENSION.get(q, q) if q else None

    units = sorted(set(uc.QUANTITY) | set(uc.UNIT_ALIASES))
    assert units, "unit tables are empty - the import is wrong"
    disagree = [u for u in units if cp.unit_dimension(u) != inline(u)]
    assert disagree == [], disagree
    # and the tables must actually be populated, so a silent empty-dict import
    # cannot make this test vacuous
    assert cp.unit_dimension("kWh") == "energy"
    assert cp.unit_dimension("kW") == "power"
    assert cp.unit_dimension("mm/min") == "velocity"   # DIMENSION override


def test_the_specific_case_the_gate_names():
    """powerusage_kw -> energy_kwh must fail. Power is not energy."""
    assert cp.unit_dimension("kW") == "power"
    assert cp.field_dimension("energy_kwh")[0] == "energy"
    assert cp.unit_dimension("kW") != cp.field_dimension("energy_kwh")[0]


@pytest.mark.parametrize("pack", ["heidenhain.json", "yaskawa_motoman.json"])
def test_the_two_new_oem_packs_compile(pack):
    """Gate 2: a new OEM added by JSON alone must pass the same gate."""
    ok, refused = cp.compile_packs([os.path.join(ROOT, "app", "packs", pack)])
    assert refused == [], [str(r) for r in refused]
    assert ok[0]["cited_rows"] == ok[0]["mapping_count"], (
        "every mapping in a new pack must carry a citation")


@pytest.mark.parametrize("pack", ["heidenhain.json", "yaskawa_motoman.json"])
def test_the_new_packs_record_what_they_could_not_map(pack):
    """The documented-but-unmapped list is the honest half of a pack."""
    raw = json.load(open(os.path.join(ROOT, "app", "packs", pack)))
    assert len(raw.get("not_mapped") or {}) >= 5, (
        "a pack authored from a real manual always leaves documented items "
        "unmapped; an empty not_mapped means they were not written down")
