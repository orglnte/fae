# The agent base image — the SEALED environment a coding agent runs in.
#
# The engine's own: the three agent CLIs (claude, opencode, agy), git (the
# workspace is a git repo) and python3 (the testagent, and any experiment's
# read-only SDK). What an experiment's agents need on top — a manifest
# linter, a cluster client, an SDK — is the experiment's layer, `FROM` this
# image (Definition.AGENT_IMAGE_DIR; fae/cell/image.py builds it).
# It has NO docker socket, NO kubeconfig, NO cluster/store access. The only
# mounts at run time are the workspace (rw) and the agent's creds home
# (rw). Everything else — seeds, engine, other cells — is simply not present
# in this filesystem.
FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI.
ARG CLAUDE_CODE_VERSION=2.1.261
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# opencode — the OSS-model agent CLI (deepseek/kimi treatments via OpenRouter).
# Its config is mounted read-only at ~/.config/opencode at run time; its
# mutable state (~/.local/share/opencode) stays in the container and dies
# with --rm, so sessions cannot leak across cells.
ARG OPENCODE_VERSION=1.18.29
RUN npm install -g opencode-ai@${OPENCODE_VERSION} && opencode --version

# Run as the base image's non-root `node` user (uid 1000); its HOME holds the
# mounted creds dir.
USER node
WORKDIR /workspace
ENV HOME=/home/node
# Seed a minimal config so the CLI does not spam "configuration file not found"
# on every headless run (the OAuth creds are mounted at ~/.claude at run time).
RUN printf '{}\n' > /home/node/.claude.json

# Antigravity CLI (agy) — the Gemini coding agent, for the Gemini treatment.
# Official installer detects the platform (linux arm64/amd64) and drops the
# binary at ~/.local/bin/agy. Its ~/.gemini creds are mounted at run time.
# AGY_VERSION is the installer's current release; it only busts this layer's
# cache, the installer always fetches its latest.
ARG AGY_VERSION=1.1.27
RUN echo "agy ${AGY_VERSION}" && curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && test -x /home/node/.local/bin/agy && /home/node/.local/bin/agy --version
ENV PATH="/home/node/.local/bin:${PATH}"

# The command (claude -p ... | agy --print ... | opencode run ...) is supplied
# by `docker run`; no ENTRYPOINT logic.
