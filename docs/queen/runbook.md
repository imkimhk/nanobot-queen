# 여왕개미 런북 — 처음부터 띄우고 대화하기

전제: 저장소 클론 완료, Python 3.11 venv(`.venv`)에 `pip install -e ".[api,dev]"` 끝남.
(설치는 [docs/cli-reference.md] 및 PoC 노트 참조.)

## 0. 단 한 번: Codex OAuth 로그인 (사람이 직접, 브라우저)

Core만 Codex 토큰을 갖는다. 머신 전역 1회.

```bash
cd /path/to/nanobot-queen && source .venv/bin/activate
nanobot provider login openai-codex      # 옵션 없음. 브라우저 인증 → ✓ Authenticated
```

## 1. Core 부팅 (게이트웨이 + 상시 Sub + 레지스트리 등록)

```bash
bash docs/poc/boot-core.sh
```
- Model Gateway를 `127.0.0.1:8900`에 띄운다(keystore 핫리로드, usage 로깅).
- 상시 Research Sub를 `8901`에 띄우고 레지스트리(`~/.nbq-core/subs.json`)에 등록.
- User 키 기본값 `me:user-key` (env `QUEEN_GATEWAY_USER_KEYS`로 변경).

추가 Sub(예: coder)를 더 띄우려면:
```bash
source .venv/bin/activate
python - <<'PY'
from nanobot.queen.registry import SubRegistry
from nanobot.queen.factory import SubFactory, SpawnSpec
from nanobot.queen.lifecycle import OnDemandManager
m = OnDemandManager(SubFactory(SubRegistry()))
print(m.ensure(SpawnSpec(role="coder", capability=["code.write","code.review"], mode="always")))
PY
python -m nanobot.queen.registry list      # 현재 Sub 확인
```

건강 확인:
```bash
curl -s http://127.0.0.1:8900/health        # gateway
curl -s http://127.0.0.1:8901/health        # research
```

## 2. 대화 (표준 Queen CLI — 한 입력창 + 응답 주체 라벨)

```bash
source .venv/bin/activate
QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key \
  python -m nanobot.queen.cli
```
- 입력하면 라우팅되어 적절한 Sub가 답하고, `[Research]`/`[Coder]`/`[Core]` 라벨이 붙는다.
- Sub가 바뀌면 `↪ Research → Coder` 전환 표시.
- 각 턴에 `routing=rule/llm`, `sub_tokens`, `routing_tokens`, `latency_ms` 표시.
- `exit`로 종료.

한 번에 검증(스크립트 모드):
```bash
QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key \
  python -m nanobot.queen.cli "개미 페로몬 한 문장 요약" "파이썬 add 함수 구현"
```

직접 HTTP로 쓰려면:
```bash
curl -s http://127.0.0.1:8900/queen/chat \
  -H "Authorization: Bearer user-key" -H "Content-Type: application/json" \
  -d '{"message":"개미 페로몬 한 문장 요약","session_id":"s1"}' | python -m json.tool
# 응답 헤더 X-Responder-Sub-Id 에 응답 Sub 표시
```

## 3. 운영 지표 보기 (실사용 중 관찰)

```bash
source .venv/bin/activate
python -m nanobot.queen.usage              # 기본 ~/.nbq-core/usage.jsonl
```
출력: 하루 누적 토큰, 라우팅 rule/llm 비율, 단일/다중 비율, rate-limit 도달(동시성/upstream 429).
→ 이 지표가 다음 우선순위(Telegram vs 토큰 최적화)를 정하는 근거.

## 4. 종료 / 재시작

```bash
pkill -f "nanobot.queen.gateway"; pkill -f "nanobot serve"   # 전부 종료
```
워크스페이스(`~/.nbq-research` 등)는 보존되므로 다시 `boot-core.sh` 하면 **기억이 이어진다**
(세션 히스토리 = `<workspace>/sessions/`). on-demand Sub는 유휴 시 자동 종료해도 재기동 시 연속.

## 5. (선택) Telegram 붙이기

> 현재 응답 주체 라벨의 Telegram 렌더(`nanobot/queen/labels.py: render_telegram`)는 구현·단위테스트
> 완료. 라이브 연결은 다음 단계이며, 표준 nanobot 텔레그램 채널 위에 bridge를 얹는 방식이다.

1. Telegram 봇 토큰 발급(@BotFather) → `~/.nanobot/config.json`의 채널 설정에 등록(표준 nanobot 방식,
   [docs/chat-apps.md] 참조).
2. Queen bridge를 텔레그램 채널 버스에 연결(= `QueenBridge`가 `channel="telegram"` InboundMessage를
   소비하도록 구동). 라벨은 `render_telegram(responder, content, prev=...)`로 `<b>[Coder]</b>` +
   `↪ <i>A → B</i>` HTML 접두사가 붙는다.
3. 라이브 검증은 CLI와 동일 기준(단일 passthrough, 모호 위임, 전환 표시, 토큰/지연).

자세한 라이브 절차는 Telegram 단계 착수 시 별도 문서로 추가한다.
