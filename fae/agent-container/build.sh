#!/usr/bin/env bash
# Build the agent base image: the three agent CLIs, git, python3 — the
# engine's own, whatever the experiment. An experiment's layer over it
# (its SDK, its tools) is built by the engine (fae/cell/image.py) from
# the Dockerfile directory its definition declares.
#
#   bash fae/agent-container/build.sh
#
# Env: AGENT_BASE_IMAGE (default fae-agent:latest),
#      CLAUDE_CODE_VERSION / OPENCODE_VERSION / AGY_VERSION (Dockerfile ARGs).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

IMAGE="${AGENT_BASE_IMAGE:-fae-agent:latest}"
echo "building $IMAGE ..."
args=()
for v in CLAUDE_CODE_VERSION OPENCODE_VERSION AGY_VERSION; do
  [[ -n "${!v:-}" ]] && args+=(--build-arg "$v=${!v}")
done
docker build ${args[@]+"${args[@]}"} -t "$IMAGE" .
echo "built $IMAGE"
