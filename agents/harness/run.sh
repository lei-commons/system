#!/usr/bin/env bash
# Runs a command inside the agent sandbox, with the checkout at /workspace.
#   agents/harness/run.sh <checkout> <command...>
# The agent's network has no route out. Its only exit is the egress container.
set -euo pipefail

CHECKOUT="$(cd "${1:?usage: run.sh <checkout> <command...>}" && pwd)"
shift
IMAGE="${LC_SANDBOX_IMAGE:-lc-sandbox}"
SEALED=lc-sandbox-sealed
OUTSIDE=lc-sandbox-outside
EGRESS=lc-sandbox-egress

docker network inspect "$SEALED" >/dev/null 2>&1 || docker network create --internal "$SEALED" >/dev/null
docker network inspect "$OUTSIDE" >/dev/null 2>&1 || docker network create "$OUTSIDE" >/dev/null

if ! docker inspect "$EGRESS" >/dev/null 2>&1; then
  docker run -d --name "$EGRESS" --network "$OUTSIDE" --cap-drop ALL --read-only \
    --security-opt no-new-privileges "$IMAGE" python -m lc_sandbox.egress >/dev/null
  docker network connect "$SEALED" "$EGRESS"
fi

docker run --rm --network "$SEALED" \
  --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
  --read-only --tmpfs /tmp --memory 4g --pids-limit 512 \
  -v "$CHECKOUT:/workspace" -w /workspace \
  -e HTTPS_PROXY="http://$EGRESS:3128" -e HTTP_PROXY="http://$EGRESS:3128" -e NO_PROXY="" \
  "$IMAGE" "$@"
