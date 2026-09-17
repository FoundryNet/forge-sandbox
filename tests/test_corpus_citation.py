"""The nte_hash is a CITATION, not a receipt (2026-09-15).

Mirror of forge-prod/tests/test_corpus_citation.py. The two engines ship
different corpora on purpose, so they MUST produce different citations — what
has to stay identical is the hash function, the field names and the
evidentiary ladder. The parity tests at the bottom pin exactly that.
"""
import hashlib
import json

import pytest

from app import corpus_version as cv


def test_release_is_engine_plus_semver_plus_digest():
    r = cv.release()
    engine, _, rest = r.partition("/")
    semver, _, suffix = rest.partition("-")
    assert engine == cv.ENGINE == "forge-sandbox"
    assert semver == cv.CORPUS_SEMVER
    assert len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix), r
    assert len(r) <= 40


def test_sandbox_never_issues_a_production_citation():
    """A sandbox reading must not be presentable as a production one. The engine
    is in the citable string itself, not only in a sibling field."""
    assert cv.release().startswith("forge-sandbox/")
    assert "forge-prod" not in cv.release()


def test_release_covers_every_decision_table():
    layers = cv.layer_digests()
    assert set(layers) == {"registry", "packs", "bounds", "sentinels", "units"}
    for name, comp in layers.items():
        assert comp.get("sha256"), name
        assert "error" not in comp, f"{name} unreadable: {comp.get('error')}"


def test_release_digest_is_a_function_of_the_layer_digests():
    layers = cv.layer_digests()
    expect = hashlib.sha256(json.dumps(
        {k: v["sha256"] for k, v in layers.items()},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert cv.release_detail()["digest"] == expect


def test_version_is_identical_across_processes():
    """Two containers of one image MUST issue the same citation. Subprocesses
    so each gets its own PYTHONHASHSEED and its own heap — the only way to catch
    a digest that accidentally depends on set order or an object address."""
    import subprocess, sys, pathlib
    root = str(pathlib.Path(__file__).resolve().parents[1])
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from app import corpus_version as cv; print(cv.release())" % root)
    seen = {subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, check=True).stdout.strip()
            for _ in range(3)}
    assert len(seen) == 1, f"corpus version forked across processes: {seen}"


def test_digest_refuses_objects_with_no_stable_representation():
    with pytest.raises(TypeError, match="no stable representation"):
        cv._sha({"fn": lambda v: v})


def test_a_changed_pack_mapping_changes_the_version():
    from app import corpus
    packs, _by_alias, _dict = corpus.load()
    oem = sorted(packs)[0]
    pack = packs[oem]
    cv.invalidate()
    before = cv.release()
    pack.mappings["zzz_citation_probe"] = "power_kw"
    cv.invalidate()
    try:
        assert cv.release() != before, "a new mapping MUST move the corpus version"
    finally:
        pack.mappings.pop("zzz_citation_probe", None)
        cv.invalidate()
    assert cv.release() == before


def test_a_changed_conversion_factor_changes_the_version():
    from app import unit_converter as uc
    key = ("psi", "bar")
    original = uc.CONVERSIONS[key]
    cv.invalidate()
    before = cv.release()
    uc.CONVERSIONS[key] = (lambda v: v * 0.07, "psi_to_bar")   # same name, new factor
    cv.invalidate()
    try:
        assert cv.release() != before
    finally:
        uc.CONVERSIONS[key] = original
        cv.invalidate()
    assert cv.release() == before


def test_tag_units_are_inside_the_digest():
    """A SunSpec register named for its quantity carries its unit ONLY in
    tag_units. Changing it changes the stored value with no mapping edit, so it
    has to move the citation."""
    from app import corpus
    packs, _b, _d = corpus.load()
    pack = next((p for p in packs.values() if getattr(p, "tag_units", None)), None)
    if pack is None:
        pytest.skip("no pack declares tag_units")
    tag = sorted(pack.tag_units)[0]
    original = pack.tag_units[tag]
    cv.invalidate()
    before = cv.release()
    pack.tag_units[tag] = "zzz_not_a_unit"
    cv.invalidate()
    try:
        assert cv.release() != before
    finally:
        pack.tag_units[tag] = original
        cv.invalidate()


def test_state_carries_this_engines_movable_layers():
    st = cv.state()
    assert st["release"] == cv.release()
    assert st["engine"] == "forge-sandbox"
    # Named for what they actually are here, not for production's layers.
    assert "fleet_overlay" in st and "satellite" in st


# ── the evidentiary ladder — MUST match production exactly ───────────────────
@pytest.mark.parametrize("match_type,layer,evidence", [
    ("exact_canonical", "L0_identity",   "deterministic"),
    ("deterministic",   "L1_pack",       "deterministic"),
    ("corpus_exact",    "L1_corpus",     "deterministic"),
    ("user_confirmed",  "L1_user",       "deterministic"),
    ("vector",          "L2_vector",     "probabilistic"),
    ("signal_inferred", "L2_signal",     "probabilistic"),
    ("llm_inferred",    "L3_llm",        "probabilistic"),
    ("sticky",          "L4_sticky",     "derived"),
    ("unknown",         "L_none",        "unresolved"),
])
def test_match_layer_ladder(match_type, layer, evidence):
    assert cv.match_layer(match_type) == (layer, evidence)


def test_every_match_type_this_engine_emits_is_classified():
    """Falling through to L_unclassified is SAFE but uninformative — it
    understates a pack hit as probabilistic. Anything app/corpus.py actually
    emits must be named explicitly."""
    emitted = {"fleet_corpus", "corpus", "corpus_normalized", "identity",
               "cross_oem", "cross_oem_unit_evidence", "signal", "unknown"}
    missing = emitted - set(cv.MATCH_LAYERS)
    assert not missing, f"unclassified match types: {sorted(missing)}"


@pytest.mark.parametrize("match_type,layer", [
    ("corpus",                  "L1_pack"),
    ("corpus_normalized",       "L1_pack"),
    ("fleet_corpus",            "L1_fleet"),
    ("cross_oem",               "L1_cross_oem"),
    ("cross_oem_unit_evidence", "L1_cross_oem"),
    ("identity",                "L0_identity"),
    ("signal",                  "L2_signal"),
])
def test_sandbox_native_match_types(match_type, layer):
    assert cv.match_layer(match_type)[0] == layer


def test_a_pack_hit_is_not_understated_as_probabilistic():
    """The regression this test exists for: before the ladder knew this
    engine's vocabulary, a curated pack row certified as `L_unclassified /
    probabilistic` — safe, but it understated the strongest evidence the
    engine has."""
    for mt in ("corpus", "corpus_normalized", "identity"):
        assert cv.match_layer(mt)[1] == "deterministic", mt


def test_unknown_match_type_never_claims_deterministic():
    for bogus in ("brand_new_layer", "", None, 17):
        assert cv.match_layer(bogus)[1] != "deterministic", bogus


# ── the property the whole change exists for ─────────────────────────────────
def _digest(payload):
    body = {k: v for k, v in payload.items()
            if k not in ("nte_hash", "field_context")}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def test_hash_moves_when_the_corpus_moves():
    base = {"normalized": {"spindle_load_pct": 87.0}, "nte_count": 1,
            "corpus_version": "forge-sandbox/1.0.0-aaaaaaaa"}
    moved = dict(base, corpus_version="forge-sandbox/1.0.0-bbbbbbbb")
    assert _digest(base) != _digest(moved)


def test_hash_is_stable_when_the_corpus_is_stable():
    base = {"normalized": {"spindle_load_pct": 87.0}, "nte_count": 1,
            "corpus_version": cv.release(), "corpus_state": cv.state()}
    assert _digest(base) == _digest(dict(base))


def test_a_sandbox_reading_cannot_collide_with_a_production_one():
    """Same numbers, same tag, different engine ⇒ different digest. This is the
    property that stops an evaluation result being presented as a licensed one."""
    reading = {"normalized": {"spindle_load_pct": 87.0}, "nte_count": 1}
    sandbox = dict(reading, corpus_version="forge-sandbox/1.0.0-aaaaaaaa")
    prod    = dict(reading, corpus_version="forge-prod/1.0.0-aaaaaaaa")
    assert _digest(sandbox) != _digest(prod)


def test_nte_count_and_corpus_version_are_both_covered():
    base = {"normalized": {"power_kw": 12.0}, "nte_count": 1,
            "corpus_version": cv.release()}
    assert _digest(dict(base, nte_count=999)) != _digest(base)
    assert _digest(dict(base, corpus_version="x/9.9.9-ffffffff")) != _digest(base)


def test_field_context_stays_outside_the_digest():
    base = {"normalized": {"power_kw": 12.0}, "nte_count": 1,
            "corpus_version": cv.release()}
    assert _digest(dict(base, field_context={"power_kw": {"unit": "kW"}})) == _digest(base)


# ── cross-engine parity: the FUNCTION, not the value ─────────────────────────
def test_hash_function_is_identical_to_production():
    """Both engines exclude exactly {nte_hash, field_context} and canonicalise
    the same way. If this drifts, an auditor's one published recipe stops
    working against one of the two engines."""
    from app.main import _NTE_HASH_EXCLUDED, _compute_nte_hash
    assert set(_NTE_HASH_EXCLUDED) == {"nte_hash", "field_context"}
    payload = {"b": 2, "a": 1, "nte_hash": "ignored", "field_context": {"x": 1}}
    assert _compute_nte_hash(payload) == _digest(payload)
