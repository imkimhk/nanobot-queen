"""Unit tests for the Core-direct system prompt (identity + live Sub roster).

The Core-direct answerer (``core_answer_session`` inside ``handle_queen_chat``)
prepends a system message built by :func:`_build_core_system_message` so the
LLM knows (a) its own identity as the Queen orchestrator's Core and (b) the
exact set of Subs currently running — the latter must reflect registry state
at *call time*, not at gateway startup.

These tests exercise the helper directly and verify the end-to-end wiring by
driving the same code path that ``handle_queen_chat`` builds (session_state
present → ``core_answer_session`` closure) via ``QueenChat``.
"""

from __future__ import annotations

import pytest

from nanobot.queen.chat import ROUTE_CORE_DIRECT, QueenChat
from nanobot.queen.gateway import _build_core_system_message
from nanobot.queen.registry import STATUS_RUNNING, STATUS_STOPPED, SubRecord, SubRegistry
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
    reg.register(SubRecord(id="idea", role="idea",
                           capability=["idea.generate", "idea.structure", "idea.evaluate"],
                           port=8904, workspace=str(tmp_path / "i"),
                           status=STATUS_RUNNING))
    return reg


# ---------------------------------------------------------------------------
# _build_core_system_message — direct unit tests
# ---------------------------------------------------------------------------


def test_core_system_prompt_includes_identity(registry):
    """The prompt must state the Core identity + Sub-delegation policy."""
    msg = _build_core_system_message(registry)
    assert "여왕개미(Queen)" in msg
    assert "Core" in msg
    assert "위임" in msg          # 전문 분야는 Sub에게 위임한다
    assert "Sub 목록" in msg      # policy hint for the LLM


def test_core_system_prompt_lists_running_subs(registry):
    """Prompt must enumerate every running Sub with its capabilities."""
    msg = _build_core_system_message(registry)
    assert "research(research.web, research.summary)" in msg
    assert "coder(code.write, code.review)" in msg
    assert "idea(idea.generate, idea.structure, idea.evaluate)" in msg


def test_core_system_prompt_reflects_registry_change(registry):
    """Adding / removing / stopping Subs must reflect on the very next call.

    No caching is allowed — the prompt is a live snapshot of ``registry.list()``
    filtered by ``STATUS_RUNNING``.
    """
    # Baseline: all three running.
    before = _build_core_system_message(registry)
    assert "idea" in before

    # Stop 'idea' — it must disappear from the next prompt.
    registry.set_status("idea", STATUS_STOPPED)
    after_stop = _build_core_system_message(registry)
    assert "idea" not in after_stop
    assert "research" in after_stop and "coder" in after_stop

    # Add a brand new Sub — it must appear on the next call.
    registry.register(SubRecord(id="writer", role="writer",
                                capability=["writing.draft"],
                                port=8905, workspace="/tmp/w",
                                status=STATUS_RUNNING))
    after_add = _build_core_system_message(registry)
    assert "writer(writing.draft)" in after_add


def test_core_system_prompt_when_no_running_subs(tmp_path):
    """With an empty (or all-stopped) registry the prompt still ships identity."""
    empty = SubRegistry(tmp_path / "empty.json")
    msg = _build_core_system_message(empty)
    assert "여왕개미(Queen)" in msg
    assert "현재 사용 가능한 Sub: (없음)" in msg


# ---------------------------------------------------------------------------
# End-to-end: the core_answer_session wrapper prepends the system message,
# still appends history, still returns (content, usage).
# ---------------------------------------------------------------------------


def _make_provider(capture):
    """Fake provider that captures the messages list and returns a canned reply."""

    class _Resp:
        def __init__(self):
            self.content = "core reply"
            self.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    class _Provider:
        async def chat(self, *, messages, model, **_):
            capture.append({"messages": list(messages), "model": model})
            return _Resp()

    return _Provider()


def _make_core_answer_session(provider, registry, state, session_id, model_name="m"):
    """Mirrors the closure inside ``handle_queen_chat`` for direct testing."""

    async def core_answer_session(user_text: str):
        history = state.get_core_history(session_id)
        system_msg = _build_core_system_message(registry)
        messages = ([{"role": "system", "content": system_msg}]
                    + history + [{"role": "user", "content": user_text}])
        r = await provider.chat(messages=messages, model=model_name)
        content = r.content or ""
        state.append_core_history(session_id, "user", user_text)
        state.append_core_history(session_id, "assistant", content)
        return content, (r.usage or {})

    return core_answer_session


async def test_core_answer_session_prepends_system_and_keeps_history(registry):
    """First turn: system + user. Second turn: system + [user, assistant] + user."""
    capture: list = []
    provider = _make_provider(capture)
    state = SessionRouterStore()
    answer = _make_core_answer_session(provider, registry, state, "S")

    content1, _ = await answer("첫 질문")
    assert content1 == "core reply"

    # Turn 1: exactly one system + one user, no history yet.
    msgs1 = capture[0]["messages"]
    assert msgs1[0]["role"] == "system"
    assert "여왕개미(Queen)" in msgs1[0]["content"]
    assert msgs1[1] == {"role": "user", "content": "첫 질문"}
    assert len(msgs1) == 2

    await answer("두 번째 질문")

    # Turn 2: system + prior [user, assistant] + new user.
    msgs2 = capture[1]["messages"]
    assert msgs2[0]["role"] == "system"
    assert msgs2[1] == {"role": "user", "content": "첫 질문"}
    assert msgs2[2] == {"role": "assistant", "content": "core reply"}
    assert msgs2[3] == {"role": "user", "content": "두 번째 질문"}
    assert len(msgs2) == 4

    # History is durably appended (unchanged behaviour).
    hist = state.get_core_history("S")
    assert [m["content"] for m in hist] == ["첫 질문", "core reply", "두 번째 질문", "core reply"]


async def test_core_answer_session_reflects_registry_change_next_turn(registry):
    """A Sub stopped between turns disappears from the next turn's system message."""
    capture: list = []
    provider = _make_provider(capture)
    state = SessionRouterStore()
    answer = _make_core_answer_session(provider, registry, state, "S")

    await answer("첫 질문")
    assert "idea" in capture[0]["messages"][0]["content"]

    registry.set_status("idea", STATUS_STOPPED)
    await answer("두 번째 질문")
    sys_msg_2 = capture[1]["messages"][0]["content"]
    assert "idea" not in sys_msg_2
    assert "research" in sys_msg_2 and "coder" in sys_msg_2


async def test_core_direct_via_queenchat_uses_system_prompt(registry):
    """Drive the real ``QueenChat`` Core-direct path with the wrapped answerer.

    Verifies the system-message injection integrates end-to-end without
    breaking existing routing / sticky invariants for the Core-direct case.
    """
    capture: list = []
    provider = _make_provider(capture)
    state = SessionRouterStore()

    async def sub_call(sub_id, task_id, text):  # pragma: no cover - never called
        raise AssertionError("core-direct must not delegate to a Sub")

    async def classify(text, subs):
        return [], {"total_tokens": 5}  # force core-direct

    core_answer = _make_core_answer_session(provider, registry, state, "S")

    chat = QueenChat(
        registry, sub_call,
        classify=classify, core_answer=core_answer,
        session_state=state, session_id="S",
    )
    res = await chat.handle("서브 나노봇 뭐 있어?")

    assert res.responder == ["core"] and res.routing == ROUTE_CORE_DIRECT
    assert res.content == "core reply"
    msgs = capture[0]["messages"]
    assert msgs[0]["role"] == "system"
    for name in ("research", "coder", "idea"):
        assert name in msgs[0]["content"]
