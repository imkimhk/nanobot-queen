#!/usr/bin/env bash
# PoC-B — Core Model Gateway: OpenAI-compatible relay on 127.0.0.1:8900
# Replaces `nanobot serve` for Sub traffic. Requires Codex OAuth (machine-global)
# to already be logged in (nanobot provider login openai-codex).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
source "$REPO/.venv/bin/activate"

export QUEEN_GATEWAY_HOST="127.0.0.1"
export QUEEN_GATEWAY_PORT="8900"
export QUEEN_GATEWAY_MODEL="openai-codex/gpt-5.5"
# sub_id:psk pairs. Sub1 presents Bearer poc-key.
export QUEEN_GATEWAY_KEYS="sub1:poc-key"
export QUEEN_GATEWAY_USAGE_LOG="$HOME/.nbq-core/usage.jsonl"

exec python -m nanobot.queen.gateway
