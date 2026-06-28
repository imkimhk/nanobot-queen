"""Natural-language admin intent — recognise "configure idea like this" requests.

The entry is natural language; the *safety mechanism is unchanged*. A recognised
config request is handed to the existing ``IdeaStyleManager`` (filter → human
approval → apply, with rollback). Nothing is ever applied without approval.

Discrimination (the hard part): a **config request** asks to change *how a Sub
works* ("idea가 이렇게 작동하게 해줘", "idea를 이렇게 설정해줘"); a **normal
question** asks the Sub to produce something now ("회의 아이디어 알려줘"). We only
trigger config on STRONG explicit signals (a Sub is named AND a configure/behave
verb is present); everything else is treated as a normal question. This bias is
safe: a question misread as config still needs approval (the user just says no),
and a config misread as question simply gets answered (the user rephrases).

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Strong "change how this Sub behaves" signals.
_CONFIG_TRIGGERS: tuple[str, ...] = (
    "설정해", "설정하", "설정 바꿔", "설정을 바꿔", "설정 변경", "설정을 변경",
    "작동하게", "작동 하게", "작동하도록", "작동 방식", "작동방식", "동작하게",
    "동작하도록", "동작 방식", "행동하게", "행동하도록",
    "이렇게 작동", "이렇게 설정", "이렇게 동작", "이렇게 행동", "이렇게 일하",
    "스타일로 바꿔", "스타일을 바꿔", "스타일 적용", "스타일로 설정",
    "방식으로 작동", "방식으로 동작", "방식으로 도출하게", "방식으로 일하게",
    "관점으로 작동", "기법으로 작동", "기법으로 도출하게",
    "configure", "set up", "behave like", "work like",
)
# Persistent-behaviour phrasings (imperative "make it work like ...").
_CONFIG_RE = re.compile(
    r"앞으로.{0,30}(이렇게|방식|스타일|관점|기법|하게)"
    r"|(이런\s*식으로|이런\s*방식으로).{0,20}(작동|동작|해|도출)"
    # "<방식/관점/기법/스타일>(으로) ... <동사>하게/하도록/했으면/하면"
    r"|(방식|관점|기법|스타일|식)\s*(으로|을|를|로)?\s*[^.?!]{0,18}"
    r"(작동|동작|도출|평가|발상|일하|생각|분석|구조화|행동|접근)\S*하(게|도록|면|길|였으면|면\s*좋)"
    # "스타일/방식 바꿔/변경/적용/설정"
    r"|(스타일|방식|관점|기법)\s*(을|를)?\s*(바꿔|변경|적용|설정)"
    # "작동/동작/행동 했으면/하면/하길/해주/하도록/하게"
    r"|(작동|동작|행동)\s*(했으면|하면|하길|해줬|해주|하도록|하게)"
    # general imperative "<동사>하게/하도록 + 해/설정/만들/바꿔" = make the Sub behave
    # this way (catches "답하게 해줘", "작성하게 해줘", "평가하게 설정"). Note: "평가해줘"
    # (no "하게") is NOT matched, keeping one-off requests as questions.
    r"|\S{1,8}하(게|도록)\s*(해|만들|바꿔|설정|구성|행동)",
    re.IGNORECASE,
)

# Interrogative guard: a question ABOUT a Sub's settings, not a command to change
# them ("어떻게 설정하는지 궁금해", "설정 알려줘"). If this matches and there is no
# imperative change-verb, treat as a normal question (route to chat).
_QUESTION_RE = re.compile(
    r"(궁금|알려줘|알려주|어떻게.{0,8}(하는지|되는지|하나|돼)|설정하는지|작동하는지|"
    r"동작하는지|뭐(야|예요|냐)|무엇|있(어|나)\s*\??$)",
    re.IGNORECASE,
)
# Imperative change-verbs that override the question guard.
_IMPERATIVE_RE = re.compile(
    r"(설정해|설정하게|바꿔|변경해|되돌려|적용해|작동하게|동작하게|행동하게|하게\s*해|하도록\s*해)",
    re.IGNORECASE,
)

_ROLLBACK_TRIGGERS: tuple[str, ...] = (
    "되돌려", "롤백", "이전으로", "이전 상태", "원래대로", "복구해", "되돌리",
    "rollback", "revert", "undo",
)

_AFFIRM: tuple[str, ...] = (
    "응", "네", "예", "그래", "적용", "좋아", "해줘", "오케이", "ok", "okay",
    "yes", "y", "ㅇㅇ", "그렇게", "진행",
)
_NEGATE: tuple[str, ...] = (
    "아니", "아냐", "취소", "그만", "하지마", "안할래", "no", "n", "ㄴㄴ", "stop", "cancel",
)


@dataclass
class AdminIntent:
    kind: str                    # "config" | "rollback" | "none"
    sub_id: str | None = None
    instruction: str = ""
    doc_path: str | None = None


def _find_sub(text: str, known_subs: list[str]) -> str | None:
    low = text.lower()
    for sid in known_subs:
        if sid.lower() in low:
            return sid
    return None


def _find_doc_path(text: str) -> str | None:
    """Return a referenced file path that exists, if any (``@path`` or a token)."""
    for tok in re.split(r"\s+", text.strip()):
        cand = tok.lstrip("@").strip("\"'")
        if ("/" in cand or cand.endswith((".md", ".txt"))) and Path(cand).expanduser().is_file():
            return str(Path(cand).expanduser())
    return None


def _has_config_trigger(text: str) -> bool:
    low = text.lower()
    if any(t in low for t in _CONFIG_TRIGGERS):
        return True
    return bool(_CONFIG_RE.search(text))


def detect_intent(text: str, known_subs: list[str]) -> AdminIntent:
    """Classify a user message into a config/rollback admin intent, or 'none'."""
    sub = _find_sub(text, known_subs)
    if sub is None:
        return AdminIntent("none")

    # rollback: sub named + a rollback verb
    low = text.lower()
    if any(t in low for t in _ROLLBACK_TRIGGERS):
        return AdminIntent("rollback", sub_id=sub)

    # question guard: a question about settings (no imperative change-verb) -> normal
    if _QUESTION_RE.search(text) and not _IMPERATIVE_RE.search(text):
        return AdminIntent("none")

    # config: sub named + a strong configure/behave signal
    if _has_config_trigger(text):
        doc = _find_doc_path(text)
        return AdminIntent("config", sub_id=sub, instruction=text, doc_path=doc)

    return AdminIntent("none")


def is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    return any(t == a or t.startswith(a + " ") or a in t.split() for a in _AFFIRM)


def is_negative(text: str) -> bool:
    t = text.strip().lower()
    return any(t == n or t.startswith(n + " ") or n in t.split() for n in _NEGATE)
