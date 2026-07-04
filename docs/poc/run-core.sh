#!/usr/bin/env bash
# PoC-A — Core instance: serve OpenAI-compatible API on 127.0.0.1:8900
# Provider = openai_codex (OAuth). You MUST run the OAuth login first (see poc-a.md).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# Use the project venv (python3.11). Adjust if you installed elsewhere.
source "$REPO/.venv/bin/activate"

exec nanobot serve \
  --config "$HOME/.nbq-core/config.json" \
  --workspace "$HOME/.nbq-core" \
  --host 127.0.0.1 \
  --port 8900 \
  --verbose
