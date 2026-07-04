"""Unit tests for the Queen orchestrator: routing, task_id, delegation."""

from __future__ import annotations

import re

import pytest

from nanobot.queen.orchestrator import (
    Orchestrator,
    Router,
    RoutingDecision,
    RoutingRule,
    SubResult,
    generate_task_id,
)

TASK_ID_RE = re.compile(r"^task_\d{8}_\d{6}_[a-z0-9]{4}$")


# --- task_id format --------------------------------------------------------


def test_task_id_format():
    tid = generate_task_id()
    assert TASK_ID_RE.match(tid), tid


def test_task_id_unique_enough():
    ids = {generate_task_id() for _ in range(200)}
    # rand4 suffix should keep collisions rare within the same second
    assert len(ids) > 190


# --- routing: rule-first, ambiguous -> direct ------------------------------


def _router() -> Router:
    return Router([
        RoutingRule(sub_id="coder", keywords=("코드", "code", "refactor")),
        RoutingRule(sub_id="writer", keywords=("글", "write", "essay")),
    ])


def test_route_no_match_is_direct():
    d = _router().decide("오늘 날씨 어때?")
    assert d.kind == "direct"
    assert d.sub_ids == ()


def test_route_single_match_delegates():
    d = _router().decide("이 code 좀 refactor 해줘")
    assert d.kind == "delegate"
    assert d.sub_ids == ("coder",)


def test_route_multiple_match_is_ambiguous_direct():
    # matches both 'code' (coder) and 'write' (writer) -> ambiguous -> direct
    d = _router().decide("write some code")
    assert d.kind == "direct"
    assert "ambiguous" in d.reason


def test_route_same_sub_via_multiple_keywords_still_single():
    d = _router().decide("코드 refactor")  # both keywords map to the same sub
    assert d.kind == "delegate"
    assert d.sub_ids == ("coder",)


# --- orchestration ---------------------------------------------------------


@pytest.fixture
def fixed_id():
    return lambda: "task_20260101_000000_abcd"


async def test_direct_path_calls_direct_handler(fixed_id):
    async def direct_call(text: str) -> str:
        return f"direct: {text}"

    async def sub_call(sub_id, task_id, text):  # should not be used
        raise AssertionError("sub_call must not run on direct path")

    orch = Orchestrator(_router(), sub_call, direct_call, id_factory=fixed_id)
    res = await orch.handle("그냥 잡담")
    assert res.handled == "direct"
    assert res.content == "direct: 그냥 잡담"
    assert res.task_id is None


async def test_single_sub_result_returned_verbatim(fixed_id):
    seen = {}

    async def direct_call(text: str) -> str:
        raise AssertionError("direct_call must not run on delegate path")

    async def sub_call(sub_id, task_id, text):
        seen["args"] = (sub_id, task_id, text)
        return SubResult(sub_id=sub_id, task_id=task_id, content="SUB SAYS HI",
                         usage={"total_tokens": 7})

    orch = Orchestrator(_router(), sub_call, direct_call, id_factory=fixed_id)
    res = await orch.handle("code refactor 부탁")

    assert res.handled == "delegate"
    assert res.task_id == "task_20260101_000000_abcd"
    assert res.content == "SUB SAYS HI"     # verbatim, no integration
    assert res.integrated is False
    assert len(res.sub_results) == 1
    # the same task_id was propagated to the Sub call
    assert seen["args"][0] == "coder"
    assert seen["args"][1] == "task_20260101_000000_abcd"


async def test_multiple_results_are_merged_without_integration_flag_false(fixed_id):
    # Build an orchestrator whose router yields a 2-sub delegate decision,
    # exercising the >1 merge branch.
    class TwoSubRouter(Router):
        def decide(self, text):
            return RoutingDecision("delegate", "forced", ("a", "b"))

    async def direct_call(text):
        raise AssertionError("not direct")

    async def sub_call(sub_id, task_id, text):
        return SubResult(sub_id=sub_id, task_id=task_id, content=f"{sub_id}-done")

    orch = Orchestrator(TwoSubRouter(), sub_call, direct_call, id_factory=fixed_id)
    res = await orch.handle("anything")
    assert res.integrated is True
    assert "[a] a-done" in res.content
    assert "[b] b-done" in res.content
