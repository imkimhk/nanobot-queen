"""Unit tests for the two-stage web-search augment (research-capable Subs).

Every test injects a fake ``provider`` and a fake ``search_impl`` so no HTTP,
no OAuth, no DDGS import is required. The default DuckDuckGo path is checked
lightly (import path only) in a smoke test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from nanobot.queen.web_augment import (
    WEB_CAPABILITY,
    maybe_augment_with_web_search,
)


@dataclass
class _FakeReply:
    content: str
    usage: dict[str, int]


class _FakeProvider:
    """Records probe calls and returns scripted replies in order."""

    def __init__(self, replies: list[_FakeReply | Exception]):
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, model=None, **_kwargs):
        self.calls.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _reply(text: str, tokens: int = 12) -> _FakeReply:
    return _FakeReply(content=text, usage={"prompt_tokens": tokens, "completion_tokens": 2, "total_tokens": tokens + 2})


# ---------------------------------------------------------------------------
# NONE case — probe declines, no search runs, user_text passes through
# ---------------------------------------------------------------------------


async def test_web_augment_none_case_returns_original_text():
    prov = _FakeProvider([_reply("NONE")])
    search_calls: list[tuple[str, int]] = []

    async def fake_search(q, n):
        search_calls.append((q, n))
        return "should not be called"

    txt, tel = await maybe_augment_with_web_search(
        "2+2 는?", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=fake_search,
    )
    assert txt == "2+2 는?"                # unmodified
    assert search_calls == []              # search NEVER ran
    assert tel["eligible"] is True
    assert tel["probe_kind"] == "none"
    assert tel["search_ran"] is False
    assert tel["probe_usage"]["total_tokens"] == 14


# ---------------------------------------------------------------------------
# Query case — probe returns a clean query, search runs, results prepended
# ---------------------------------------------------------------------------


async def test_web_augment_query_case_prepends_search_results():
    prov = _FakeProvider([_reply("서울 날씨 오늘")])

    async def fake_search(q, n):
        assert q == "서울 날씨 오늘"
        assert n == 5
        return "1. [기상청](https://weather.go.kr) - 맑음, 25°C"

    txt, tel = await maybe_augment_with_web_search(
        "오늘 서울 날씨 알려줘", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=fake_search,
    )
    assert txt.startswith("[web_search 결과 (검색어: 서울 날씨 오늘)]")
    assert "기상청" in txt
    assert "[사용자 원문 요청]\n오늘 서울 날씨 알려줘" in txt
    assert tel["probe_kind"] == "query"
    assert tel["search_ran"] and tel["search_ok"]
    assert tel["query"] == "서울 날씨 오늘"


# ---------------------------------------------------------------------------
# Capability off — no probe, no search, wrapper is a pure passthrough
# ---------------------------------------------------------------------------


async def test_web_augment_capability_off_never_calls_provider():
    prov = _FakeProvider([])   # empty — any provider call would raise IndexError

    async def fake_search(q, n):
        raise AssertionError("search must not run for non-web capability")

    txt, tel = await maybe_augment_with_web_search(
        "함수 짜줘", capabilities=["code.write"],
        provider=prov, model="m", search_impl=fake_search,
    )
    assert txt == "함수 짜줘"
    assert prov.calls == []                # provider NEVER called
    assert tel["eligible"] is False
    assert tel["probe_kind"] == "skip"


# ---------------------------------------------------------------------------
# Safety filter — probe leaked prose; must reject, no search, no augment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prose", [
    # too many newlines → prose
    "웹 검색이 필요해 보입니다.\n검색어는:\n서울 날씨\n\n이렇게 하면 좋겠어요.",
    # single line but > _QUERY_MAX_LEN (200) chars (each "서울 " = 3 code points)
    "서울 " * 80,
    # empty / whitespace-only reply
    "   \n\n   ",
])
async def test_web_augment_safety_filter_rejects_prose_reply(prose):
    prov = _FakeProvider([_reply(prose)])
    search_calls: list[tuple[str, int]] = []

    async def fake_search(q, n):
        search_calls.append((q, n))
        return "unused"

    txt, tel = await maybe_augment_with_web_search(
        "오늘 서울 날씨 알려줘", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=fake_search,
    )
    assert txt == "오늘 서울 날씨 알려줘"     # untouched
    assert search_calls == []                # search suppressed
    assert tel["probe_kind"] == "reject"


# ---------------------------------------------------------------------------
# Search failure — augment must instruct the Sub NOT to fabricate
# ---------------------------------------------------------------------------


async def test_web_augment_search_failure_signals_do_not_fabricate():
    prov = _FakeProvider([_reply("서울 지하철 요금 2026")])

    async def failing_search(q, n):
        return "Error: DuckDuckGo search failed (rate limited)"

    txt, tel = await maybe_augment_with_web_search(
        "2026 서울 지하철 요금", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=failing_search,
    )
    assert txt.startswith("[web_search 실패 (검색어: 서울 지하철 요금 2026)]")
    assert "추측이나 기억으로 답하지 마세요" in txt
    assert "[사용자 원문 요청]\n2026 서울 지하철 요금" in txt
    assert tel["search_ran"] is True
    assert tel["search_ok"] is False


async def test_web_augment_search_exception_signals_failure_too():
    prov = _FakeProvider([_reply("삼성전자 종가")])

    async def crashing_search(q, n):
        raise RuntimeError("network down")

    txt, tel = await maybe_augment_with_web_search(
        "삼성전자 오늘 종가", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=crashing_search,
    )
    assert "web_search 실패" in txt
    assert "network down" in txt
    assert tel["search_ran"] is True
    assert tel["search_ok"] is False


# ---------------------------------------------------------------------------
# Telemetry / usage log — probe_usage carried through, keys well-formed
# ---------------------------------------------------------------------------


async def test_web_augment_usage_log_shape_matches_contract():
    prov = _FakeProvider([_reply("파이썬 walrus 연산자", tokens=30)])

    async def ok_search(q, n):
        return "1. [PEP 572](https://peps.python.org/pep-0572/) - The := walrus"

    _txt, tel = await maybe_augment_with_web_search(
        "파이썬 walrus 연산자 뭐야?", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=ok_search,
    )
    # telemetry shape (matches usage-log doc in web_augment.py)
    assert set(tel) == {
        "eligible", "probe_kind", "query", "search_ran", "search_ok",
        "result_head", "probe_usage",
    }
    assert tel["eligible"] is True
    assert tel["probe_kind"] == "query"
    assert tel["query"] == "파이썬 walrus 연산자"
    assert tel["search_ran"] and tel["search_ok"]
    assert tel["probe_usage"]["total_tokens"] == 32
    assert "PEP 572" in tel["result_head"]


# ---------------------------------------------------------------------------
# Probe error — provider unreachable → passthrough (no fabricated augment)
# ---------------------------------------------------------------------------


async def test_web_augment_probe_error_falls_through_to_original_text():
    prov = _FakeProvider([RuntimeError("provider down")])

    async def fake_search(q, n):
        raise AssertionError("search must not run when probe failed")

    txt, tel = await maybe_augment_with_web_search(
        "오늘 서울 날씨", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=fake_search,
    )
    assert txt == "오늘 서울 날씨"
    assert tel["probe_kind"] == "probe_error"
    assert tel["search_ran"] is False


# ---------------------------------------------------------------------------
# β — anti-tool-call / anti-XML leak clause is present in BOTH augment paths
# ---------------------------------------------------------------------------


_ANTI_LEAK_MARKERS = ("<tool_call>", "<function>", "<invoke>", "이 turn에서는 도구를 다시 호출하지 않는다")


async def test_augment_success_contains_anti_tool_call_language():
    prov = _FakeProvider([_reply("서울 날씨")])

    async def ok_search(q, n):
        return "1. [기상청](https://weather.go.kr) - 맑음"

    txt, _ = await maybe_augment_with_web_search(
        "오늘 서울 날씨", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=ok_search,
    )
    for m in _ANTI_LEAK_MARKERS:
        assert m in txt, f"success augment missing anti-leak marker {m!r}"
    # sanity: user text still at the tail
    assert txt.endswith("[사용자 원문 요청]\n오늘 서울 날씨")


async def test_augment_failure_contains_anti_tool_call_language():
    prov = _FakeProvider([_reply("서울 지하철 요금")])

    async def failing_search(q, n):
        return "Error: DuckDuckGo search failed (rate limited)"

    txt, _ = await maybe_augment_with_web_search(
        "2026 지하철 요금", capabilities=[WEB_CAPABILITY],
        provider=prov, model="m", search_impl=failing_search,
    )
    for m in _ANTI_LEAK_MARKERS:
        assert m in txt, f"failure augment missing anti-leak marker {m!r}"
    # sanity: original "추측하지 마세요" contract preserved
    assert "추측이나 기억으로 답하지 마세요" in txt
