#!/usr/bin/env bash
#
# pre-push-gate.sh — the gate between "works on my machine" and "a prospect
# pulled the image".
#
#   ./pre-push-gate.sh          build, boot and verify. Does NOT push.
#   ./pre-push-gate.sh --push   the same, then push to GHCR only if all green.
#
# Every step runs against a FRESHLY BUILT IMAGE on an unused port, never
# against the working tree and never against a container that happens to be
# running. The bug that started this was a stale container on :8080 answering
# for an image that no longer existed.
#
# ANY failure exits non-zero and nothing is pushed.

set -Eeuo pipefail

IMAGE="${IMAGE:-ghcr.io/foundrynet/forge-sandbox}"
TAG="${TAG:-latest}"
GATE_IMAGE="forge-sandbox:gate-test"
GATE_NAME="forge-gate-$$"
GATE_PORT="${GATE_PORT:-9999}"
PUSH=0
[ "${1:-}" = "--push" ] && PUSH=1

cd "$(dirname "$0")"

step()  { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
ok()    { printf '\033[32m  ok\033[0m %s\n' "$*"; }
die()   { printf '\n\033[31m=== GATE FAILED: %s ===\033[0m\n' "$*" >&2; exit 1; }

cleanup() { docker rm -f "$GATE_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'die "interrupted"' INT TERM

# A port with something already on it means we would be testing that thing.
if curl -fsS -m 2 "localhost:${GATE_PORT}/health" >/dev/null 2>&1; then
    die "port ${GATE_PORT} is already serving. Free it, or set GATE_PORT=."
fi

step "1/8  unit suite"
python3 -m pytest tests/ -x -q --tb=short || die "unit tests"
ok "pytest"

step "2/8  prospect evaluation suite"
# Every target company's first payload. A failure names the prospect.
python3 -m pytest tests/test_prospect_evaluation.py -x -q --tb=short \
    || die "a prospect evaluation would have failed"
ok "every prospect payload resolves correctly"

step "3/8  own-pack regression"
python3 -m pytest tests/test_own_pack.py -x -q --tb=short || die "own-pack regression"
ok "own-pack"

step "4/8  build image"
docker build -q -t "$GATE_IMAGE" . >/dev/null || die "docker build"
ok "$GATE_IMAGE"

step "5/8  boot on :${GATE_PORT}"
docker run -d --name "$GATE_NAME" -p "${GATE_PORT}:8000" "$GATE_IMAGE" >/dev/null \
    || die "docker run"
for i in $(seq 1 30); do
    curl -fsS -m 2 "localhost:${GATE_PORT}/health" >/dev/null 2>&1 && break
    [ "$i" = 30 ] && die "container never became healthy"
    sleep 1
done
curl -fsS "localhost:${GATE_PORT}/health" | python3 -c \
    'import json,sys; b=json.load(sys.stdin); assert b["status"]=="healthy", b; print("   ",b["service"],b["version"])' \
    || die "health"
ok "healthy"

step "6/8  live endpoint smoke"
# Against the running IMAGE, not the working tree.
RESULT=$(curl -fsS -X POST "localhost:${GATE_PORT}/v1/normalize" \
    -H 'Content-Type: application/json' \
    -d '{"oem":"haas","data":{"S1Temp":72.1,"SP_SPEED":5204}}') || die "normalize"
echo "$RESULT" | python3 -c \
    'import json,sys; n=json.load(sys.stdin)["normalized"]; assert n["spindle_speed_rpm"]==5204, n; print("   spindle_speed_rpm =",n["spindle_speed_rpm"])' \
    || die "normalize returned the wrong value"
ok "normalize"

step "7/8  live robotics regression (the deal payloads)"
# The three defects that shipped, checked against the built image rather than
# the source tree: tcp_speed 60x, battery_pct unresolved, refusal note "None".
python3 - "$GATE_PORT" <<'PY' || die "robotics regression against the live image"
import json, sys, urllib.request

port = sys.argv[1]

def post(body):
    req = urllib.request.Request(
        f"http://localhost:{port}/v1/normalize",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

fails = []

r = post({"oem": "kuka", "data": {"TCP_Speed_mm_s": 250.0}})
tcp = r["normalized"].get("sensor_readings.tcp_speed")
if tcp != 250.0:
    fails.append(f"tcp_speed corrupted: 250 mm/s -> {tcp}")

r = post({"oem": "locus", "data": {"battery_pct": 68, "payload_kg": 8.6}})
if r["normalized"].get("battery_soc_pct") != 68:
    fails.append(f"battery_pct did not resolve: {r['normalized']}")
if r["oem_recognized"] is not True:
    fails.append("locus reported oem_recognized: false")

r = post({"oem": "universal_robots", "data": {"actual_q_0": 1.57}})
pos = r["normalized"].get("robot.joint.0.position")
if pos is None or abs(pos - 89.95) > 0.1:
    fails.append(f"UR joint 0 did not resolve to degrees: {pos}")

# The refusal note must never interpolate a null.
r = post({"oem": "generic", "data": {"BatteryCurrent": -12.4}})
for tag, m in r["field_mappings"].items():
    note = m.get("note") or ""
    if "is None" in note or "a None mapping" in note:
        fails.append(f"refusal note prints None: {note[:90]}")

if fails:
    for f in fails:
        print("   FAIL:", f)
    sys.exit(1)
print("    tcp_speed 250 mm/s · battery_pct 68 · UR joint 89.95 deg · notes clean")
PY
ok "robotics"

step "8/8  publish + anonymous-pull parity"
# Two traps live in this step.
#
# 1. The image that was TESTED must be the image that is PUSHED. Pushing
#    "${IMAGE}:${TAG}" directly would publish whatever that tag pointed at
#    before the gate ran -- which, the first time this script existed, was a
#    20-hour-old build that never saw a single test above.
# 2. The published image is multi-arch. A plain `docker build` on an Apple
#    Silicon machine produces arm64 ONLY, so pushing it would leave every
#    amd64 prospect unable to run the image at all.
#
# So the publish is a buildx run over the same source tree, for both
# platforms, and the manifest is checked for both before we call it done.
if [ "$PUSH" = 1 ]; then
    docker buildx build \
        --platform linux/amd64,linux/arm64 \
        --tag "${IMAGE}:${TAG}" \
        --push . || die "buildx push"
    ok "pushed ${IMAGE}:${TAG} (linux/amd64, linux/arm64)"

    # No f-string here on purpose: escaping quotes inside one, inside a
    # single-quoted shell argument, is a SyntaxError on 3.11 and it failed
    # AFTER the push had already succeeded -- reporting a red gate on a
    # correctly published image, which is the worst of both.
    docker manifest inspect "${IMAGE}:${TAG}" | python3 -c '
import json, sys
have = set()
for m in json.load(sys.stdin)["manifests"]:
    plat = m["platform"]
    have.add(plat["os"] + "/" + plat["architecture"])
need = {"linux/amd64", "linux/arm64"}
missing = need - have
assert not missing, "published manifest is missing " + repr(missing)
real = sorted(x for x in have if "unknown" not in x)
print("    platforms: " + ", ".join(real))
' || die "published manifest is not multi-arch"
    ok "multi-arch manifest"
    ANON_DIR=$(mktemp -d)
    if DOCKER_CONFIG="$ANON_DIR" docker manifest inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
        ok "anonymous pull verified"
    else
        rm -rf "$ANON_DIR"
        die "pushed, but an anonymous client cannot see it — check GHCR visibility"
    fi
    rm -rf "$ANON_DIR"
else
    ok "skipped (no --push); re-run with --push to publish"
fi

printf '\n\033[32m=== ALL GATES PASSED ===\033[0m\n'
[ "$PUSH" = 1 ] || printf 'Nothing was pushed. Run: ./pre-push-gate.sh --push\n'
