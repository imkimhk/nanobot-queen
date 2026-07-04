#!/usr/bin/env bash
# PoC-A — Sub1 instance: serve OpenAI-compatible API on 127.0.0.1:8901
# Provider = custom (OpenAI-compatible) pointing at Core's /v1. No OAuth here.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
source "$REPO/.venv/bin/activate"

exec nanobot serve \
  --config "$HOME/.nbq-sub1/config.json" \
  --workspace "$HOME/.nbq-sub1" \
  --host 127.0.0.1 \
  --port 8901 \
  --verbose
