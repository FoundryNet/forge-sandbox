"""Own-pack regression -- every tag in every pack resolves to its own canonical.

The packs are data, not code, so nothing else in the suite notices when an
edit to one silently changes what a tag means. 2,131 mappings across 19 packs;
this walks all of them.

ONE TRAP, and it produces exactly 431 false failures if you miss it: the engine
folds alias canonicals into a single primary. Axis fields arrived in two
competing shapes -- an INDEXED form (`axes.0.position_actual`) and a LETTERED
one (`axes.x_position_actual`) -- for the same physical axis, and the lettered
form won. A pack still DECLARES the indexed form and the engine correctly
RETURNS the lettered primary. Comparing the raw strings reads that as a
mismatch on every indexed axis row. Both sides go through
`corpus.resolve_canonical` for that reason.
"""

import glob
import json
import os

import pytest

from app import corpus

PACK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "app", "packs")
CHUNK = 40


def _packs():
    out = []
    for path in sorted(glob.glob(os.path.join(PACK_DIR, "*.json"))):
        name = os.path.basename(path)[:-5]
        if name.startswith("_"):          # _index, _canonical_fields, _registry_gaps
            continue
        with open(path) as fh:
            mappings = json.load(fh).get("mappings") or {}
        if mappings:
            out.append((name, mappings))
    return out


PACKS = _packs()
TOTAL = sum(len(m) for _, m in PACKS)


def test_the_pack_set_has_not_shrunk():
    """A pack that fails to load is otherwise invisible -- its rows just stop
    being checked and everything still passes."""
    assert len(PACKS) >= 19, f"only {len(PACKS)} packs loaded"
    assert TOTAL >= 2131, f"only {TOTAL} mappings loaded (was 2131)"


@pytest.mark.parametrize("pack_name,mappings", PACKS, ids=[n for n, _ in PACKS])
def test_every_tag_resolves_to_its_declared_canonical(client, pack_name, mappings):
    tags = sorted(mappings)
    bad = []
    for i in range(0, len(tags), CHUNK):
        batch = tags[i:i + CHUNK]
        r = client.post("/v1/normalize",
                        json={"oem": pack_name, "data": {t: 1 for t in batch}})
        assert r.status_code == 200, f"{pack_name}: HTTP {r.status_code}"
        fm = r.json().get("field_mappings") or {}
        for t in batch:
            want = corpus.resolve_canonical(mappings[t])
            got = (fm.get(t) or {}).get("canonical_field")
            got = corpus.resolve_canonical(got) if got else got
            if got != want:
                bad.append((t, mappings[t], got))
    assert not bad, (f"{pack_name}: {len(bad)}/{len(tags)} mismatched, "
                     f"first 5: {bad[:5]}")
