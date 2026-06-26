# PoC-A — 1왕복 (Sub1 → Core → ChatGPT/Codex)

여왕개미 아키텍처의 최소 검증: **Sub1**이 표준 OpenAI `/v1/chat/completions`로
**Core**를 호출하고, Core만 보유한 **Codex OAuth**로 ChatGPT(Codex)에 도달해 응답이
Sub1까지 1왕복으로 돌아오는지 확인한다.

```
   curl ──► Sub1 (:8901)  ──►  Core (:8900)  ──►  ChatGPT/Codex (OAuth)
            provider=custom     provider=openai_codex
            (OpenAI 호환)         (OAuth, Core만 보유)
```

원본 코드는 **수정하지 않았다**. nanobot 0.2.2 기본 기능(`nanobot serve` + config)만 사용한다.

---

## 0. 사전 준비 (이미 완료된 것)

| 파일 | 내용 |
|---|---|
| `~/.nbq-core/config.json` | Core. `provider=openai_codex`, 모델 `openai-codex/gpt-5.1-codex`, API `127.0.0.1:8900` |
| `~/.nbq-sub1/config.json` | Sub1. `providers.custom.apiBase=http://127.0.0.1:8900/v1`, `apiKey=poc-key`, API `127.0.0.1:8901` |
| `docs/poc/run-core.sh` | Core `serve` 실행 스크립트 |
| `docs/poc/run-sub1.sh` | Sub1 `serve` 실행 스크립트 |

> 두 인스턴스는 같은 머신에서 **포트만 다르게** 동작한다. 둘 다 `127.0.0.1`에만 바인딩된다
> (`ApiConfig.host` 기본값이 `127.0.0.1`, schema.py:310 — 전제 6번 충족).

### 설정 근거 / 가정 검증 (코드 확인 완료)

- **모델명 일치 필요**: Core의 `serve`는 요청의 `model` 필드가 Core 설정 모델명과
  다르면 **400 `Only configured model ... is available`** 를 반환한다
  (`nanobot/api/server.py:235`). 그래서 Sub1의 preset `model` 을 Core와 똑같이
  `openai-codex/gpt-5.1-codex` 로 맞췄다.
- **prefix 미절단**: `custom` provider는 `strip_model_prefix=False`
  (`nanobot/providers/registry.py:124`) 이므로 모델명을 **그대로** Core로 전송한다.
  따라서 위 모델명이 변형 없이 Core에 도달해 일치한다.
- **`provider=openai_compat` 의 실제 키 이름**: nanobot에는 `openai_compat` 라는
  provider 키가 없다. OpenAI 호환 엔드포인트는 빌트인 **`custom`** provider로 정의한다
  (백엔드가 `openai_compat`). 그래서 Sub1은 `providers.custom` 을 사용한다.

---

## ⚠️ 보안/정직성 메모 (전제 6·8번 — 반드시 읽을 것)

- **stock `nanobot serve` 에는 사전공유 키 검증이 없다.** `nanobot/api/server.py` 의
  `/v1/chat/completions`, `/v1/models`, `/health` 라우트에는 인증 미들웨어가 전혀 없다
  (코드 확인 완료). 즉 Sub1이 보내는 `apiKey=poc-key` 는 Core 쪽에서 **검증되지 않고
  무시**된다. 어떤 키를 보내든(혹은 안 보내도) Core는 OAuth로 upstream을 호출한다.
- 따라서 **PoC-A는 "1왕복 경로가 성립하는가"만 검증**한다. 전제 6번
  ("무효 키는 upstream 호출 전에 차단")은 **아직 충족되지 않았으며**, Core fork에
  별도 인증 모듈을 추가하는 다음 단계의 작업이다. PoC-A 통과가 보안 충족을 의미하지 않는다.
- Codex **OAuth 토큰은 Sub1 설정/env에 전혀 없다.** Sub1은 `poc-key` 문자열만 가진다.
  진짜 토큰은 Core의 OAuth 세션에만 존재한다 (전제 3·9번 충족).

---

## 1. 🔴 OAuth 로그인 — **여기서 사람이 직접 해야 한다**

> **이 단계는 실행하지 않았다. 당신(사용자)이 직접 해야 하는 지점이다.**
> ChatGPT Plus/Pro 계정으로 브라우저 OAuth가 열린다.

```bash
cd /Users/imkimhk/Project/nanobot-queen
source .venv/bin/activate
nanobot provider login openai-codex
```

> ⚠️ `provider login` 은 **`--config`/`--workspace` 옵션을 받지 않는다**. provider 인자
> 하나만 받는다. 플래그를 붙이면 typer가 `No such option: --config` 로 거부한다.
> OAuth 세션은 **config 별이 아니라 머신 전역**으로 저장되므로 Core config를 지정할 필요가 없다.

- 브라우저가 열리면 ChatGPT 계정으로 인증한다.
- OAuth 세션은 **config.json 밖**(머신 전역 저장소)에 저장된다.
  → config/커밋/로그에 토큰이 남지 않는다. Core·Sub1 어느 config에도 토큰이 없다.
- Sub1 쪽은 **로그인 불필요**. Sub1은 OAuth를 절대 보유하지 않는다.
- Core를 띄울 때 OAuth가 없어도 `serve` 자체는 기동된다(바인딩 성공). OAuth는 **요청 시점**에
  필요하므로, 로그인 없이 curl 하면 Core가 upstream 호출에서 인증 오류를 반환한다.

세션 제거(테스트 후 정리할 때만, 지금은 실행하지 말 것):
```bash
nanobot provider logout openai-codex
```

---

## 2. 두 인스턴스 띄우기

각각 **별도 터미널**에서 실행한다 (둘 다 포그라운드로 떠 있어야 한다).

**터미널 A — Core:**
```bash
/Users/imkimhk/Project/nanobot-queen/docs/poc/run-core.sh
```
기대 출력(요약):
```
🐈 Starting OpenAI-compatible API server
  Endpoint : http://127.0.0.1:8900/v1/chat/completions
  Model    : openai-codex/gpt-5.1-codex (preset: codex)
```

**터미널 B — Sub1:**
```bash
/Users/imkimhk/Project/nanobot-queen/docs/poc/run-sub1.sh
```
기대 출력(요약):
```
🐈 Starting OpenAI-compatible API server
  Endpoint : http://127.0.0.1:8901/v1/chat/completions
  Model    : openai-codex/gpt-5.1-codex (preset: coreproxy)
```

> 스크립트는 프로젝트 `.venv`(python3.11)를 활성화한다. venv 위치가 다르면
> `run-*.sh` 상단의 `source` 경로를 수정한다.

헬스 체크(선택):
```bash
curl -s http://127.0.0.1:8900/health   # Core
curl -s http://127.0.0.1:8901/health   # Sub1
```

---

## 3. 질문 1건 보내기 (1왕복 검증)

**터미널 C** 에서 Sub1(:8901)에 OpenAI 표준 요청을 보낸다. Sub1이 내부적으로 Core(:8900)를
호출하고, Core가 Codex로 도달한다.

```bash
curl -s http://127.0.0.1:8901/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer poc-key" \
  -d '{
    "model": "openai-codex/gpt-5.1-codex",
    "messages": [
      {"role": "user", "content": "PoC-A 1왕복 테스트입니다. 한 문장으로 자기소개 해주세요."}
    ]
  }' | python -m json.tool
```

> `model` 은 **Sub1 설정 모델명과 동일**해야 한다(`openai-codex/gpt-5.1-codex`).
> 다른 값을 넣으면 Sub1이 400을 반환한다(server.py:235 규칙).
> `Authorization` 헤더는 형식상 넣었지만 stock 코드에선 검증되지 않는다(위 보안 메모 참조).

또는 nanobot CLI로 직접:
```bash
cd /Users/imkimhk/Project/nanobot-queen
source .venv/bin/activate
# Sub1 인스턴스의 에이전트로 1건 질의 (serve 없이도 Sub1 config로 Core를 호출)
nanobot agent -m "PoC-A 1왕복 테스트, 한 문장으로 자기소개." \
  --config "$HOME/.nbq-sub1/config.json" --workspace "$HOME/.nbq-sub1"
```

---

## 4. 통과 판정 (사실 수집 — 전제 8번)

"통과"라고만 말하지 않는다. 다음 **실제 출력**을 캡처해 비교한다:

1. Core 터미널에 `API request session_key=api:default ...` 로그가 찍힌다 (Sub→Core 도달).
2. curl 응답 JSON의 `choices[0].message.content` 에 모델 답변 텍스트가 들어 있다.
3. `usage` 토큰 수가 0이 아니다.

**실패 시 점검:**
| 증상 | 원인 후보 |
|---|---|
| Sub1 → `Only configured model ... available` (400) | curl `model` 값이 Sub1 preset과 불일치 |
| Core → 401/OAuth 오류 | 1단계 OAuth 로그인 미완료/만료 → Core에서 재로그인 |
| `Connection refused` (:8900) | Core 미기동 또는 포트 불일치 |
| 빈 응답 / 타임아웃 | Codex 모델명 오류, 또는 upstream 지연(`--timeout` 상향) |

---

## 다음 단계 (PoC-A 이후)

- **전제 6번**: Core fork에 사전공유 키 검증 미들웨어 추가 → 무효 키는 upstream(Codex)
  호출 **이전에** 차단. 이건 별도 모듈로 분리(원본 src 최소 수정 원칙).
- Sub 다중화 / 역할 부여 / 범위 조정은 PoC-A 1왕복이 확인된 뒤 진행한다.
