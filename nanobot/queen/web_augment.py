"""Two-stage web-search augment for research-capable Subs.

Motivation
----------
The Codex-via-ChatGPT backend does not honour ``response_format`` /
``text.format`` structured-output requests (experimental subscription
endpoint), so we cannot ask the model to *both* decide when to search and
return a strict JSON tool call. Instead we split the turn into two Codex
calls plus one deterministic Python-side search:

  1. **intent probe** — a single Codex call asks *only* whether a web search
     is needed. The prompt asks for a bare query on one line, or the literal
     token ``NONE`` when the model can answer from memory. No tool schema is
     provided.
  2. **safety filter** — the raw string is parsed conservatively:
     * ``NONE`` (case-insensitive, trim) → no search.
     * multi-line / very long / suspicious punctuation → treated as prose,
       no search. This shields the Sub from a chatty model that ignored the
       one-line contract.
     * otherwise the string is taken verbatim as the query.
  3. **live search** — the query is passed to nanobot's own DuckDuckGo path
     (``WebSearchTool._search_duckduckgo``). No new HTTP code here.
  4. **augment** — the (possibly truncated) result block is prepended to the
     original user text so the delegated Sub sees:
         ``[web_search 결과 (검색어: X)]\n<results>\n\n[사용자 원문 요청]\n<original>``
     On search failure we prepend a *do-not-fabricate* instruction instead.

The final "answer" Codex call is the Sub's own inference call — this module
never generates the user-visible answer directly. That preserves the Queen
routing/sticky/handoff invariants; augmenting is just a pre-processing pass
on the message body forwarded to the Sub.

Only Subs whose registered ``capability`` list contains ``research.web`` get
augmented. Every other Sub (coder, idea, …) sees the message unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

# Capability key that opts a Sub in to web-search augment.
WEB_CAPABILITY = "research.web"

# Intent-probe prompt: strict one-line contract. Kept short so the classifier
# call itself stays cheap in tokens.
_INTENT_PROMPT_SYSTEM = (
    "너는 라우터다. 아래 사용자 요청이 최신/외부 사실을 필요로 하는지 판정한다.\n"
    "필요하면 검색 엔진에 넣을 검색어만 **한 줄**로 답하라. 다른 설명·인용·부호·따옴표 금지.\n"
    "필요 없으면 정확히 대문자 'NONE' 한 단어만 답하라. 다른 말 붙이지 마라."
)
_INTENT_PROMPT_USER_TEMPLATE = "사용자 요청:\n{user_text}\n\n검색어 또는 NONE:"

# Safety filter thresholds on the intent-probe response.
_QUERY_MAX_LEN = 200
_QUERY_MAX_NEWLINES = 1
# The probe sometimes wraps the query in quotes or backticks; strip them.
_QUERY_TRIM_CHARS = " \t\r\n\"'`“”‘’「」"

# When the search adapter returns one of these prefixes the search failed and
# the augment must NOT invent facts — instead we tell the Sub to admit failure.
_SEARCH_FAILURE_PREFIXES = ("Error:", "No results")

# Search callable contract: ``async (query, n) -> results_str``. Returning a
# string keeps parity with WebSearchTool._search_duckduckgo which already
# formats titles/URLs/snippets — the Sub just needs a readable block.
SearchImpl = Callable[[str, int], Awaitable[str]]


def _is_search_failure(result: str) -> bool:
    return any(result.strip().startswith(p) for p in _SEARCH_FAILURE_PREFIXES)


def _clean_probe_reply(raw: str) -> str:
    """Trim quotes/backticks/whitespace the probe often adds around the query."""
    if not raw:
        return ""
    line = raw.strip()
    # If the model gave multiple lines despite the instructions, take the
    # first non-empty one — the rest is prose we would discard anyway.
    for candidate in line.splitlines():
        candidate = candidate.strip(_QUERY_TRIM_CHARS)
        if candidate:
            return candidate
    return ""


def _classify_probe(raw: str) -> tuple[str, str]:
    """Return ``(kind, query)`` where kind ∈ ``{"none", "query", "reject"}``.

    * ``none``   — probe answered NONE (case-insensitive). No search.
    * ``reject`` — probe answered something that fails the safety filter
                   (too long, too many newlines, or empty after cleanup).
                   No search — the Sub sees the original text unchanged.
    * ``query``  — probe returned a clean single-line query to search for.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return "reject", ""
    # Cheap length guard *before* per-line cleanup so we do not accept a
    # thousand-char paragraph that happens to have a short first line.
    if len(stripped) > _QUERY_MAX_LEN:
        return "reject", ""
    if stripped.count("\n") > _QUERY_MAX_NEWLINES:
        return "reject", ""
    query = _clean_probe_reply(stripped)
    if not query:
        return "reject", ""
    if query.upper() == "NONE":
        return "none", ""
    return "query", query


# Shared anti-leak clause added to both success and failure augments. It exists
# because the Codex-via-ChatGPT backend sometimes echoes the ambient tool-call
# vocabulary (``<tool_call>…</tool_call>``, ``<function …/>``, ``<invoke …>``)
# into the assistant's final content when the prompt hints at a "search" turn.
# We make the anti-echo instruction very explicit and put it *close to* the
# original user request so it stays in the model's immediate attention window.
_ANTI_TOOL_LEAK_CLAUSE = (
    "[주의] 답변 지침\n"
    "- 검색은 이미 완료되었다. 답변 안에 `<tool_call>`, `</tool_call>`, "
    "`<function>`, `<invoke>`, `<tool_name …/>` 같은 XML/도구 호출 태그를 "
    "**절대** 포함하지 마라. 이 turn에서는 도구를 다시 호출하지 않는다.\n"
    "- 재검색을 요청하는 문구나 도구 호출 스키마를 답변 텍스트에 삽입하지 마라.\n"
    "- 결과가 부족해도 새로운 도구 호출 시도 문장을 넣지 말고, 정직하게 "
    "정보 부족을 사용자에게 알려라. 마크다운 링크·인용·표는 그대로 써도 된다."
)


def _augment_success(user_text: str, query: str, results: str) -> str:
    """Augment message when the search succeeded.

    Anti-leak clause is placed *between* the search results and the original
    user request so the model sees "results → don't leak tool syntax → user
    question" in immediate order.
    """
    return (
        f"[web_search 결과 (검색어: {query})]\n"
        f"{results}\n\n"
        f"{_ANTI_TOOL_LEAK_CLAUSE}\n"
        f"- 제공된 검색 결과만을 근거로 답하고, 결과가 부족하면 "
        f"'제공된 결과만으로는 확답이 어렵다'라고 정직히 말하라. "
        f"기억이나 추측으로 보충하지 마라.\n\n"
        f"[사용자 원문 요청]\n{user_text}"
    )


def _augment_failure(user_text: str, query: str, err: str) -> str:
    """Augment message when the search failed. Same anti-leak clause applies."""
    return (
        f"[web_search 실패 (검색어: {query})]\n"
        f"검색을 시도했으나 결과를 얻지 못했습니다 ({err.strip() or 'unknown error'}).\n\n"
        f"{_ANTI_TOOL_LEAK_CLAUSE}\n"
        "- **추측이나 기억으로 답하지 마세요.** 이 사실을 사용자에게 명확히 알리고, "
        "최신 정보가 필요한 질문이면 사용자가 URL을 직접 붙여넣어 달라고 요청하세요.\n\n"
        f"[사용자 원문 요청]\n{user_text}"
    )


async def _default_search_impl(query: str, n: int) -> str:
    """Reuse WebSearchTool's DuckDuckGo path without touching upstream code.

    ``WebSearchTool()`` initialises with a default ``WebSearchConfig`` whose
    provider is already ``"duckduckgo"``; we call the internal helper directly
    to bypass the tool-schema machinery. If the import fails at runtime (e.g.
    Sub agent tools not installed) the caller sees a failure result string.
    """
    try:
        from nanobot.agent.tools.web import WebSearchTool
    except ImportError as e:
        return f"Error: web tool unavailable ({e})"
    tool = WebSearchTool()
    return await tool._search_duckduckgo(query, n)  # noqa: SLF001 — deliberate reuse


async def maybe_augment_with_web_search(
    user_text: str,
    capabilities: Iterable[str],
    provider: Any,
    model: str,
    *,
    search_impl: SearchImpl | None = None,
    n_results: int = 5,
    probe_timeout_s: float = 20.0,
) -> tuple[str, dict[str, Any]]:
    """Two-stage augment. Returns ``(possibly_augmented_text, telemetry)``.

    ``telemetry`` is a plain dict with keys:
      * ``eligible``         — True when the Sub had ``research.web`` capability
      * ``probe_kind``       — ``"none"|"query"|"reject"|"probe_error"|"skip"``
      * ``query``            — the query string used (empty when not searched)
      * ``search_ran``       — True iff DuckDuckGo was actually called
      * ``search_ok``        — True iff results returned without an error/empty
      * ``result_head``      — first 200 chars of the results (or error), for logs
      * ``probe_usage``      — usage dict returned by the intent-probe Codex call

    When the Sub is not web-capable, or the probe rejects, the returned text
    is *identical* to ``user_text`` and no search runs.
    """
    telemetry: dict[str, Any] = {
        "eligible": False, "probe_kind": "skip", "query": "",
        "search_ran": False, "search_ok": False, "result_head": "",
        "probe_usage": {},
    }
    if WEB_CAPABILITY not in set(capabilities or []):
        return user_text, telemetry
    telemetry["eligible"] = True

    # ── stage 1: intent probe (one short Codex call, no tools) ─────────────
    probe_messages = [
        {"role": "system", "content": _INTENT_PROMPT_SYSTEM},
        {"role": "user", "content": _INTENT_PROMPT_USER_TEMPLATE.format(user_text=user_text)},
    ]
    try:
        probe = await asyncio.wait_for(
            provider.chat(messages=probe_messages, model=model),
            timeout=probe_timeout_s,
        )
    except Exception as e:
        # Probe unreachable → treat as "no search" (safer than fabricating).
        telemetry["probe_kind"] = "probe_error"
        telemetry["result_head"] = f"probe error: {type(e).__name__}: {e}"[:200]
        return user_text, telemetry

    telemetry["probe_usage"] = getattr(probe, "usage", None) or {}
    raw = getattr(probe, "content", None) or ""
    kind, query = _classify_probe(raw)
    telemetry["probe_kind"] = kind
    telemetry["query"] = query

    if kind != "query":
        return user_text, telemetry

    # ── stage 2: run the search (Python-side, deterministic) ───────────────
    do_search = search_impl or _default_search_impl
    try:
        results = await do_search(query, n_results)
    except Exception as e:
        telemetry["search_ran"] = True
        telemetry["result_head"] = f"exception: {type(e).__name__}: {e}"[:200]
        return _augment_failure(user_text, query, f"{type(e).__name__}: {e}"), telemetry

    telemetry["search_ran"] = True
    telemetry["result_head"] = (results or "")[:200]
    if _is_search_failure(results or ""):
        return _augment_failure(user_text, query, results), telemetry

    telemetry["search_ok"] = True
    return _augment_success(user_text, query, results), telemetry
