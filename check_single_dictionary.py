#!/usr/bin/env python3
"""Build-time guard: one canonical dictionary, one truth.

`app/canonical_fields.json` is the SOURCE OF TRUTH -- field_registry.py loads it
and it is the only vocabulary the kernel may emit.

`app/packs/_canonical_fields.json` is NOT a stray copy. It is a GENERATED
artifact: `tools/build_packs.py` writes it and `tools/build_robotics_packs.py`
reads it back as the resolution vocabulary. Deleting it breaks pack generation,
so this check does not demand its absence -- it demands that it cannot DRIFT.

Fails the build if any second dictionary disagrees with the source by even one
field name. Run in CI before packaging the image.

The long-term fix is to make build_packs.py emit a single file and point the
other tools at it; that is a change to pack tooling, not a delete.
"""
import glob, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "app", "canonical_fields.json")


def names(path):
    d = json.load(open(path))
    f = d.get("fields", d)
    return set(f) if isinstance(f, dict) else {
        x if isinstance(x, str) else x.get("name") for x in f}


def main():
    if not os.path.exists(SOURCE):
        print(f"FAIL: source of truth missing: {SOURCE}"); return 1
    src = names(SOURCE)
    print(f"source of truth: app/canonical_fields.json  ({len(src)} fields)")

    dupes, bad = [], 0
    for p in glob.glob(os.path.join(HERE, "app", "**", "*canonical_fields*.json"),
                       recursive=True):
        if os.path.abspath(p) == os.path.abspath(SOURCE) or ".bak" in p:
            continue
        dupes.append(p)
        try:
            other = names(p)
        except Exception as e:
            print(f"FAIL: duplicate {p} unreadable: {e}"); bad += 1; continue
        rel = os.path.relpath(p, HERE)
        if other == src:
            print(f"  ok   {rel}: generated artifact, {len(other)} fields, matches source")
        else:
            print(f"  FAIL {rel}: HAS DRIFTED from the source of truth "
                  f"(+{len(other - src)} extra / -{len(src - other)} missing). "
                  f"Regenerate with tools/build_packs.py or reconcile by hand.")
            bad += 1
    if bad:
        print(f"\n{bad} dictionary file(s) disagree with the source. The build must "
              f"not ship two different vocabularies.")
        return 1
    print(f"PASS: {len(dupes)} generated copy/copies, all identical to the source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
