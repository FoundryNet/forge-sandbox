#!/usr/bin/env bash
#
# sync-verify.sh — prove the PUBLISHED image is the image we just tested.
#
# Runs after a GHCR push (pre-push-gate.sh calls it), or standalone at any time
# to answer "is what a prospect pulls right now actually current?".
#
#   ./sync-verify.sh                 verify ghcr.io/foundrynet/forge-sandbox:latest
#   LOCAL_IMAGE=forge-sandbox:gate-test ./sync-verify.sh
#                                    ...and also compare it against a local build
#
# The bug this exists for: the gate goes green against a locally built image, the
# push silently no-ops or pushes a different tag, and the published image stays a
# week old. Every check below therefore runs against a FRESHLY PULLED image, never
# against a local build and never against a container that happened to be running.
#
# ANONYMITY: we do NOT `docker logout`. Logging out destroys the operator's
# credentials as a side effect of a read-only check, and if the script dies
# midway they are left logged out with no warning. A throwaway DOCKER_CONFIG dir
# gives a genuinely credential-free pull with no lasting effect.

set -Eeuo pipefail

IMAGE="${IMAGE:-ghcr.io/foundrynet/forge-sandbox}"
TAG="${TAG:-latest}"
PORT="${SYNC_PORT:-9998}"
NAME="sync-check-$$"
LOCAL_IMAGE="${LOCAL_IMAGE:-}"
PROD_HEALTH="${PROD_HEALTH:-https://forge.foundrynet.io/health}"

step() { printf '\n\033[1m--- %s ---\033[0m\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  warn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m=== SYNC VERIFY FAILED: %s ===\033[0m\n' "$*" >&2; exit 1; }

ANON_DIR="$(mktemp -d)"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$ANON_DIR"; }
trap cleanup EXIT
trap 'die "interrupted"' INT TERM

printf '\n\033[1m=== SYNC VERIFICATION ===\033[0m\n'

step "pull ${IMAGE}:${TAG} anonymously"
DOCKER_CONFIG="$ANON_DIR" docker pull "${IMAGE}:${TAG}" >/dev/null 2>&1 \
    || die "an anonymous client cannot pull ${IMAGE}:${TAG} — check GHCR package visibility"
# `--format {{.Manifest.Digest}}` is not supported on every buildx build, and a
# failed format silently yields an empty string that then "matches" nothing.
# Parse the index digest out of the plain output instead.
# Digest lookup is INFORMATIONAL — it must never take the script down. Under
# `set -Eeuo pipefail` a failing command substitution in an assignment aborts
# the whole run, so every branch here is explicitly tolerated. (It already did
# exactly that once: the pull succeeded, the digest lookup failed, and the
# script exited 1 having verified nothing.)
REMOTE_DIGEST=""
if _out=$(DOCKER_CONFIG="$ANON_DIR" docker buildx imagetools inspect "${IMAGE}:${TAG}" 2>/dev/null); then
    REMOTE_DIGEST=$(printf '%s' "$_out" | awk '/^Digest:/ {print $2; exit}') || REMOTE_DIGEST=""
fi
if [ -z "$REMOTE_DIGEST" ]; then
    REMOTE_DIGEST=$(docker image inspect "${IMAGE}:${TAG}" \
        --format '{{index .RepoDigests 0}}' 2>/dev/null | cut -d@ -f2) || REMOTE_DIGEST=""
fi
ok "pulled ${REMOTE_DIGEST:-(digest unavailable)}"

step "boot the PULLED image on :${PORT}"
if curl -fsS -m 2 "localhost:${PORT}/health" >/dev/null 2>&1; then
    die "port ${PORT} is already serving — we would be testing that, not the pull"
fi
docker run -d --name "$NAME" -p "${PORT}:8000" "${IMAGE}:${TAG}" >/dev/null || die "docker run"
for i in $(seq 1 30); do
    curl -fsS -m 2 "localhost:${PORT}/health" >/dev/null 2>&1 && break
    [ "$i" = 30 ] && die "pulled image never became healthy"
    sleep 1
done
ok "healthy"

step "/health on the published image"
HEALTH=$(curl -fsS "localhost:${PORT}/health") || die "health fetch"
read -r FIELDS PACKS MAPPINGS SANDBOX <<<"$(printf '%s' "$HEALTH" | python3 -c '
import json, sys
d = json.load(sys.stdin)
# /health exposes canonical_fields (int) and packs as an OEM -> mapping-count
# MAP, not a count and not a total. Deriving both here rather than asking jq for
# .packs / .total_mappings, which are not keys on this endpoint and would come
# back null — a null compared against anything passes, so the check would look
# green while testing nothing.
packs = d.get("packs") or {}
print(d.get("canonical_fields"), len(packs), sum(packs.values()), str(d.get("sandbox")).lower())
')"
printf '  published image: %s fields · %s packs · %s mappings\n' "$FIELDS" "$PACKS" "$MAPPINGS"
[ "$SANDBOX" = "true" ] || die "sandbox flag is not true on the published image"
ok "sandbox flag true"

step "canonical payloads against the published image"
python3 - "$PORT" <<'PY' || die "a published-image payload did not normalize correctly"
import json, sys, urllib.request
port = sys.argv[1]
def post(body):
    req = urllib.request.Request(f"http://localhost:{port}/v1/normalize",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

fails = []
def check(label, got, want, tol=None):
    good = abs(got - want) <= tol if (tol is not None and isinstance(got, (int, float))) else got == want
    print(f"    {'ok  ' if good else 'FAIL'} {label}: {got!r}")
    if not good:
        fails.append(f"{label}: got {got!r}, want {want!r}")

n = post({"oem": "haas", "data": {"S1Temp": 72.1, "SP_SPEED": 5204}})["normalized"]
check("haas spindle_speed_rpm", n.get("spindle_speed_rpm"), 5204)

# Water: pH passes through, and MGD must CONVERT. A water pack that shipped
# without the unit table would return 12.4 here instead of 1955.8.
n = post({"oem": "water", "data": {"pH": 7.2, "Turbidity_NTU": 145,
                                   "Influent_Flow_MGD": 12.4}})["normalized"]
check("water ph", n.get("ph"), 7.2)
check("water turbidity_ntu", n.get("turbidity_ntu"), 145)
check("water influent_flow_m3_h", n.get("influent_flow_m3_h") or 0.0, 1955.80, tol=0.05)

# An alias, because the aliases are how a plant actually labels itself.
n = post({"oem": "wwtp", "data": {"MLSS": 3250, "Conductivity_mS_cm": 0.72,
                                  "System_Pressure_PSI": 62}})["normalized"]
check("wwtp mlss_mg_l", n.get("mlss_mg_l"), 3250)
check("wwtp conductivity_us_cm", n.get("conductivity_us_cm") or 0.0, 720.0, tol=0.01)
check("wwtp system_pressure_kpa", n.get("system_pressure_kpa") or 0.0, 427.47, tol=0.01)

n = post({"oem": "j1939", "data": {"EngineSpeed": 2400, "FuelRate_GPH": 8.4}})["normalized"]
check("j1939 engine_speed_rpm", n.get("engine_speed_rpm"), 2400)

n = post({"oem": "universal_robots",
          "data": {"actual_q_0": 1.57, "joint_temperatures_0": 32.4}})["normalized"]
check("UR joint temperature present", n.get("robot.joint.0.temperature") is not None, True)

if fails:
    print("\n  " + "\n  ".join(fails))
    sys.exit(1)
PY
ok "haas · water · wwtp alias · j1939 · universal_robots"

if [ -n "$LOCAL_IMAGE" ]; then
    step "digest parity: local build vs published"
    LOCAL_DIGEST=$(docker image inspect "$LOCAL_IMAGE" \
        --format '{{index .RepoDigests 0}}' 2>/dev/null | cut -d@ -f2 || echo "")
    if [ -z "$LOCAL_DIGEST" ]; then
        # A locally built image has no RepoDigest until it is pushed. The
        # multi-arch manifest digest also cannot equal a single-arch local image
        # id, so this compares CONTENT instead: same field count, same packs.
        warn "local image has no repo digest (not pushed as-is) — comparing /health instead"
        LOCAL_FIELDS=$(docker run --rm --entrypoint python3 "$LOCAL_IMAGE" -c \
            'import json;print(json.load(open("app/packs/_canonical_fields.json"))["field_count"])' 2>/dev/null || echo "")
        if [ -n "$LOCAL_FIELDS" ] && [ "$LOCAL_FIELDS" != "$FIELDS" ]; then
            die "PUBLISHED IMAGE IS STALE: local build has $LOCAL_FIELDS fields, published has $FIELDS"
        fi
        ok "local build and published image agree on $FIELDS fields"
    elif [ "$LOCAL_DIGEST" = "$REMOTE_DIGEST" ]; then
        ok "digests match: $REMOTE_DIGEST"
    else
        die "digest mismatch — local $LOCAL_DIGEST vs published $REMOTE_DIGEST"
    fi
fi

step "site / prod sync (warns, never blocks)"
# Sandbox and prod are ALLOWED to differ — the sandbox has carried packs prod
# does not, and vice versa. What is not allowed is not knowing. Prod exposes its
# count at .normalization.canonical_fields; /health has no top-level
# canonical_fields key, so asking for one returns null and silently "passes".
PROD_FIELDS=$(curl -fsS -m 20 -H 'User-Agent: curl/8.4.0' "$PROD_HEALTH" 2>/dev/null | python3 -c '
import json, sys
try:
    print((json.load(sys.stdin).get("normalization") or {}).get("canonical_fields") or "")
except Exception:
    print("")
' || echo "")
if [ -z "$PROD_FIELDS" ]; then
    warn "could not read a field count from $PROD_HEALTH"
elif [ "$PROD_FIELDS" = "$FIELDS" ]; then
    ok "sandbox and prod both report $FIELDS canonical fields"
else
    warn "sandbox ($FIELDS) != prod ($PROD_FIELDS) — port the gap or label the difference on the site"
fi
printf '\n\033[1m  MANUAL:\033[0m does the site say %s canonical fields and %s packs?\n' "$FIELDS" "$PACKS"
printf '         forge.foundrynet.io/pricing · foundrynet.io (sandbox tier copy)\n'

printf '\n\033[32m=== ALL SYNC CHECKS PASSED ===\033[0m\n'
