"""Unit tests for sticky routing (session_id -> pinned Sub) and Core-direct session.

Covers the fix for the routing regression where every turn re-classified from
scratch, so a follow-up like "그 코드명 뭐였지?" jumped from the ``research``
Sub (holding the ZEBRA memory) to the ``coder`` Sub. Also covers the STEP 10-3
OUT_OF_SCOPE handoff and the ``@sub_id`` explicit-switch escape hatch.
"""

from __future__ import annotations

import pytest

from nanobot.queen.chat import (
    ROUTE_CORE_DIRECT,
    ROUTE_HANDOFF,
    ROUTE_LLM,
    ROUTE_RULE,
    ROUTE_STICKY,
    QueenChat,
)
from nanobot.queen.orchestrator import SubResult
from nanobot.queen.registry import STATUS_RUNNING, SubRecord, SubRegistry
from nanobot.queen.session_state import SessionRouterStore


@pytest.fixture
def registry(tmp_path):
    reg = SubRegistry(tmp_path / "subs.json")
    reg.register(SubRecord(id="research", role="research",
                           capability=["research.web", "research.summary"],
                           port=8902, workspace=str(tmp_path / "r"),
                           status=STATUS_RUNNING))
    reg.register(SubRecord(id="coder", role="coder",
                           capability=["code.write", "code.review"],
                           port=8903, workspace=str(tmp_path / "c"),
                           status=STATUS_RUNNING))
    return reg


def _sub_call_recorder(record, content_by_sub: dict | None = None):
    """Async sub_call that records invocations and returns scripted content per sub."""
    async def sub_call(sub_id, task_id, text):
        record.append((sub_id, task_id, text))
        content = (content_by_sub or {}).get(sub_id, f"{sub_id}: done")
        return SubResult(sub_id=sub_id, task_id=task_id, content=content,
                         usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
    return sub_call


# ---------------------------------------------------------------------------
# Sticky: follow-up stays with the same Sub, classifier is NOT called again.
# ---------------------------------------------------------------------------


async def test_sticky_followup_reuses_same_sub_and_skips_classifier(registry):
    calls = []
    classifier_calls = 0

    async def classify(text, subs):
        nonlocal classifier_calls
        classifier_calls += 1
        # First turn: research (matches the ZEBRA "remember this" case).
        return ["research"], {"total_tokens": 50}

    state = SessionRouterStore()

    # Turn 1 — ambiguous, classifier picks research; sticky is set.
    chat1 = QueenChat(registry, _sub_call_recorder(calls),
                      classify=classify, session_state=state, session_id="sess-A")
    r1 = await chat1.handle("기억해 ZEBRA")
    assert r1.responder == ["research"]
    assert r1.routing == ROUTE_LLM
    assert classifier_calls == 1
    assert state.get_sticky("sess-A") == ["research"]

    # Turn 2 — "코드명 뭐였지?" contains the "코드" keyword. Without sticky
    # this would misroute to coder; sticky must send it to research and skip
    # the classifier entirely (0 extra routing tokens on this turn).
    chat2 = QueenChat(registry, _sub_call_recorder(calls),
                      classify=classify, session_state=state, session_id="sess-A")
    r2 = await chat2.handle("그 코드명 뭐였지?")
    assert r2.responder == ["research"]           # stayed with research
    assert r2.routing == ROUTE_STICKY
    assert r2.routing_usage["total_tokens"] == 0  # no classifier call
    assert classifier_calls == 1                  # unchanged from turn 1


async def test_sticky_isolated_per_session(registry):
    """Different session_ids do not share their sticky state."""
    calls = []

    async def classify(text, subs):
        # Both sessions start ambiguous; ship each to a different Sub.
        return (["research"] if "조사" in text else ["coder"]), {"total_tokens": 40}

    state = SessionRouterStore()

    # Session A -> research
    await QueenChat(registry, _sub_call_recorder(calls),
                    classify=classify, session_state=state,
                    session_id="A").handle("음... 조사해줘")
    # Session B -> coder (independent of A)
    await QueenChat(registry, _sub_call_recorder(calls),
                    classify=classify, session_state=state,
                    session_id="B").handle("음... 짜줘")

    assert state.get_sticky("A") == ["research"]
    assert state.get_sticky("B") == ["coder"]


# ---------------------------------------------------------------------------
# Explicit user switch: ``@coder …`` overrides the sticky bond for this turn.
# ---------------------------------------------------------------------------


async def test_explicit_at_mention_overrides_sticky(registry):
    calls = []

    async def classify(text, subs):
        raise AssertionError("classifier must not run when @mention is present")

    state = SessionRouterStore()
    state.set_sticky("S", ["research"])

    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, session_state=state, session_id="S")
    res = await chat.handle("@coder 이 함수 리팩터링 해줘")
    assert res.responder == ["coder"]
    assert res.routing == ROUTE_RULE
    # sticky moves to the newly-mentioned Sub
    assert state.get_sticky("S") == ["coder"]


async def test_mention_of_unknown_sub_is_ignored(registry):
    calls = []

    async def classify(text, subs):
        return ["research"], {"total_tokens": 40}

    state = SessionRouterStore()
    # No sticky yet; @nobody should NOT hijack routing.
    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, session_state=state, session_id="S")
    res = await chat.handle("@nobody 뭘 좀 알아봐줘")
    assert res.responder == ["research"]  # fell through to normal classification


# ---------------------------------------------------------------------------
# STEP 10-3 handoff: OUT_OF_SCOPE from the sticky Sub reroutes this turn.
# ---------------------------------------------------------------------------


async def test_out_of_scope_triggers_handoff_and_clears_sticky(registry):
    calls = []

    async def classify(text, subs):
        # Called only on handoff: exclude coder, so research is the natural pick.
        assert all(s.id != "coder" for s in subs)  # excluded from the retry
        return ["research"], {"total_tokens": 30}

    contents = {"coder": "OUT_OF_SCOPE: 리서치 요청은 범위 밖 | suggested_capability: research.web",
                "research": "research: answered"}
    state = SessionRouterStore()
    state.set_sticky("S", ["coder"])   # pretend an earlier turn stuck on coder

    chat = QueenChat(registry, _sub_call_recorder(calls, contents),
                     classify=classify, session_state=state, session_id="S")
    res = await chat.handle("최신 논문 요약해줘")

    # coder handled first (sticky), returned OUT_OF_SCOPE, then research took over.
    assert [c[0] for c in calls] == ["coder", "research"]
    assert res.responder == ["research"]
    assert res.routing == ROUTE_HANDOFF
    assert res.content == "research: answered"
    # sticky moved to research after the successful handoff
    assert state.get_sticky("S") == ["research"]


async def test_out_of_scope_with_no_alternative_falls_back_to_core(registry):
    calls = []

    async def classify(text, subs):
        return [], {"total_tokens": 20}  # no other Sub fits

    async def core_answer(text):
        return "core answered", {"total_tokens": 100}

    contents = {"coder": "OUT_OF_SCOPE: 코드 아님 | suggested_capability: none"}
    state = SessionRouterStore()
    state.set_sticky("S", ["coder"])

    chat = QueenChat(registry, _sub_call_recorder(calls, contents),
                     classify=classify, core_answer=core_answer,
                     session_state=state, session_id="S")
    res = await chat.handle("잡담이나 하자")
    assert res.responder == ["core"]
    assert res.routing == ROUTE_CORE_DIRECT
    assert res.content == "core answered"
    # core-direct pins sticky to core so next follow-ups stay on Core-direct
    assert state.get_sticky("S") == ["core"]


# ---------------------------------------------------------------------------
# Core-direct sticky: subsequent turns keep answering via Core when the
# classifier still finds no Sub, but the classifier IS re-consulted every
# turn (A3-c: sticky=[core] is treated as provisional so a follow-up that
# actually needs a Sub can escape the Core bond).
# ---------------------------------------------------------------------------


async def test_core_direct_sticky_follows_up_on_core(registry):
    calls = []
    classifier_calls = 0

    async def classify(text, subs):
        nonlocal classifier_calls
        classifier_calls += 1
        return [], {"total_tokens": 20}   # every turn: still no Sub fits

    core_answers: list[str] = []

    async def core_answer(text):
        core_answers.append(text)
        return f"CORE:{text}", {"total_tokens": 200}

    state = SessionRouterStore()

    # Turn 1: no Sub fits -> Core-direct, sticky = ["core"]
    r1 = await QueenChat(registry, _sub_call_recorder(calls),
                         classify=classify, core_answer=core_answer,
                         session_state=state, session_id="S").handle("아무 sub와도 무관")
    assert r1.responder == ["core"] and r1.routing == ROUTE_CORE_DIRECT
    assert state.get_sticky("S") == ["core"]

    # Turn 2: sticky=[core] re-asks the classifier (A3-c). Since it still
    # returns [], we stay on Core-direct and sticky remains [core].
    r2 = await QueenChat(registry, _sub_call_recorder(calls),
                         classify=classify, core_answer=core_answer,
                         session_state=state, session_id="S").handle("그거 뭐였지?")
    assert r2.responder == ["core"] and r2.routing == ROUTE_CORE_DIRECT
    assert classifier_calls == 2                     # re-classified on turn 2
    assert core_answers == ["아무 sub와도 무관", "그거 뭐였지?"]
    assert state.get_sticky("S") == ["core"]


# ---------------------------------------------------------------------------
# Backward compat: without session_state / session_id, behavior is unchanged.
# ---------------------------------------------------------------------------


async def test_stateless_mode_still_reclassifies_every_turn(registry):
    """If callers don't supply session_state, sticky is disabled entirely."""
    calls = []
    classifier_calls = 0

    async def classify(text, subs):
        nonlocal classifier_calls
        classifier_calls += 1
        return ["research"], {"total_tokens": 40}

    chat_call = _sub_call_recorder(calls)
    # No session_state / session_id: original stateless behavior.
    r1 = await QueenChat(registry, chat_call, classify=classify).handle("음..")
    r2 = await QueenChat(registry, chat_call, classify=classify).handle("또..")

    assert r1.routing == ROUTE_LLM and r2.routing == ROUTE_LLM
    assert classifier_calls == 2  # classifier called on both turns


# ---------------------------------------------------------------------------
# SessionRouterStore — small direct tests
# ---------------------------------------------------------------------------


def test_session_store_sticky_roundtrip():
    s = SessionRouterStore()
    assert s.get_sticky("x") is None
    s.set_sticky("x", ["research"])
    assert s.get_sticky("x") == ["research"]
    s.clear_sticky("x")
    assert s.get_sticky("x") is None


def test_session_store_core_history_is_capped():
    s = SessionRouterStore(max_core_history_messages=4)
    for i in range(10):
        s.append_core_history("S", "user", f"u{i}")
        s.append_core_history("S", "assistant", f"a{i}")
    hist = s.get_core_history("S")
    assert len(hist) == 4                  # capped
    assert hist[-1] == {"role": "assistant", "content": "a9"}


def test_session_store_core_history_is_isolated_per_session():
    s = SessionRouterStore()
    s.append_core_history("A", "user", "hi-A")
    s.append_core_history("B", "user", "hi-B")
    assert [m["content"] for m in s.get_core_history("A")] == ["hi-A"]
    assert [m["content"] for m in s.get_core_history("B")] == ["hi-B"]


# ---------------------------------------------------------------------------
# Telegram-specific misroute regressions (A/B/C fixes)
# ---------------------------------------------------------------------------


async def test_reply_to_prefix_does_not_misroute(registry):
    """Telegram '[Reply to bot: ...코드...]' prefix must not drag routing to coder.

    Reproduces the exact ZEBRA-recurrence path: a user replies to a bot message
    that contained "코드" and asks a follow-up. Before A+B the rule router would
    substring-match "코드" and pin the session to coder. Now the prefix is
    stripped for routing and the LLM classifier decides on the actual question.
    """
    calls = []

    async def classify(text, subs):
        # Classifier sees the full text (context preserved) and picks research.
        return ["research"], {"total_tokens": 40}

    state = SessionRouterStore()
    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, session_state=state, session_id="tg:S")
    telegram_content = (
        "[Reply to bot: [Research] ZEBRA로 기억했습니다. 필요하면 예시 코드도 드릴게요.]\n"
        "그 코드명 뭐였지?"
    )
    res = await chat.handle(telegram_content)
    assert res.responder == ["research"]     # NOT coder
    assert res.routing == ROUTE_LLM          # first-turn LLM-first (B)
    # sticky pins to research so the actual ZEBRA follow-up stays there
    assert state.get_sticky("tg:S") == ["research"]


async def test_transcription_tag_stripped_in_routing(registry):
    """A '[transcription: ...]' voice-message prefix must not force coder either."""
    calls = []
    classify_calls = 0

    async def classify(text, subs):
        nonlocal classify_calls
        classify_calls += 1
        # Simulate the classifier answering research for a research-y question.
        return ["research"], {"total_tokens": 30}

    state = SessionRouterStore()
    # Force rule-first ON to prove the normalization strip works even when
    # rule fires. Without A the "코드" inside the transcription would misroute.
    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, session_state=state, session_id="tg:V",
                     first_turn_rule_first=True)
    res = await chat.handle(
        "[transcription: 방금 네가 알려준 예시 코드 이야기 말인데 궁금해서 물어봐.]"
    )
    # Primary text (after strip) is empty → routing_text falls back to original,
    # but the rule strip happens on primary_text which is empty → rule sees
    # empty primary and cannot match → classifier is used.
    assert res.responder == ["research"]
    assert classify_calls >= 1               # classifier consulted


async def test_media_tags_stripped_but_kept_in_sub_prompt(registry):
    """Sub must receive the ORIGINAL text (with tags) even after normalization."""
    seen_sub_texts: list[str] = []

    async def sub_call(sub_id, task_id, text):
        # Record exactly what the Sub was asked — this must be the raw text.
        seen_sub_texts.append(text)
        return SubResult(sub_id=sub_id, task_id=task_id, content=f"{sub_id}: done",
                         usage={"total_tokens": 5})

    async def classify(text, subs):
        return ["research"], {"total_tokens": 10}

    state = SessionRouterStore()
    original = "[Reply to bot: prior]\n[image: /tmp/x.jpg]\n이거 뭔지 조사해줘"
    await QueenChat(registry, sub_call,
                    classify=classify, session_state=state,
                    session_id="tg:M").handle(original)
    assert seen_sub_texts == [original]      # Sub sees the raw prefixed text


async def test_first_turn_prefers_llm_classifier_when_rule_ambiguous(registry):
    """B: a first-turn message that would substring-match rule is deferred to LLM.

    "코드명" contains the boundary-checked keyword "코드" but is not a real code
    request. b1 boundary-check alone rejects the substring; b2 (LLM-first on
    the first turn) additionally guarantees rule is skipped even for genuine
    ambiguous cases. Together they prevent the first-turn misroute.
    """
    calls = []
    classify_calls = 0

    async def classify(text, subs):
        nonlocal classify_calls
        classify_calls += 1
        return ["research"], {"total_tokens": 30}

    state = SessionRouterStore()
    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, session_state=state, session_id="tg:F")
    res = await chat.handle("내 코드명 기억해줘: ZEBRA")
    assert res.responder == ["research"]
    assert res.routing == ROUTE_LLM
    assert classify_calls == 1


async def test_out_of_scope_flexible_match_with_apology_preface(registry):
    """C: OUT_OF_SCOPE with a short apology/preface still triggers handoff."""
    calls = []

    async def classify(text, subs):
        # Handoff path: coder excluded, so research is the natural pick.
        assert all(s.id != "coder" for s in subs)
        return ["research"], {"total_tokens": 20}

    # LLM sometimes prepends a short apology before the OUT_OF_SCOPE marker
    # even though the prompt asks for exactly one line. The tolerant matcher
    # must still catch the marker within the first ~200 chars.
    contents = {
        "coder": "죄송합니다. 이건 제 범위 밖입니다.\nOUT_OF_SCOPE: 리서치 요청 | suggested_capability: research.web",
        "research": "research: answered",
    }
    state = SessionRouterStore()
    state.set_sticky("tg:O", ["coder"])

    chat = QueenChat(registry, _sub_call_recorder(calls, contents),
                     classify=classify, session_state=state, session_id="tg:O")
    res = await chat.handle("최신 논문 요약해줘")

    assert [c[0] for c in calls] == ["coder", "research"]  # handoff happened
    assert res.responder == ["research"]
    assert res.routing == ROUTE_HANDOFF
    assert res.content == "research: answered"
    assert state.get_sticky("tg:O") == ["research"]


# ---------------------------------------------------------------------------
# _normalize_for_routing / boundary matcher — direct unit tests
# ---------------------------------------------------------------------------


def test_normalize_for_routing_strips_reply_prefix():
    from nanobot.queen.chat import _normalize_for_routing

    primary, quoted = _normalize_for_routing(
        "[Reply to bot: [Research] ZEBRA로 기억. 예시 코드도 드릴게요.]\n그 코드명 뭐였지?"
    )
    assert primary == "그 코드명 뭐였지?"
    assert "예시 코드" in quoted
    # Multiple prefix tags in a row are all peeled off.
    primary2, _ = _normalize_for_routing(
        "[Reply to bot: prior]\n[image: /tmp/x.jpg]\n이거 뭔지 조사해줘"
    )
    assert primary2 == "이거 뭔지 조사해줘"


def test_normalize_for_routing_leaves_unknown_bracket_alone():
    from nanobot.queen.chat import _normalize_for_routing

    # Not a known tag prefix — must NOT be stripped so user brackets stay intact.
    primary, quoted = _normalize_for_routing("[note: 개인 메모] 나머지 질문")
    assert primary.startswith("[note:")
    assert quoted == ""


def test_boundary_keyword_matcher_excludes_compound_words():
    """Boundary matcher: '코드' inside '코드명' must NOT match; '코드를' still does."""
    from nanobot.queen.chat import _keyword_matches

    assert _keyword_matches("코드", "코드 짜줘")           # space boundary
    assert _keyword_matches("코드", "코드를 짜줘")         # particle 를
    assert _keyword_matches("코드", "이 코드 확인해줘")    # spaces around
    assert not _keyword_matches("코드", "그 코드명 뭐였지?")  # compound noun
    assert not _keyword_matches("함수", "함수화된 것")     # compound
    # Non-boundary keywords still use plain substring.
    assert _keyword_matches("research", "let's do some research")
    assert _keyword_matches("리서치", "리서치해줘")


# ---------------------------------------------------------------------------
# Core-direct sticky recovery (A3-c): sticky=[STICKY_CORE] is provisional —
# each turn re-asks the classifier so a request that actually fits a Sub can
# escape the Core-direct bond. Named-Sub sticky is unchanged (regression test).
# ---------------------------------------------------------------------------


async def test_sticky_core_recalls_classifier_and_recovers_to_sub(registry):
    """sticky=[core] must re-classify; a real Sub match escapes the Core bond."""
    calls = []
    classifier_calls = 0

    async def classify(text, subs):
        nonlocal classifier_calls
        classifier_calls += 1
        return ["research"], {"total_tokens": 40}

    async def core_answer(text):  # pragma: no cover - never called on this path
        raise AssertionError("core_answer must NOT be called when classifier resolves a Sub")

    state = SessionRouterStore()
    # Simulate several prior Core-direct turns that pinned sticky to [core].
    state.set_sticky("S", ["core"])

    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, core_answer=core_answer,
                     session_state=state, session_id="S")
    res = await chat.handle("최신 KOSPI 뉴스 알려줘")

    assert classifier_calls == 1
    assert res.responder == ["research"]
    assert res.routing == ROUTE_LLM
    # sticky escaped from Core → pinned to the newly-resolved Sub.
    assert state.get_sticky("S") == ["research"]
    # The Sub actually received the delegation.
    assert [c[0] for c in calls] == ["research"]


async def test_sticky_core_keeps_core_when_classifier_none(registry):
    """sticky=[core] + classifier none → stays Core-direct, sticky unchanged."""
    calls = []
    classifier_calls = 0

    async def classify(text, subs):
        nonlocal classifier_calls
        classifier_calls += 1
        return [], {"total_tokens": 20}   # still no Sub fits

    async def core_answer(text):
        return "core: hi", {"total_tokens": 30}

    state = SessionRouterStore()
    state.set_sticky("S", ["core"])

    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, core_answer=core_answer,
                     session_state=state, session_id="S")
    res = await chat.handle("안녕 잘 지내?")

    assert classifier_calls == 1
    assert res.responder == ["core"]
    assert res.routing == ROUTE_CORE_DIRECT
    # sticky remains at core (kept, not silently cleared).
    assert state.get_sticky("S") == ["core"]
    # No Sub was called.
    assert calls == []


async def test_sticky_named_sub_still_skips_classifier(registry):
    """Regression: named-Sub sticky (e.g. sticky=[research]) is unchanged."""
    calls = []
    classifier_calls = 0

    async def classify(text, subs):
        nonlocal classifier_calls
        classifier_calls += 1
        raise AssertionError("classifier MUST NOT run on named-Sub sticky")

    state = SessionRouterStore()
    state.set_sticky("S", ["research"])

    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, session_state=state, session_id="S")
    res = await chat.handle("아무 후속 질문")

    assert classifier_calls == 0                # 회귀 방지: classifier 미호출
    assert res.responder == ["research"]
    assert res.routing == ROUTE_STICKY
    assert res.routing_usage["total_tokens"] == 0
    assert state.get_sticky("S") == ["research"]


async def test_sticky_core_recalled_classifier_transition_note():
    """QueenBridge stamps PREV_RESPONDER once the Core-sticky session escapes."""
    from unittest.mock import AsyncMock

    from nanobot.queen.bridge import QueenBridge
    from nanobot.queen.labels import PREV_RESPONDER_META_KEY, RESPONDER_META_KEY

    class _StubBus:
        pass

    class _StubGateway:
        def __init__(self):
            self.chat = AsyncMock()

    class _Msg:
        def __init__(self, chat_id, session_key, content):
            self.channel = "test"
            self.chat_id = chat_id
            self.session_key = session_key
            self.content = content

    gw = _StubGateway()
    bridge = QueenBridge(_StubBus(), gw)

    # Turn 1: Core-direct (last responder stored as ["core"]).
    gw.chat.return_value = {
        "content": "hi from core", "responder": ["core"],
        "routing": ROUTE_CORE_DIRECT, "multi": False,
    }
    out1 = await bridge.handle_one(_Msg("chat-1", "S", "안녕"))
    assert out1.metadata[RESPONDER_META_KEY] == ["core"]
    assert PREV_RESPONDER_META_KEY not in out1.metadata

    # Turn 2: gateway resolves to research (sticky-core recovery path).
    gw.chat.return_value = {
        "content": "research: latest news…", "responder": ["research"],
        "routing": ROUTE_LLM, "multi": False,
    }
    out2 = await bridge.handle_one(_Msg("chat-1", "S", "최신 뉴스 알려줘"))
    assert out2.metadata[RESPONDER_META_KEY] == ["research"]
    # Transition banner metadata is stamped once responder changes.
    assert out2.metadata[PREV_RESPONDER_META_KEY] == ["core"]


async def test_first_turn_still_llm_when_no_sticky(registry):
    """Regression: session_active + no sticky → still first-turn LLM classifier."""
    calls = []
    classifier_calls = 0

    async def classify(text, subs):
        nonlocal classifier_calls
        classifier_calls += 1
        return ["research"], {"total_tokens": 25}

    state = SessionRouterStore()
    # No sticky pre-set.
    chat = QueenChat(registry, _sub_call_recorder(calls),
                     classify=classify, session_state=state, session_id="S")
    res = await chat.handle("최신 논문 요약해줘")

    assert classifier_calls == 1
    assert res.responder == ["research"]
    assert res.routing == ROUTE_LLM
    # After the first successful delegation sticky pins to that Sub.
    assert state.get_sticky("S") == ["research"]
