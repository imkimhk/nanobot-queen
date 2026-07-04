#!/usr/bin/env bash
# Queen Core boot — Model Gateway + an always-on Research Sub (created from
# scratch via the factory), registered in ~/.nbq-core/subs.json.
#
# Prereq: Codex OAuth already logged in (nanobot provider login openai-codex).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
source "$REPO/.venv/bin/activate"

GW_LOG="/tmp/nbq-gw.log"

wait_health() {  # $1=url $2=name
  for _ in $(seq 1 30); do
    if curl -s -m 2 "$1" >/dev/null 2>&1; then echo "  ✓ $2 healthy"; return 0; fi
    sleep 0.5
  done
  echo "  ✗ $2 did not become healthy" >&2; return 1
}

echo "[1/3] Starting Model Gateway (Core) on 127.0.0.1:8900 ..."
QUEEN_GATEWAY_HOST="127.0.0.1" \
QUEEN_GATEWAY_PORT="8900" \
QUEEN_GATEWAY_MODEL="openai-codex/gpt-5.5" \
QUEEN_GATEWAY_KEYS_FILE="$HOME/.nbq-core/keys.json" \
QUEEN_GATEWAY_USER_KEYS="${QUEEN_GATEWAY_USER_KEYS:-me:user-key}" \
QUEEN_GATEWAY_USAGE_LOG="$HOME/.nbq-core/usage.jsonl" \
QUEEN_GATEWAY_MAX_CONCURRENCY="8" \
  nohup python -m nanobot.queen.gateway > "$GW_LOG" 2>&1 &
GW_PID=$!
echo "  gateway pid=$GW_PID (log: $GW_LOG)"
wait_health "http://127.0.0.1:8900/health" "gateway"

echo "[2/3] Spawning always-on Research Sub (factory creates ~/.nbq-research) ..."
python - <<'PY'
from nanobot.queen.registry import SubRegistry
from nanobot.queen.factory import SubFactory, SpawnSpec
from nanobot.queen.lifecycle import OnDemandManager
mgr = OnDemandManager(SubFactory(SubRegistry()))
res = mgr.ensure(SpawnSpec(
    role="research", capability=["research.web", "research.summary"],
    mode="always", port=8901,
))
print(f"  research: {res.action} port={res.port} healthy={res.healthy} status={res.status}")
PY

echo "[3/3] Registry contents:"
python -m nanobot.queen.registry list

echo ""
echo "Core booted. Gateway pid=$GW_PID. Add more Subs via the factory; chat via"
echo "  QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key python -m nanobot.queen.cli"
echo "Stop with: pkill -f 'nanobot.queen.gateway'; pkill -f 'nanobot serve'"
