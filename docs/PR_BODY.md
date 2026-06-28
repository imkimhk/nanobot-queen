# 여왕개미 멀티에이전트 오케스트레이션 (Queen multi-agent orchestration)

## 요약 (Summary)

nanobot 0.2.2 fork 위에 **추가 모듈만으로** 여왕개미 아키텍처를 올린다. Core(여왕)가 Codex OAuth를
독점하고, 전문 Sub(별도 nanobot serve 인스턴스)는 표준 OpenAI로 Core 게이트웨이를 거쳐 동작한다.

- **Core 모델 게이트웨이** — Sub→Core→Codex 중계. 사전공유 키 검증(무효 키는 upstream 전 차단),
  사용량 로깅, 동시성 상한·429 백오프, keystore 핫리로드.
- **Sub 생애주기** — 레지스트리(JSON) + 팩토리(역할/capability → 워크스페이스·고유키·고유포트·serve·
  등록·health) + 온디맨드 생성/유휴 종료 + 역할 조정(draft→승인→apply, 금지패턴 필터, 롤백).
- **User↔Sub 직접 대화** — `/queen/chat` 라우팅(rule-first → 모호 시 LLM). 단일 Sub면 패스스루
  (Core LLM 0토큰), 다중이면 Core가 통합. 응답 주체(sub_id) 라벨 표시. CLI 라이브.
- **빈 'idea' Sub 패러다임** — 미리 정의된 전문가 대신, 빈 idea Sub를 띄우고 **자연어로 작동 방식을
  주입**. 하드 경계(툴/범위/게이트웨이)는 자연어로 변경 불가, 작동 스타일만 바꾼다.

## 검증 흐름 (각 단계 실측 1줄)

| 단계 | 결과 |
|---|---|
| **PoC-A** Sub→Core→Codex 1왕복 | 경로 성립. 모델명(gpt-5.5)·다중 메시지 relay 블로커 2건 해결 |
| **PoC-B** 게이트웨이 키 검증 | 무효 키 401(토큰 0 소모), 다중 메시지 relay 200, usage 귀속 |
| **PoC-C** 재기동 기억 보존 | `<workspace>/sessions/`로 영속 → 재기동 후 기억 유지, 새 워크스페이스는 격리 |
| **PART1** 오케스트레이션+게이트웨이 | rule-first 분기·task_id·동시성·429 백오프 |
| **PART1** 레지스트리 + Research 상시 | 등록·capability 경계(OUT_OF_SCOPE) 실증 |
| **PART2** 팩토리 자동 생성 | allowlist·고유키·고유포트, 생성→health→1왕복 라이브 |
| **PART2** 역할 조정 + 안전장치 | 홈유지 기억보존·격리, 금지패턴 차단, 롤백 |
| **PART2** 통합 메모리 + 온디맨드 | 중요 결과 Core 승격(격리에도 보존), 유휴 종료→재기동 기억 연속 |
| **효율** 동시성/캐싱 측정 | 호출당 ~8.8k 고정 프롬프트(토큰 99.6%). **Codex 구독은 캐싱 미적용**(cached_tokens=0) → 툴 가지치기로 8,395→6,552(~22%↓) |
| **STEP10** User↔Sub 직접대화 + 라벨 | 단일 passthrough 0토큰/~3s, 모호 정확 위임, 다중 통합, `[Research]`/`↪ A → B` 라벨, 채널↔게이트웨이 글루 라이브 |
| **idea 1단계** 빈 idea Sub | `/spawn idea`로 생성, 툴 0개(file/exec/web 차단), 코드 요청 → OUT_OF_SCOPE |
| **idea 2단계** 자연어 작동방식 주입 | draft→승인→apply, 주입 후에도 경계 유지, injection 차단, 롤백 |
| **subagent 우회 차단(B1)** | 서브에이전트가 부모의 cli_apps 차단을 미상속하던 버그 수정 → 하드 경계가 서브에이전트까지 전파 |
| **자연어 설정 UX** | 슬래시 최소화(/spawn·/subs·/stop). 설정·승인·롤백을 자연어로(안전장치 0 변경) |

## 품질

- **원본 nanobot src 수정: 1파일** — `nanobot/agent/subagent.py`의 B1 버그픽스(서브에이전트가 부모의
  exec/web/file은 상속하면서 cli_apps만 빠뜨린 누락 수정). 그 외 전부 `nanobot/queen/` 신규 모듈 +
  `tests/queen/` + `docs/`. (B1은 HKUDS/nanobot 업스트림 PR 기여 후보.)
- **pytest 4,617 passed, 3 skipped — 회귀 0.**
- `ruff` 통과. 39 files changed, +5,424 / -1.

## 신규 모듈 (`nanobot/queen/`, 14개)

`gateway.py`(두 표면 /v1·/queen/chat) · `registry.py` · `factory.py` · `adjuster.py` · `memory.py` ·
`lifecycle.py` · `orchestrator.py` · `chat.py` · `bridge.py` · `labels.py` · `cli.py` · `usage.py` ·
`idea_style.py`(자연어 작동방식 주입) · `admin_nl.py`(자연어 설정 의도 인식)
— 각 모듈 단위테스트(`tests/queen/test_*.py` 13파일). 문서: `docs/queen/architecture.md`, `docs/queen/runbook.md`.

## 알려진 한계 (Known limitations)

- **다중 작업 지연**: 다중 Sub 조율은 분류+N Sub+통합의 순차 호출이라 최대 ~145초. 다중 빈도가
  높으면 스트리밍/진행표시가 다음 개선 후보.
- **프롬프트 캐싱 불가**: ChatGPT 구독 Codex는 캐싱을 적용/할인하지 않음(측정 확정). 구독은 정액이라
  캐싱 대신 프롬프트 축소(툴 가지치기)가 레버.
- **서브에이전트 소프트 경계 미상속**: B1로 툴(하드 경계)은 막혔으나, 서브에이전트는 부모 역할 프롬프트
  경계를 상속하지 않아 코드를 *텍스트로* 써주는 약한 누수는 가능(실행은 불가). 완전 격리(spawn config
  게이트 + 경계 상속)는 미구현 — 실사용에서 문제가 되면 검토.
- **WebUI 미연결**: nanobot 내장 WebUI는 정상 기동되나(별도), 여왕개미 라우팅·라벨 연결은 미구현.
- **자연어 설정 구분**: 규칙 기반(토큰 0)이라 드문 표현은 오분류 가능. 단 **승인 게이트가 안전망** —
  잘못 분류돼도 승인 없이 적용되는 일은 없다. idea Sub에 한정.

## 배포/보안 메모

- Core API는 `127.0.0.1` 바인딩. 실제 Codex OAuth 토큰은 Core 머신 세션에만; Sub는 비민감 사전공유
  키만 보유(repo의 `poc-key`/`user-key`는 데모 placeholder, env로 교체).
- 라이브 산출물(`~/.nbq-*`, usage/keystore/sessions)은 `$HOME`(repo 밖)에 저장 — repo에 커밋되지
  않으며, `.gitignore`에도 방어적으로 등록됨.

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)
