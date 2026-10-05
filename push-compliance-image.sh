#!/usr/bin/env bash
# Build and publish forge-sandbox with LICENSE + THIRD_PARTY_NOTICES.
#
# Blocked on one thing only: GHCR push needs the `write:packages` scope, and the
# token `gh auth login` issues does not carry it. Verified 2026-09-18:
#
#   docker login ghcr.io          -> Login Succeeded      (misleading: login != push)
#   docker push ghcr.io/...       -> denied: permission_denied:
#                                    The token provided does not match expected scopes.
#
# Grant the scope first, either way works:
#
#   gh auth refresh -h github.com -s write:packages -s read:packages
#     (adds the scope to the existing login; nothing to copy or paste)
#
#   -- or, with a classic PAT that has write:packages --
#   echo "$YOUR_PAT" | docker login ghcr.io -u FoundryNet --password-stdin
#     (credential is stored by Docker; it never appears in a transcript)
#
# Then: ./push-compliance-image.sh
set -euo pipefail

IMAGE="ghcr.io/foundrynet/forge-sandbox"
PLATFORMS="linux/amd64,linux/arm64"
cd "$(dirname "$0")"

echo "==> test suite"
python3 -m pytest tests/ -q -p no:warnings

echo "==> preflight: the files that made this build necessary"
test -f LICENSE || { echo "LICENSE missing"; exit 1; }
test -f THIRD_PARTY_NOTICES.md || { echo "THIRD_PARTY_NOTICES.md missing"; exit 1; }
grep -q "COPY LICENSE THIRD_PARTY_NOTICES.md" Dockerfile \
  || { echo "Dockerfile does not copy the notices into the image"; exit 1; }

echo "==> preflight: no settlement vocabulary in what actually SHIPS"
# Check the built image, not the source tree. The source tree carries .bak files
# that .dockerignore already excludes, and "anchored" appears legitimately in
# unit_converter.py talking about regex anchoring — scanning the source produces
# false positives in both directions. The image is the artifact people receive,
# so the image is what gets checked.
docker build --target runtime -t forge-sandbox:preflight . >/dev/null
if docker run --rm --entrypoint sh forge-sandbox:preflight \
     -c "grep -ril 'on-chain\|solana\|mint_id' /app/app/ /app/*.md 2>/dev/null"; then
  echo "FAIL: the files listed above ship with settlement terminology"; exit 1
fi
docker run --rm --entrypoint sh forge-sandbox:preflight \
  -c "test -f /app/LICENSE && test -f /app/THIRD_PARTY_NOTICES.md" \
  || { echo "FAIL: notices did not make it into the image"; exit 1; }
docker rmi forge-sandbox:preflight >/dev/null 2>&1 || true
echo "    clean"

echo "==> auth check"
# Check the credential store. Do NOT re-run `docker login --password-stdin </dev/null`:
# that sends an empty password and always fails, so it reports "not logged in" even
# when a perfectly good credential is cached. (It did exactly that on first run.)
if ! python3 - <<'EOF'
import base64, json, os, sys
p = os.path.expanduser("~/.docker/config.json")
try:
    cfg = json.load(open(p))
except Exception:
    sys.exit(1)
sys.exit(0 if any("ghcr.io" in k for k in (cfg.get("auths") or {})) else 1)
EOF
then
  echo "    no ghcr.io credential found — see the header of this script"; exit 1
fi

echo "==> buildx multi-arch build + push ($PLATFORMS)"
docker buildx build \
  --platform "$PLATFORMS" \
  --target runtime \
  --tag "$IMAGE:latest" \
  --push .

echo "==> verify the published image, pulled fresh"
docker rmi "$IMAGE:latest" >/dev/null 2>&1 || true
docker pull "$IMAGE:latest"
docker run --rm --entrypoint ls "$IMAGE:latest" -la /app/LICENSE /app/THIRD_PARTY_NOTICES.md
echo "==> done: $IMAGE:latest"
