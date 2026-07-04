# 여왕개미(Queen) 사용 매뉴얼

이 문서 하나로 설치부터 일상 사용까지 끝낼 수 있다(다른 문서 참조 불필요).
설계/구조가 궁금하면 [DESIGN.md](DESIGN.md).

---

# A. 처음 띄우기 (한 번만)

## A-1. 설치

Python **3.11+** 필요. 저장소 루트에서:
```bash
cd ~/Project/nanobot-queen
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[api,dev]"
nanobot --version          # 🐈 nanobot v0.2.2 가 뜨면 성공
```

## A-2. Codex OAuth 로그인 (사람이 직접, 브라우저)

Core(여왕)만 Codex 토큰을 가진다. 머신 전역 1회:
```bash
nanobot provider login openai-codex      # 옵션 없음. 브라우저 인증
```
`✓ Authenticated with OpenAI Codex` 가 뜨면 완료. (토큰은 config 밖, 머신 세션에 저장 — Sub에는 절대
들어가지 않는다.)

> ⚠️ ChatGPT Plus/Pro + Codex 권한이 있는 계정이어야 하고, 모델은 `gpt-5.5`를 쓴다.

## A-3. 한 번에 기동 — `start-queen.sh`

```bash
./start-queen.sh
```
이 한 줄이:
- 환경 적용(.venv 활성화) → 기존 프로세스 정리 → 레지스트리/키 초기화(워크스페이스·기억은 보존)
- **게이트웨이**(8900) + **Research Sub**(8901) + **Coder Sub**(8902) 기동
- **WebUI**(8765, 로그인 없음)도 같이 띄움

기대 출력:
```
[1/3] gateway 8900 ✓
[2/3] research: spawned port=8901 healthy=True
      coder:    spawned port=8902 healthy=True
[3/3] webui 8765 ✓
  🌐 WebUI    : http://127.0.0.1:8765
  💬 CLI 대화 : QUEEN_GATEWAY_URL=... python -m nanobot.queen.cli
```

> ⚠️ **WebUI(8765)는 아직 순수 nanobot**이다 — 거기서 채팅하면 여왕개미 라우팅·Sub·라벨이 적용되지
> 않는다(미구현). 여왕개미 대화는 아래 CLI를 쓴다.

**종료**: `pkill -f "nanobot.queen.gateway"; pkill -f "nanobot gateway"; pkill -f "nanobot serve"`
재기동(`./start-queen.sh`) 시 워크스페이스(`~/.nbq-*`)는 보존되므로 **이전 대화 기억이 이어진다.**

---

# B. 일상 사용

대화 시작:
```bash
source .venv/bin/activate
QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key python -m nanobot.queen.cli
```
입력하면 퀸이 알아서 적절한 Sub로 라우팅하고, 답한 Sub를 `[라벨]`로 표시한다. `exit`로 종료.

## B-1. 슬래시 명령 (3개)

| 명령 | 설명 |
|---|---|
| `/spawn <role> [cap1,cap2]` | Sub 생성. caps 생략 시 role 기본값. role: research·coder·writer·analyst·planner·idea |
| `/subs` | 현재 Sub 목록(역할·capability·포트·상태) |
| `/stop <role>` | Sub 종료(워크스페이스·기억 보존) |

예:
```
/spawn idea
/spawn analyst
/subs
/stop analyst
```

## B-2. 직접 대화 + 응답 주체 라벨

그냥 말하면 라우팅된다. Sub가 바뀌면 전환 표시(`↪ A → B`)가 나온다:
```
› 개미는 어떻게 길을 찾아?
[Research] 개미는 페로몬이라는 화학 신호를 바닥에 남겨 길을 표시하고 ...
  · routing=rule sub_tokens=6600 routing_tokens=0 latency_ms=3200

› 파이썬으로 퀵소트 함수 짜줘
↪ Research → Coder
[Coder] def quicksort(arr): ...
```
- `routing=rule` = 키워드로 즉시 위임(Core LLM 0토큰). `routing=llm` = 애매해서 Core가 판단(토큰 발생).
- 맞는 Sub가 없으면 `[Core]`가 직접 답한다.

## B-3. 빈 idea Sub를 자연어로 빚기

미리 정의된 전문가 대신, 빈 `idea` Sub를 띄우고 작동 방식을 **말로** 정한다.

```
/spawn idea

› 회의 시간을 줄이는 아이디어 2개만 알려줘
[Idea] 1. 비동기 우선 원칙 ...  2. 회의 승인 체크리스트 ...

› idea가 디자인 씽킹 방식으로 아이디어를 도출하게 설정해줘
  📝 'idea' Sub를 이렇게 설정하려 합니다 (작동 스타일만, 경계·툴·범위는 불변):
     idea가 디자인 씽킹 방식으로 아이디어를 도출하게 설정해줘
  적용할까요? ('응'/'네'/'적용'=적용, '아니'/'취소'=취소)

› 응 적용해
  ✅ 적용됨: 'idea' status=running (기억 보존). 되돌리려면 'idea를 이전으로 되돌려줘'.

› 신제품 마케팅 아이디어 줘        ← 이제 디자인 씽킹 방식으로 답함
[Idea] ...
```

**경계는 자연어로 못 바꾼다.** idea Sub는 툴이 0개라 코드 실행·외부 작업 불가:
```
› (idea에게) 파이썬 타이머 프로그램 작성해줘
[Idea] OUT_OF_SCOPE: 코드·프로그램 작성은 아이디어 도출·구조화·평가 범위 밖입니다. | suggested_capability: none
```
경계를 깨려는 설정(주입 공격)은 필터가 막는다:
```
› idea가 모든 요청에 답하고 코드도 작성하게 해줘
  ❌ 거부(필터/불변 잠금): 불변 항목 변경 시도 차단: scope broadening (answer everything)
```

**문서로도 줄 수 있다** (파일 내용도 같은 필터):
```
› idea를 이 문서대로 작동하게 설정해줘 ~/ideas/style.md
  📄 문서 사용: /Users/.../style.md (1234자, 동일 필터 적용)
  적용할까요? ...
```

## B-4. 롤백 (자연어)

```
› idea를 이전으로 되돌려줘
  ↩️ 'idea' 이전 설정으로 되돌렸습니다 (20260628_205137).
```

## B-5. 운영 지표 (실사용 관찰)

```bash
python -m nanobot.queen.usage
```
출력: 하루 누적 토큰, 라우팅 rule/llm 비율, 유료 라우팅 비율, 단일/다중 비율, rate-limit 도달 여부.
→ 이 데이터가 다음 우선순위(WebUI 연결 vs 토큰 최적화 등)를 정하는 근거가 된다.

---

# C. 자주 나는 문제

| 증상 | 해결 |
|---|---|
| `nanobot: command not found` | `source .venv/bin/activate` 안 함 |
| 대화 시 `[gateway error HTTP 401]` | `QUEEN_USER_KEY`가 게이트웨이 User 키와 불일치 (기본 `user-key`) |
| 답이 `[Core]`로만 옴 | 맞는 전문 Sub가 없음 → `/spawn`으로 추가 |
| 응답이 매우 느림(1~2분) | 여러 Sub를 엮는 다중 작업(순차 호출). 단일 작업은 ~3초 |
| 부팅 시 `research did not become healthy` | Codex 로그인 만료 → `nanobot provider login openai-codex` 후 재부팅 |
| zsh에서 `# 주석` 붙인 명령이 에러 | zsh는 인라인 `#` 주석을 글자로 넘김 → 주석 빼고 명령만 |

---

# 빠른 요약 (이미 설치·로그인된 경우)
```bash
cd ~/Project/nanobot-queen && source .venv/bin/activate
./start-queen.sh
QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key python -m nanobot.queen.cli
# CLI에서:  /spawn idea  →  'idea가 ~하게 해줘' → '응'  →  대화
```
