"""The public contact address is one live address, on every surface.

`forge@` on foundrynet.io never existed as a mailbox. The domain is send-only
(Resend DKIM on send.foundrynet.io), so inbound mail to it bounces. The sandbox
displayed it twice: the README footer, and the licensing/attribution line in
THIRD_PARTY_NOTICES.md — which is the address of record for CC BY 4.0
attribution corrections and licensing questions, so a bounce there is a
compliance defect rather than a cosmetic one.

The sandbox sends no mail at all, so there is no legitimate sender address here
and no exemption: every address on a public surface must be the contact
address, or an RFC 2606 reserved placeholder that can never route.

Run: python3 -m pytest tests/test_contact_address.py -q
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTACT = "foundrynet@proton.me"

_RESERVED = re.compile(
    r"(^|\.)(example\.(com|org|net)|example|invalid|test|localhost)$", re.I)
# Domain must start with a letter: excludes Package-URL fragments such as
# `pkg:pypi/python-dateutil@2.9.0.post0`, which are not addresses.
_EMAIL = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z][A-Za-z0-9.-]*\.[A-Za-z]{2,}")

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".pytest_cache", "fixtures"}
_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".sh", ".html",
             ".cfg", ".ini", ".toml"}


def _public_files():
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(names):
            if ".bak" in name:          # the sandbox carries several; none ship
                continue
            if name == os.path.basename(__file__):
                continue
            if os.path.splitext(name)[1].lower() not in _SUFFIXES:
                continue
            full = os.path.join(root, name)
            yield os.path.relpath(full, REPO), full


def _scan(extra_text=None):
    bad = []
    for rel, full in _public_files():
        try:
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _EMAIL.findall(line):
                if match.lower() == CONTACT:
                    continue
                if _RESERVED.search(match.split("@", 1)[1]):
                    continue
                bad.append(f"{rel}:{lineno}  {match}")
    for match in _EMAIL.findall(extra_text or ""):
        if match.lower() != CONTACT and not _RESERVED.search(match.split("@", 1)[1]):
            bad.append(f"<injected>  {match}")
    return bad


def test_scanner_can_find_an_address():
    """Positive control: without this, an empty result from the check below
    could mean the regex is broken rather than the tree being clean."""
    dead = "forge@foundrynet" + ".io"
    assert any(dead in row for row in _scan(extra_text=f"contact: {dead}"))


def test_no_dead_address_on_any_public_surface():
    found = _scan()
    assert not found, ("addresses other than the public contact appear on "
                       "public surfaces:\n  " + "\n  ".join(found))


def test_readme_footer_names_the_live_address():
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
        rows = [l for l in fh.read().splitlines()
                if l.startswith("Forge by Foundry Labs ·")]
    assert rows, "the README footer line is missing"
    assert rows[0] == (f"Forge by Foundry Labs · [{CONTACT}](mailto:{CONTACT})"), rows[0]


def test_licensing_contact_address_is_live():
    """THIRD_PARTY_NOTICES.md is the address of record for licensing and
    CC BY 4.0 attribution corrections."""
    path = os.path.join(REPO, "THIRD_PARTY_NOTICES.md")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    idx = [i for i, l in enumerate(lines)
           if "correction to this file" in l.lower()]
    assert idx, "the licensing-contact line is missing from THIRD_PARTY_NOTICES.md"
    assert lines[idx[0] + 1].strip() == CONTACT, lines[idx[0] + 1]
