# 여왕개미(Queen) 아키텍처

nanobot 0.2.2 fork 위에 **추가 모듈만으로**(원본 src 미수정) 올린 오케스트레이션 레이어.
Core(여왕)가 Codex OAuth를 독점하고, 전문 Sub들이 표준 OpenAI로 Core를 거쳐 동작한다.

```
                ┌──────────────────────────────────────────────┐
                │  Core (여왕개미) — 127.0.0.1                   │
   User ─┐      │                                              │
         │ /queen/chat (User→Sub, 라우팅)                       │
   CLI ──┼─────►│  ┌────────────┐   라우팅    ┌──────────────┐  │
 (bridge)│      │  │  Gateway   │───rule/llm─►│ Orchestrator │  │
Telegram─┘      │  │  :8900     │            │ (router)     │  │
                │  │            │   /v1 (Sub→Codex, 모델 중계)  │  │
                │  │            │◄───────────────────────────┐ │  │
                │  └─────┬──────┘                            │ │  │
                │        │ Codex OAuth (Core만 보유)          │ │  │
                │        ▼                                   │ │  │
                │   ChatGPT/Codex (gpt-5.5)                  │ │  │
                └────────────────────────────────────────────┘ │
                          ▲ HTTP /v1                            │
                          │                                     │
        ┌─────────────────┼──────────────┬──────────────────┐  │
   Sub: research(:8902)  coder(:8903)   …  (full nanobot serve 인스턴스)
        각 Sub는 provider=custom → Core /v1 + 고유 사전공유 키
```

## 두 표면 (Two surfaces)

| 표면 | 방향 | 인증 | LLM | 용도 |
|---|---|---|---|---|
| **`/v1/chat/completions`** | Sub → Core → Codex | Sub 키 (keystore) | Codex(모델) | Sub가 모델을 부른다. 다중 메시지 relay + 429 백오프 + 동시성 상한 |
| **`/queen/chat`** | User → Core → Sub | User 키 | 라우팅 시에만(LLM 분류/통합); 단일 Sub면 0 | User가 Sub와 대화. 라우팅·패스스루·responder 태깅 |

> `nanobot serve`의 기본 `/v1`은 **단일 user 메시지만** 허용(`server.py`)하므로 Sub→Core에는
> 쓸 수 없다. 그래서 Gateway가 다중 메시지 relay를 별도로 제공한다. (블로커 B 해결)

## 모듈 (`nanobot/queen/`)

| 모듈 | 책임 |
|---|---|
| `gateway.py` | 두 표면(`/v1`, `/queen/chat`, `/health`, `/v1/models`). 키 검증(무효 키는 upstream 전 401 차단), 사용량 로깅, 동시성 상한(429), 429 백오프, keystore 핫리로드 |
| `registry.py` | Sub 카탈로그(JSON, atomic): id·role·capability·port·workspace·status·mode·prompt_version·last_used·pid + CLI |
| `factory.py` | 입력(role/capability/skills/mode)→ 워크스페이스 복제 + 역할 프롬프트·provider·고유키·고유포트 주입 → serve 기동 → 등록 → health. allowlist 안전장치. **capability별 최소 툴셋**(프롬프트 토큰 절감) |
| `adjuster.py` | 역할/범위 조정. draft→사람승인→apply. 홈유지(기억보존)/격리. 금지패턴 필터(게이트웨이 우회·Sub 사칭·자격증명 유출), 이력+롤백 |
| `lifecycle.py` | 온디맨드 생성/유휴 종료(워크스페이스·sessions 보존), 재기동-기억연속(PoC-C). always-on 미종료 |
| `memory.py` | Core 통합 메모리 승격. ImportancePolicy(task_result/pattern/decision/fact). 격리에도 보존 |
| `orchestrator.py` | rule-first 라우팅(Router) + 위임(Orchestrator): task_id, gather, 단일 verbatim/다중 merge |
| `chat.py` | `/queen/chat` 배선: rule-first→(모호)LLM 분류→단일 패스스루/다중 통합. `SubForwarder`(Sub `/v1`로 포워딩) |
| `bridge.py` | 채널↔게이트웨이 글루. 버스 `InboundMessage` 소비 → `/queen/chat` → `OutboundMessage.metadata[responder_sub_id]` emit. `AgentLoop.run()` 형태 |
| `labels.py` | 응답 주체 라벨. CLI `[Research]`, Telegram `<b>[Research]</b>`, 전환 `↪ A → B` |
| `cli.py` | 표준 인터랙티브 CLI(추가 구현, 원본 무수정). 한 입력창 + 라벨 + 전환 + 토큰/지연 |
| `usage.py` | 사용량 집계(고정비용 추정) + 운영 지표(하루 토큰·rule/llm·단일/다중·rate-limit) |

## 라우팅 흐름 (`/queen/chat`)

1. **rule-first** (`orchestrator.Router`, 0 토큰): 명백한 도메인 키워드만. 단일 Sub 매칭 → 그 Sub로 패스스루(Core LLM 안 탐).
2. **모호** → Core LLM 분류기(정확도 우선, 결정 4). 단일 Sub 반환 → 패스스루.
3. **다중** Sub → Core가 각 Sub 호출 후 **LLM 통합**(결정 1).
4. **맞는 Sub 없음** → Core가 직접 답(`core_direct`).

responder(sub_id)는 `X-Responder-Sub-Id` 헤더 + JSON으로 반환(토큰 0 메타데이터) → 채널이 라벨 렌더.

## 핵심 실측 사실 (데이터로 확정)

- **Codex 구독은 프롬프트 캐싱 미지원**: 동일 14k prefix 반복에도 `cached_tokens=0`, `store=true`는 400. 구독은 정액(토큰 과금 아님). → 캐싱 대신 **시스템 프롬프트 축소**가 레버. 툴 가지치기로 research Sub 8,395→6,552 토큰(~22%↓).
- **단일 vs 다중 비용**: 단일 패스스루 routing_tokens=0/~3s. 다중은 분류+N Sub+통합 = 순차 다회 호출(예: 145s). → "단일은 직접, 다중만 Core"가 정당. 다중 빈도가 높으면 스트리밍/진행표시가 다음 후보.
- **메모리 경로**: Sub 작업기억은 `<workspace>/sessions/<session_id>.jsonl`. 워크스페이스(특히 `sessions/`) 보존 시 재기동에도 유지. Core 통합 메모리는 `~/.nbq-core/memory/promoted.jsonl`(격리에도 보존).

## 보안 경계

- Core API `127.0.0.1` 바인딩. Sub→Core·User→Core 모두 사전공유 키 검증, 무효 키는 upstream 전 차단.
- **Codex OAuth 토큰은 Core(머신 전역 OAuth 세션)에만**. Sub는 고유 사전공유 키만 보유. keystore `~/.nbq-core/keys.json`는 0600, repo 밖.
- 역할 조정 시 금지패턴 필터로 LLM 작성 프롬프트의 게이트웨이 우회·Sub 사칭·자격증명 유출 차단.
