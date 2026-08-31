"""Demo rehearsal -- the 35 beats a prospect actually sees.

This is the only suite here that leaves the process, and it exists because of a
specific failure: on 2026-08-31 the demo had been broken for five days while
every source-level suite was green. `run_demo.sh` deliberately runs off a LOCAL
pinned image (`forge-demo:pinned`) so a demo cannot change under you mid-call --
and that pinning is exactly why a fix landing in source never reached it. The
pinned image predated the cross-pack unit-contract fix, so `M_AC_Power` (declared
W in `solaredge_meter`) kept 412600 and was nulled as a physics violation.

THE DEMO IS WHAT THE PROSPECT SEES, NOT THE SOURCE.

So this drives the real container through `run_demo.sh --check`, not the
in-process app. It SKIPS rather than fails when docker or the demo directory is
absent, because a clone on a machine with neither is not a broken demo.

Re-pin after a GHCR push with:
    docker tag ghcr.io/foundrynet/forge-sandbox:latest forge-demo:pinned
"""

import os
import shutil
import subprocess

import pytest

DEMO_DIR = os.path.expanduser("~/Desktop/licensing-demo")
DEMO_SH = os.path.join(DEMO_DIR, "run_demo.sh")
EXPECTED_BEATS = 35


def _docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.isfile(DEMO_SH),
                       reason=f"demo not present at {DEMO_DIR}"),
    pytest.mark.skipif(not _docker_ok(),
                       reason="docker is not running"),
]


@pytest.fixture(scope="module")
def rehearsal():
    """Run the real rehearsal and hand back its output."""
    proc = subprocess.run([DEMO_SH, "--check"], cwd=DEMO_DIR,
                          capture_output=True, text=True, timeout=600)
    # --check runs preflight and then renders the deck; the verdict is preflight's.
    return proc


def _plain(text):
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_every_beat_verifies(rehearsal):
    out = _plain(rehearsal.stdout + rehearsal.stderr)
    failed = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]
    assert not failed, (
        "the demo is NOT safe to run:\n  " + "\n  ".join(failed) +
        "\n\nIf source is green and this is not, the pinned image is stale:\n"
        "  docker tag ghcr.io/foundrynet/forge-sandbox:latest forge-demo:pinned")


def test_all_thirty_five_beats_ran(rehearsal):
    """A beat that silently stops running is a beat nobody is checking."""
    out = _plain(rehearsal.stdout + rehearsal.stderr)
    checks = sum(1 for ln in out.splitlines()
                 if ln.strip().startswith(("ok ", "FAIL")))
    assert checks == EXPECTED_BEATS, f"{checks} beats ran, expected {EXPECTED_BEATS}"


def test_rehearsal_declares_the_demo_safe(rehearsal):
    out = _plain(rehearsal.stdout + rehearsal.stderr)
    assert "Demo is safe to run" in out, out[-1500:]
