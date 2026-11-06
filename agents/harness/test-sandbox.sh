#!/usr/bin/env bash
# Proves the sandbox for real: builds it, then tries to escape from inside.
set -euo pipefail

docker build -q -f agents/harness/Dockerfile -t lc-sandbox . >/dev/null
agents/harness/run.sh . python -m lc_sandbox.selfcheck
agents/harness/run.sh . python -c 'from anthropic import AnthropicVertex; print("vertex client imports")'
echo "egress log:"
docker logs lc-sandbox-egress
