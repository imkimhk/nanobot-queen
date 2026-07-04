"""Unit tests for the Queen User->Sub chat wiring (orchestrator on the real path)."""

from __future__ import annotations

import pytest

from nanobot.queen.chat import (
    ROUTE_CORE_DIRECT,
    ROUTE_LLM,
    ROUTE_RULE,
    QueenChat,
    SubForwarder,
    build_rule_router,
)
from nanobot.queen.orchestrator import SubResult
from nanobot.queen.registry import STATUS_RUNNING, SubRecord, SubRegistry


@pytest.fixture
def registry(tmp_path):
    reg = SubRegistry(tmp_path / "subs.json")
    reg.register(SubRecord(id="research", role="research", capability=["research.web", "research.summary"],
                           port=8902, workspace=str(tmp_path / "r"), status=STATUS_RUNNING))
    reg.register(SubRecord(id="coder", role="coder", capability=["code.write", "code.review"],
                           port=8903, workspace=str(tmp_path / "c"), status=STATUS_RUNNING))
    return reg


def _sub_call_factory(record):
    async def sub_call(sub_id, task_id, text):
        record.append((sub_id, task_id, text))
        return SubResult(sub_id=sub_id, task_id=task_id, content=f"{sub_id}: done",
                         usage={"prompt_tokens": 6000, "completion_tokens": 10, "total_tokens": 6010})
    return sub_call


# --- rule-first single (no Core LLM) ---------------------------------------


async def test_rule_route_single_sub_no_core_llm(registry):
    calls = []

    async def classify(text, subs):  # must NOT be called on the rule path
        raise AssertionError("classifier must not run for a clear rule match")

    chat = QueenChat(registry, _sub_call_factory(calls), classify=classify)
    res = await chat.handle("이 함수를 구현해줘")  # '함수'/'구현' -> coder only
    assert res.routing == ROUTE_RULE
    assert res.responder == ["coder"]
    assert res.multi is False
    assert res.routing_usage["total_tokens"] == 0      # 0 routing tokens
    assert res.task_id and res.task_id.startswith("task_")
    assert calls and calls[0][0] == "coder"


# --- ambiguous -> Core LLM classifier --------------------------------------


async def test_ambiguous_escalates_to_llm_single(registry):
    calls = []

    async def classify(text, subs):
        return ["research"], {"prompt_tokens": 80, "completion_tokens": 4, "total_tokens": 84}

    chat = QueenChat(registry, _sub_call_factory(calls), classify=classify)
    res = await chat.handle("음... 뭔가 알아봐줘")  # no clear keyword
    assert res.routing == ROUTE_LLM
    assert res.responder == ["research"]
    assert res.routing_usage["total_tokens"] == 84
    assert res.multi is False


# --- multi -> Core integrates ----------------------------------------------


async def test_multi_sub_core_integrates(registry):
    calls = []

    async def classify(text, subs):
        return ["research", "coder"], {"total_tokens": 90, "prompt_tokens": 85, "completion_tokens": 5}

    async def integrate(text, results):
        assert len(results) == 2
        return "MERGED", {"total_tokens": 120, "prompt_tokens": 110, "completion_tokens": 10}

    chat = QueenChat(registry, _sub_call_factory(calls), classify=classify, integrate=integrate)
    res = await chat.handle("리서치하고 코드도 짜줘")
    assert res.multi is True
    assert set(res.responder) == {"research", "coder"}
    assert res.content == "MERGED"
    # routing tokens = classify + integrate
    assert res.routing_usage["total_tokens"] == 90 + 120
    # sub usage aggregated across both subs
    assert res.sub_usage["total_tokens"] == 6010 * 2


# --- no Sub fits -> Core answers directly ----------------------------------


async def test_no_fit_core_answers_directly(registry):
    async def classify(text, subs):
        return [], {"total_tokens": 50}

    async def core_answer(text):
        return "CORE", {"total_tokens": 200}

    chat = QueenChat(registry, _sub_call_factory([]), classify=classify, core_answer=core_answer)
    res = await chat.handle("아무 sub와도 무관한 요청")
    assert res.routing == ROUTE_CORE_DIRECT
    assert res.responder == ["core"]
    assert res.content == "CORE"
    assert res.routing_usage["total_tokens"] == 50 + 200


# --- rule router construction ----------------------------------------------


def test_build_rule_router_only_running(registry):
    registry.set_status("coder", "stopped")
    router = build_rule_router(registry)
    # '코드' keyword now has no running coder -> no match -> direct
    assert router.decide("코드 짜줘").kind == "direct"
    assert router.decide("조사해줘").sub_ids == ("research",)


# --- SubForwarder ----------------------------------------------------------


async def test_forwarder_success(registry):
    async def fake_post(url, key, body):
        assert ":8903/" in url  # coder port
        return 200, {"choices": [{"message": {"content": "hi"}}],
                     "usage": {"total_tokens": 42}}

    fwd = SubForwarder(registry, model="m", key_lookup=lambda s: "K", post=fake_post)
    r = await fwd.forward("coder", "x", session_id="s", task_id="t")
    assert r.ok and r.content == "hi" and r.usage["total_tokens"] == 42


async def test_forwarder_sub_not_running(registry):
    registry.set_status("coder", "stopped")
    fwd = SubForwarder(registry, model="m", key_lookup=lambda s: "K",
                       post=None)
    r = await fwd.forward("coder", "x", session_id=None, task_id="t")
    assert not r.ok and "not running" in r.error


async def test_forwarder_http_error(registry):
    async def fake_post(url, key, body):
        return 502, {}
    fwd = SubForwarder(registry, model="m", key_lookup=lambda s: "K", post=fake_post)
    r = await fwd.forward("research", "x", session_id=None, task_id="t")
    assert not r.ok and "502" in r.error
