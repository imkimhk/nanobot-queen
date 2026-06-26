#!/usr/bin/env bash
# Queen Core boot — starts the Model Gateway + always-on Research Sub,
# then registers Research in the Sub registry (~/.nbq-core/subs.json).
#
# Prereq: Codex OAuth already logged in (nanobot provider login openai-codex).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
source "$REPO/.venv/bin/activate"

GW_LOG="/tmp/nbq-gw.log"
RESEARCH_LOG="/tmp/nbq-research.log"
REGISTRY="$HOME/.nbq-core/subs.json"

wait_health() {  # $1=url $2=name
  for _ in $(seq 1 20); do
    if curl -s -m 2 "$1" >/dev/null 2>&1; then echo "  ✓ $2 healthy"; return 0; fi
    sleep 0.5
  done
  echo "  ✗ $2 did not become healthy" >&2; return 1
}

echo "[1/4] Starting Model Gateway (Core) on 127.0.0.1:8900 ..."
QUEEN_GATEWAY_HOST="127.0.0.1" \
QUEEN_GATEWAY_PORT="8900" \
QUEEN_GATEWAY_MODEL="openai-codex/gpt-5.5" \
QUEEN_GATEWAY_KEYS="sub1:poc-key,research:research-key" \
QUEEN_GATEWAY_KEYS_FILE="$HOME/.nbq-core/keys.json" \
QUEEN_GATEWAY_USAGE_LOG="$HOME/.nbq-core/usage.jsonl" \
QUEEN_GATEWAY_MAX_CONCURRENCY="8" \
  nohup python -m nanobot.queen.gateway > "$GW_LOG" 2>&1 &
GW_PID=$!
echo "  gateway pid=$GW_PID (log: $GW_LOG)"
wait_health "http://127.0.0.1:8900/health" "gateway"

echo "[2/4] Starting always-on Research Sub on 127.0.0.1:8901 ..."
nohup nanobot serve \
  --config "$HOME/.nbq-research/config.json" \
  --workspace "$HOME/.nbq-research" \
  --host 127.0.0.1 --port 8901 --verbose > "$RESEARCH_LOG" 2>&1 &
RESEARCH_PID=$!
echo "  research pid=$RESEARCH_PID (log: $RESEARCH_LOG)"
wait_health "http://127.0.0.1:8901/health" "research"

echo "[3/4] Registering Research in Sub registry ($REGISTRY) ..."
python -m nanobot.queen.registry --file "$REGISTRY" register \
  --id research \
  --role "리서치 전문가" \
  --capability research.web,research.summary \
  --port 8901 \
  --workspace "$HOME/.nbq-research" \
  --mode always \
  --prompt-version v1 \
  --status running \
  --pid "$RESEARCH_PID"

echo "[4/4] Registry contents:"
python -m nanobot.queen.registry --file "$REGISTRY" list

echo ""
echo "Core booted. Gateway pid=$GW_PID, Research pid=$RESEARCH_PID."
echo "Stop with: kill $GW_PID $RESEARCH_PID"
