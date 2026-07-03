#!/usr/bin/env bash
# start-queen.sh — 여왕개미 시스템 전체 + WebUI를 한 번에 기동한다.
#
# 사용법:
#   ./start-queen.sh            (또는  bash start-queen.sh)
#
# 띄우는 것:
#   - 여왕개미 게이트웨이      http://127.0.0.1:8900   (/queen/chat, /v1)
#   - Research Sub            127.0.0.1:8901
#   - Coder Sub              127.0.0.1:8902
#   - nanobot WebUI          http://127.0.0.1:8765   (로그인 없음)
#   - Telegram Queen Runner  @queen_nanobot → http://127.0.0.1:8900/queen/chat
#
# 전제: 이 저장소의 .venv 에 nanobot 설치됨 + 1회 Codex 로그인 완료
#       (nanobot provider login openai-codex)
#
# 매 실행마다 기존 프로세스를 정리하고 새로 띄운다. 워크스페이스(~/.nbq-*)는
# 보존되므로 이전 대화 기억은 이어진다.

set -uo pipefail

# ── 0. 환경 적용 (가장 먼저) ───────────────────────────────────────────────
REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"
# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"

WEBUI_DIR="$HOME/.nbq-webui-test"

wait_health() {  # $1=url  $2=label
  for _ in $(seq 1 40); do
    if curl -s -m 2 "$1" >/dev/null 2>&1; then echo "  ✓ $2"; return 0; fi
    sleep 0.5
  done
  echo "  ✗ $2 (health 응답 없음 — 로그 확인)"; return 1
}

echo "[0/5] 기존 프로세스 정리..."
pkill -f "nanobot.queen.gateway" 2>/dev/null || true
pkill -f "nanobot.queen.telegram_runner" 2>/dev/null || true
pkill -f "nanobot gateway"       2>/dev/null || true
pkill -f "nanobot serve"         2>/dev/null || true
sleep 1
# NOTE: 레지스트리/키는 보존한다 — 생성·설정한 Sub가 재기동 때 그대로 복원되도록.

echo "[1/5] 여왕개미 게이트웨이 (8900)..."
QUEEN_GATEWAY_HOST="127.0.0.1" \
QUEEN_GATEWAY_PORT="8900" \
QUEEN_GATEWAY_MODEL="openai-codex/gpt-5.5" \
QUEEN_GATEWAY_KEYS_FILE="$HOME/.nbq-core/keys.json" \
QUEEN_GATEWAY_USER_KEYS="${QUEEN_GATEWAY_USER_KEYS:-me:user-key}" \
QUEEN_GATEWAY_USAGE_LOG="$HOME/.nbq-core/usage.jsonl" \
QUEEN_GATEWAY_MAX_CONCURRENCY="8" \
  nohup python -m nanobot.queen.gateway > /tmp/nbq-gw.log 2>&1 &
wait_health "http://127.0.0.1:8900/health" "gateway 8900"

echo "[2/5] 등록된 Sub 복원 (설정·기억 보존, 최근 5개까지)..."
python - <<'PY'
from nanobot.queen.registry import SubRegistry
from nanobot.queen.factory import SubFactory, SpawnSpec
from nanobot.queen.fleet import FleetManager
reg = SubRegistry()
fleet = FleetManager(SubFactory(reg))
if not reg.list():
    # 첫 실행 등 레지스트리가 비었을 때만 기본 Sub 시드
    for spec in [
        SpawnSpec(role="research", capability=["research.web", "research.summary"], mode="always", port=8901),
        SpawnSpec(role="coder",    capability=["code.write", "code.review"],        mode="always", port=8902),
    ]:
        r = fleet.spawn(spec)
        print(f"  seed {r['sub_id']}: {r['action']} port={r['port']} healthy={r['healthy']}")
else:
    for sub_id, how in fleet.restore_all():
        print(f"  {sub_id}: {how}")
PY

echo "[3/5] nanobot WebUI (8765, 비밀번호 없음)..."
mkdir -p "$WEBUI_DIR"
cat > "$WEBUI_DIR/config.json" <<'JSON'
{
  "modelPresets": { "codex": { "provider": "openai_codex", "model": "openai-codex/gpt-5.5" } },
  "agents": { "defaults": { "modelPreset": "codex" } },
  "channels": { "websocket": { "enabled": true, "websocketRequiresToken": false } }
}
JSON
nohup nanobot gateway --config "$WEBUI_DIR/config.json" --workspace "$WEBUI_DIR" \
  > /tmp/nbq-webui-gw.log 2>&1 &
wait_health "http://127.0.0.1:8765/" "webui 8765"

echo "[4/5] Telegram Queen Runner (@queen_nanobot)..."
QUEEN_GATEWAY_URL="http://127.0.0.1:8900" \
QUEEN_USER_KEY="${QUEEN_USER_KEY:-user-key}" \
  nohup python -m nanobot.queen.telegram_runner > /tmp/nbq-telegram.log 2>&1 &
sleep 2
if pgrep -f "nanobot.queen.telegram_runner" >/dev/null 2>&1; then
  echo "  ✓ telegram runner"
else
  echo "  ✗ telegram runner (기동 실패 — /tmp/nbq-telegram.log 확인)"
fi

echo "[5/5] 준비 완료."
echo
echo "  🌐 WebUI    : http://127.0.0.1:8765        (브라우저에서 열기 · 로그인 없음)"
echo "  💬 Telegram : @queen_nanobot               (Queen gateway 경유)"
echo "  💬 CLI 대화 : QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key python -m nanobot.queen.cli"
echo "  📋 Sub 목록 : python -m nanobot.queen.registry list"
echo "  📊 지표     : python -m nanobot.queen.usage"
echo
echo "  ⚠️  주의: WebUI(8765)는 아직 순수 nanobot이라 여왕개미 라우팅·Sub·라벨이 적용되지 않습니다"
echo "      (단일 에이전트가 Codex로 직접 응답). 여왕개미 대화는 위 CLI를 쓰세요."
echo
echo "  🛑 전체 종료: pkill -f 'nanobot.queen.gateway'; pkill -f 'nanobot.queen.telegram_runner'; pkill -f 'nanobot gateway'; pkill -f 'nanobot serve'"
