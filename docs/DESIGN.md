# 여왕개미(Queen) 설계서 — 최종본 (설계 의도 → 실제 구현)

nanobot 0.2.2 fork 위에 **추가 모듈만으로**(원본 src는 B1 버그픽스 1파일 제외) 올린 멀티에이전트
오케스트레이션. 이 문서 하나로 전체 설계와 구현 상태를 파악할 수 있다(다른 문서 참조 불필요).
사용법은 [USAGE.md](USAGE.md) 참고.

---

## 1. 비전과 핵심 결정

**여왕개미**: Core(여왕)가 Codex OAuth를 독점하고, 전문 Sub(일개미)들을 낳고·부린다. 각 Sub는
원본 nanobot의 풀 인스턴스(별도 `nanobot serve` 프로세스)이며, 표준 OpenAI로 Core 게이트웨이를
거쳐 모델을 쓴다.

확정된 설계 결정:
1. **단일 OAuth 통제** — 실제 Codex 토큰은 Core 머신 세션에만. Sub는 비민감 사전공유 키만 보유.
2. **별도 프로세스 풀 인스턴스** — Sub는 경량 내장 subagent가 아니라 독립 nanobot serve(길 A).
3. **표준 OpenAI 프로토콜** — 별도 큐/DB/프로토콜 없음. `/v1/chat/completions` 그대로.
4. **로컬 바인딩** — Core API는 `127.0.0.1`. 무효 키는 upstream(Codex) 호출 전에 차단.
5. **원본 최소 수정** — 전문화는 워크스페이스/설정/프롬프트로. 추가 기능은 신규 모듈.
6. **위임 정확도 우선** — rule-first는 명백한 것만, 애매하면 Core LLM. 토큰보다 정확도.

---

## 2. 두 표면 (Two surfaces)

| 표면 | 방향 | 인증 | LLM | 용도 |
|---|---|---|---|---|
| **`/v1/chat/completions`** | Sub → Core → Codex | Sub 키(keystore) | Codex(모델) | Sub가 모델을 부른다. 다중 메시지 relay + 동시성 상한 + 429 백오프 |
| **`/queen/chat`** | User → Core → Sub | User 키 | 라우팅 시에만 | User가 Sub와 대화. 라우팅·패스스루·responder 태깅 |

> `nanobot serve`의 기본 `/v1`은 **단일 user 메시지만** 허용한다. 그래서 게이트웨이가 다중 메시지
> relay를 별도로 제공한다(블로커 B 해결). User→Sub 패스스루는 `/queen/chat`이 담당한다.

```
   User(CLI) ─┐
              │ /queen/chat (라우팅)          /v1 (모델 중계)
              ▼                                      ▲
        ┌──────────────────────────────────────────────┐
        │  Core Gateway :8900  (127.0.0.1)              │
        │   인증·로깅·동시성·429·keystore 핫리로드        │
        │            │ Codex OAuth (Core만)              │
        │            ▼                                   │
        │       ChatGPT/Codex (gpt-5.5)                  │
        └──────────────────────────────────────────────┘
              ▲ HTTP /v1 (Sub 키)        │ HTTP /v1 포워딩(User→Sub)
   Sub: research:8901  coder:8902  idea:8903 …  (각자 nanobot serve, provider=custom→게이트웨이)
```

---

## 3. 모듈 (`nanobot/queen/`, 14개)

| 모듈 | 책임 |
|---|---|
| `gateway.py` | 두 표면 + `/health`·`/v1/models`. 사전공유 키 검증(무효→upstream 전 401), 사용량 로깅, 동시성 상한(429), 429 백오프, keystore 핫리로드, User→Sub 라우팅 배선 |
| `registry.py` | Sub 카탈로그(JSON, atomic): id·role·capability·port·workspace·status·mode·prompt_version·last_used·pid + CLI |
| `factory.py` | 입력(role/capability/skills/mode) → 워크스페이스 복제 + 역할 프롬프트·provider·고유키·고유포트 주입 → serve 기동 → 등록 → health. allowlist 안전장치. capability별 최소 툴셋(프롬프트 토큰 절감) |
| `adjuster.py` | 역할/범위 조정. draft→승인→apply. 홈유지(기억보존)/격리. 금지패턴 필터, 이력+롤백 |
| `lifecycle.py` | 온디맨드 생성/유휴 종료(워크스페이스·sessions 보존), 재기동-기억연속. always-on 미종료 |
| `memory.py` | Core 통합 메모리 승격(중요 결과). 격리에도 보존 |
| `orchestrator.py` | rule-first 라우팅(Router) + 위임(Orchestrator): task_id, gather, 단일 verbatim/다중 merge |
| `chat.py` | `/queen/chat` 배선: rule-first→(모호)LLM 분류→단일 패스스루/다중 통합. `SubForwarder` |
| `bridge.py` | 채널↔게이트웨이 글루. 버스 InboundMessage 소비 → `/queen/chat` → 라벨 metadata로 OutboundMessage emit |
| `labels.py` | 응답 주체 라벨. CLI `[Research]`, Telegram `<b>[Research]</b>`, 전환 `↪ A → B` |
| `cli.py` | 표준 인터랙티브 CLI. 슬래시 3개(/spawn·/subs·/stop) + 자연어 설정 |
| `usage.py` | 사용량 집계(고정비용 추정) + 운영 지표(하루 토큰·rule/llm·단일/다중·rate-limit) |
| `idea_style.py` | 빈 idea Sub에 자연어 작동방식 주입(필터→승인→적용), 불변 잠금, 롤백 |
| `admin_nl.py` | 자연어 설정 의도 인식(config / rollback / 일반질문 구분) |

---

## 4. 라우팅 흐름 (`/queen/chat`)

1. **rule-first** (`Router`, 0 토큰): 명백한 도메인 키워드만. 단일 Sub 매칭 → 그 Sub로 패스스루(Core LLM 안 탐).
2. **모호** → Core LLM 분류기(정확도 우선). 단일 Sub 반환 → 패스스루.
3. **다중** Sub → Core가 각 Sub 호출 후 LLM 통합.
4. **맞는 Sub 없음** → Core가 직접 답(`core_direct`).

responder(sub_id)는 `X-Responder-Sub-Id` 헤더 + JSON으로 반환(토큰 0 메타데이터) → 채널이 라벨 렌더.

---

## 5. Sub 생성·생애주기

- **생성(factory)**: `role`(allowlist) + `capability`(allowlist) → `~/.nbq-<role>` 워크스페이스 생성,
  역할 프롬프트(AGENTS.md/SOUL.md)·provider(→게이트웨이)·고유 사전공유 키·고유 포트 주입 → serve →
  레지스트리 등록 → health.
- **capability별 최소 툴셋**: capability가 필요로 하지 않는 툴 그룹(file/exec/web/my/cliApps)을 끈다.
  프롬프트 토큰 절감(연구 Sub 8,395→6,552, ~22%↓) + 권한 최소화.
- **온디맨드(lifecycle)**: 필요 시 생성, 유휴 시 종료(워크스페이스·sessions 보존). 재기동 시 같은
  워크스페이스·포트·키로 **기억 연속**.
- **역할 조정(adjuster)**: 이미 뜬 Sub의 역할/범위를 draft→승인→apply로 변경. 홈유지(기억보존) 또는
  격리(`isolate`, sessions 아카이브). 금지패턴 필터·이력·롤백.

생성 가능 역할/능력(allowlist):

| 역할 | 능력 |
|---|---|
| research | research.web, research.summary |
| coder | code.write, code.review |
| writer | writing.draft, writing.edit |
| analyst | data.analyze, data.viz |
| planner | planning.decompose |
| **idea** | idea.generate, idea.structure, idea.evaluate |

---

## 6. 빈 'idea' Sub 패러다임 (새 패러다임)

미리 정의된 전문가 대신, **빈 idea Sub**를 띄우고 **자연어/문서로 작동 방식을 나중에 빚는다.**

- **1단계 — 빈 Sub**: `idea` role은 툴이 0개(file/exec/web 전부 차단) → 코드 실행·외부 작업 불가,
  순수 사고만. 역할 프롬프트에 "아이디어 영역 한정, 산출물 제작 금지, 범위 밖이면 OUT_OF_SCOPE" 경계.
- **2단계 — 자연어 빚기(idea_style)**: 사용자가 "idea가 이렇게 작동하게 해줘 [지침]"이라고 하면,
  그 지침을 역할 프롬프트의 **"작동 스타일" 섹션**으로 주입한다. 주입은 STEP 7 adjuster 메커니즘 재사용
  (draft→승인→apply, 홈유지=기억보존, 롤백).

**불변 잠금(자연어로 변경 불가, 구조적 강제)**:
- **툴 권한** — config가 강제(idea.* capability → 툴 0개). 프롬프트가 "툴 써라" 해도 config에 툴 없음.
- **capability 영역** — adjuster가 capability를 유지 → idea 도메인 고정.
- **게이트웨이 경유** — provider config 고정.
- 자연어가 바꿀 수 있는 건 "아이디어를 어떤 방식·관점·구조로 도출하는가"(작동 스타일)뿐.

방어 필터: 주입 텍스트를 ① 금지패턴 필터(게이트웨이 우회·Sub 사칭·자격증명 유출) ② idea 불변 필터
(범위 확장·경계 해제·산출물/툴 활성화·OUT_OF_SCOPE 무효화 시도 차단)로 검증. 외부 문서도 동일 필터.
**검증**: 작동 방식을 바꾼 뒤에도 코드 요청은 여전히 OUT_OF_SCOPE(경계 유지 확인됨).

---

## 7. 자연어 설정 (슬래시 최소화)

슬래시 명령은 **/spawn · /subs · /stop** 3개만. 설정·승인·롤백은 자연어로(`admin_nl`):

- **설정 인식**: "idea가 ~하게 해줘"(작동 방식 변경) vs "회의 아이디어 알려줘"(일반 질문)를 구분.
  강한 신호(Sub 지명 + configure/behave 동사)만 설정으로, 질문 가드("어떻게 설정하는지 궁금해"→질문).
- **승인도 자연어**: "응/네/적용"=적용, "아니/취소"=취소.
- **롤백 자연어**: "idea를 이전으로 되돌려줘".

**안전 원칙**: 입구만 자연어, 안전장치(필터·승인·롤백)는 0 변경. **승인 없이 적용되는 일은 절대 없다.**
오분류돼도 승인 게이트가 안전망(잘못 분류돼도 적용 전 사용자가 거부). 설정은 idea Sub에 한정.

---

## 8. B1 — 서브에이전트 경계 상속 (원본 버그픽스)

Sub가 nanobot 내장 subagent(`spawn` 도구)를 쓰는 건 의도된 기능이다. 진단 결과:
- **provider/게이트웨이는 상속됨** — 서브에이전트 모델 호출도 게이트웨이 경유(단일 OAuth 유지). ✅
- **버그**: `_subagent_tools_config()`가 exec/web/file만 상속하고 **cli_apps를 누락** → 부모가 끈
  cli_apps가 서브에이전트에서 기본값(활성)으로 복원 → idea가 spawn한 서브에이전트가 `run_cli_app`을
  얻어 외부 실행 우회 가능.
- **수정(B1)**: `nanobot/agent/subagent.py`에서 cli_apps·my·image_generation도 부모에서 상속하게
  3줄 추가. 부모가 끈 툴 그룹을 서브에이전트가 다시 못 얻는다. (원본 1파일 수정 — exec/web/file은
  상속하면서 cli_apps만 빠뜨린 upstream 누락 버그픽스 성격. HKUDS/nanobot PR 기여 후보.)

**남는 한계(차선)**: 서브에이전트는 부모 역할 프롬프트 경계를 상속하지 않아, 코드를 *텍스트로* 써주는
약한 누수는 가능(하드 경계로 실제 실행은 불가). 완전 격리(spawn config 게이트 + 경계 상속)는 미구현.

---

## 9. 핵심 실측 사실 (데이터로 확정)

- **Codex 구독은 프롬프트 캐싱 미적용**: 동일 14k prefix 반복에도 `cached_tokens=0`, `store=true`는
  400. 구독은 정액(토큰 과금 아님). → 캐싱 대신 **시스템 프롬프트 축소**(툴 가지치기)가 레버.
- **호출당 ~8.8k 고정 프롬프트**(토큰의 99.6%가 시스템 프롬프트). Sub·호출이 늘수록 선형 증가.
- **단일 vs 다중 비용**: 단일 패스스루 routing_tokens=0/~3s. 다중은 분류+N Sub+통합의 순차 호출이라
  최대 ~145초. → "단일은 직접, 다중만 Core" 설계가 비용으로 정당.
- **메모리 경로**: Sub 작업기억은 `<workspace>/sessions/<session_id>.jsonl`. 워크스페이스 보존 시
  재기동에도 유지. Core 통합 메모리는 `~/.nbq-core/memory/promoted.jsonl`(격리에도 보존).

---

## 10. 설계 의도 → 실제 구현 (대조표)

| 설계 의도 | 구현 | 상태 |
|---|---|---|
| Core 단일 OAuth, Sub는 키만 | gateway 키 검증 + keystore, OAuth는 Core 머신 세션 | ✅ |
| Sub = 별도 풀 인스턴스 | factory가 `nanobot serve` 프로세스 생성 | ✅ |
| 무효 키 upstream 전 차단 | `_authenticate` → 401, 토큰 0 소모 | ✅ |
| Sub 생성/역할/범위 조정 | factory + adjuster(draft→승인→롤백) | ✅ |
| User↔Sub 직접 대화 + 응답 주체 표시 | /queen/chat 패스스루 + labels + bridge | ✅ (CLI 라이브) |
| 빈 idea Sub + 자연어 빚기 | idea role(툴0) + idea_style 주입 | ✅ |
| 자연어 설정(슬래시 최소화) | admin_nl + cli | ✅ |
| 서브에이전트도 하드 경계 상속 | B1 (subagent.py cli_apps 상속) | ✅ |
| 통합 메모리 / 온디맨드 | memory / lifecycle | ✅ |
| **WebUI를 여왕개미 메인 UI로** | 내장 WebUI는 기동되나 라우팅 연결 안 됨 | ❌ **미구현** |
| 다중 작업 스트리밍/진행표시 | — | ❌ 미구현 |
| 서브에이전트 소프트 경계(역할) 상속 | — | ❌ 미구현(차선) |
| Telegram 라이브 | 라벨 렌더는 구현·단위테스트, 봇 연결은 — | ❌ 미구현 |

---

## 11. 보안 경계

- Core API `127.0.0.1` 바인딩. Sub→Core·User→Core 모두 사전공유 키 검증, 무효 키는 upstream 전 차단.
- **Codex OAuth 토큰은 Core 머신 세션에만**(`~/Library/Application Support/oauth-cli-kit/`). Sub는
  고유 사전공유 키만. keystore `~/.nbq-core/keys.json`는 0600, repo 밖.
- 역할 조정/자연어 설정 시 금지패턴 + idea 불변 필터로 게이트웨이 우회·Sub 사칭·자격증명 유출·경계
  해제 차단. 적용은 항상 사람 승인 후.
- 라이브 산출물(`~/.nbq-*`, usage/keystore/sessions)은 `$HOME`(repo 밖) + `.gitignore` 등록.

---

## 12. 품질

- 원본 nanobot src 수정: **1파일**(`subagent.py` B1). 그 외 전부 `nanobot/queen/` 신규 + tests + docs.
- **pytest 4,617 passed, 3 skipped — 회귀 0.** ruff 통과.
