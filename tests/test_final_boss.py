"""Final boss -- one payload, every bug class.

49 fields, `oem: siemens`, `locale: de_DE`: German tags, a decimal comma, an
OPC node ID, uint16 and int32 sentinels, a quality wrapper, quoted numbers,
values that name their own unit, and deliberate garbage. 36 of the 49 resolve
to nothing, which is the point -- the tags the kernel does NOT recognise are
the ones nobody has looked at, and they used to skip every gate.

The bar is not "everything maps". It is that every field is DECIDED: resolved,
explicitly nulled with a reason, or explicitly listed as unresolved. Nothing
may pass through unexamined.

Payload: tests/fixtures/final_boss_49.json (from FINAL_BOSS_TEST.md, 2026-08-27).
"""

import json
import os

import pytest

from conftest import FIXTURES

SENTINELS = {65535, 32767, -32768, 2147483647, -2147483648, 4294967295}


@pytest.fixture(scope="module")
def payload():
    with open(os.path.join(FIXTURES, "final_boss_49.json")) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def out(client, payload):
    r = client.post("/v1/normalize", json=payload)
    assert r.status_code == 200, r.text[:400]
    return r.json()


def test_payload_is_still_49_fields(payload):
    assert len(payload["data"]) == 49


def test_every_field_is_decided(out, payload):
    fm = out.get("field_mappings") or {}
    ns = out.get("null_states") or {}
    unresolved = out.get("unresolved_tags") or []
    null_raw = {v.get("raw_field") for v in ns.values() if isinstance(v, dict)}

    undecided = [t for t in payload["data"]
                 if t not in fm and t not in unresolved and t not in null_raw]
    assert not undecided, f"undecided fields: {undecided}"


def test_no_tier_one_sentinel_survives(out):
    leaked = {k: v for k, v in (out["normalized"] or {}).items() if v in SENTINELS}
    assert not leaked, f"sentinel values reached the caller: {leaked}"


def test_no_raw_structure_reaches_the_caller(out):
    bad = {k: type(v).__name__ for k, v in (out["normalized"] or {}).items()
           if isinstance(v, (dict, list))}
    assert not bad, f"raw structures in output: {bad}"


def test_field_accounting_adds_up(out):
    assert out["fields_total"] == out["fields_mapped"] + out["fields_unknown"]


def test_no_invariant_violations(out):
    """The relief valve is a backstop. If it fires here, a gate above it failed."""
    assert not out.get("_invariant_violations")


def test_collisions_are_explicit_about_what_was_kept(out):
    """Two tags landing on one canonical is allowed. Silently picking one is not."""
    for canonical, entries in (out.get("collisions") or {}).items():
        for e in entries:
            assert "kept" in e, f"{canonical}: collision entry without a verdict"
            if not e["kept"]:
                assert e.get("superseded_because") or e.get("stored_as"), \
                    f"{canonical}: a discarded value with no reason and nowhere to go"
