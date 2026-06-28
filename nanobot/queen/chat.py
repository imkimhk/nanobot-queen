"""Queen User↔Sub chat wiring — connect the STEP 4 orchestrator to a real path.

This does NOT introduce a new router. It *wires* the existing
:mod:`nanobot.queen.orchestrator` (rule-first ``Router`` + ``Orchestrator``) into
the User→Sub gateway path:

  1. **rule-first** (``Router.decide``, 0 tokens) handles only the obvious cases;
  2. anything ambiguous escalates to a **Core LLM classifier** (delegation
     accuracy over tokens, per design decision 4);
  3. a **single** resolved Sub → passthrough (no Core orchestrator LLM in the
     answer path — "direct" per decision 3);
  4. **multiple** Subs → Core coordinates: gather each Sub's answer, then a Core
     LLM integration step combines them (decision 1).

The Sub call is an HTTP forward to the Sub's own ``/v1/chat/completions`` (the
Sub is a full nanobot ``serve`` instance), so the Sub does its own model work via
the gateway's existing Sub→Codex path. The classifier/integrator are injected so
this module stays unit-testable without any network or live model.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nanobot.queen.orchestrator import (
    Orchestrator,
    Router,
    RoutingDecision,
    RoutingRule,
    SubResult,
    generate_task_id,
)
from nanobot.queen.registry import STATUS_RUNNING, SubRegistry

ROUTE_RULE = "rule"            # resolved by rule-first router (0 routing tokens)
ROUTE_LLM = "llm"              # resolved by Core LLM classifier
ROUTE_CORE_DIRECT = "core_direct"  # no Sub fit -> Core answers directly

# Obvious domain keywords per capability — kept tight so rule-first only fires on
# unambiguous requests; everything else escalates to the LLM classifier.
CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "research.web": ("조사", "리서치", "research", "검색", "찾아"),
    "research.summary": ("요약", "summarize", "summary"),
    "code.write": ("함수", "코드", "구현", "function", "코딩"),
    "code.review": ("리뷰", "review", "버그", "bug"),
    "writing.draft": ("초안", "draft", "작문"),
    "writing.edit": ("교정", "퇴고", "edit"),
    "data.analyze": ("분석", "analyze", "통계"),
    "data.viz": ("그래프", "차트", "chart", "시각화"),
    "planning.decompose": ("계획", "분해", "plan"),
    "idea.generate": ("아이디어", "idea", "브레인스토밍", "구상", "발상"),
    "idea.structure": ("아이디어 정리", "구조화"),
    "idea.evaluate": ("아이디어 평가", "장단점"),
}


def build_rule_router(registry: SubRegistry) -> Router:
    """Build a rule-first Router from the running Subs' capabilities."""
    rules: list[RoutingRule] = []
    for rec in registry.list():
        if rec.status != STATUS_RUNNING:
            continue
        kws: list[str] = []
        for cap in rec.capability:
            kws.extend(CAPABILITY_KEYWORDS.get(cap, ()))
        if kws:
            rules.append(RoutingRule(sub_id=rec.id, keywords=tuple(kws)))
    return Router(rules)


@dataclass
class ChatResult:
    content: str
    responder: list[str]       # sub_ids that answered (["core"] if direct)
    routing: str               # ROUTE_RULE | ROUTE_LLM | ROUTE_CORE_DIRECT
    multi: bool
    task_id: str | None
    sub_usage: dict            # aggregated Sub token usage
    routing_usage: dict        # tokens spent on Core LLM routing/integration
    latency_ms: int


# (text) -> (sub_ids, usage)         Core LLM picks which Sub(s) fit, or []
Classifier = Callable[[str, list], Awaitable[tuple[list[str], dict]]]
# (text, sub_results) -> (content, usage)   Core LLM merges multi-Sub answers
Integrator = Callable[[str, list[SubResult]], Awaitable[tuple[str, dict]]]
# (text) -> (content, usage)         Core answers directly when no Sub fits
CoreAnswer = Callable[[str], Awaitable[tuple[str, dict]]]


class _FixedRouter(Router):
    """Router that returns a pre-resolved decision (to reuse Orchestrator core)."""

    def __init__(self, decision: RoutingDecision):
        super().__init__([])
        self._decision = decision

    def decide(self, text: str) -> RoutingDecision:
        return self._decision


def _agg_usage(results: list[SubResult]) -> dict:
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for r in results:
        for k in out:
            out[k] += int((r.usage or {}).get(k, 0) or 0)
    return out


class QueenChat:
    """Routes a User message to the right Sub(s) and returns who answered."""

    def __init__(
        self,
        registry: SubRegistry,
        sub_call,                       # async (sub_id, task_id, text) -> SubResult
        *,
        rule_router: Router | None = None,
        classify: Classifier | None = None,
        integrate: Integrator | None = None,
        core_answer: CoreAnswer | None = None,
        id_factory: Callable[[], str] = generate_task_id,
    ):
        self.registry = registry
        self.sub_call = sub_call
        self.rule_router = rule_router if rule_router is not None else build_rule_router(registry)
        self.classify = classify
        self.integrate = integrate
        self.core_answer = core_answer
        self.id_factory = id_factory

    async def handle(self, text: str) -> ChatResult:
        started = time.monotonic()
        routing_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # 1) rule-first
        decision = self.rule_router.decide(text)
        if decision.kind == "delegate":
            routing = ROUTE_RULE
            sub_ids = list(decision.sub_ids)
        else:
            # 2) ambiguous -> Core LLM classifier (accuracy over tokens)
            routing = ROUTE_LLM
            sub_ids, cls_usage = ([], {})
            if self.classify is not None:
                subs = [r for r in self.registry.list() if r.status == STATUS_RUNNING]
                sub_ids, cls_usage = await self.classify(text, subs)
            for k in routing_usage:
                routing_usage[k] += int((cls_usage or {}).get(k, 0) or 0)

        # 3) no Sub fits -> Core answers directly
        if not sub_ids:
            content, usage = ("", {})
            if self.core_answer is not None:
                content, usage = await self.core_answer(text)
            for k in routing_usage:
                routing_usage[k] += int((usage or {}).get(k, 0) or 0)
            return ChatResult(content, ["core"], ROUTE_CORE_DIRECT, False, None,
                              {}, routing_usage, int((time.monotonic() - started) * 1000))

        # 4) delegate via the EXISTING Orchestrator (task_id, gather, verbatim/merge)
        orch = Orchestrator(
            _FixedRouter(RoutingDecision("delegate", "resolved", tuple(sub_ids))),
            self.sub_call,
            core_answer_unused,
            id_factory=self.id_factory,
        )
        result = await orch.handle(text)
        multi = len(result.sub_results) > 1

        content = result.content
        if multi and self.integrate is not None:
            # Core coordinates multiple Sub answers (decision 1)
            content, int_usage = await self.integrate(text, result.sub_results)
            for k in routing_usage:
                routing_usage[k] += int((int_usage or {}).get(k, 0) or 0)

        return ChatResult(
            content=content,
            responder=[r.sub_id for r in result.sub_results],
            routing=routing,
            multi=multi,
            task_id=result.task_id,
            sub_usage=_agg_usage(result.sub_results),
            routing_usage=routing_usage,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


async def core_answer_unused(text: str) -> str:  # pragma: no cover - never called
    """Placeholder direct_call for the Orchestrator (delegate path only)."""
    raise AssertionError("direct path should not run inside QueenChat delegation")


# ---------------------------------------------------------------------------
# Sub forwarder — passthrough a User message to a Sub's own /v1 endpoint
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402
from pathlib import Path  # noqa: E402


def default_key_lookup(registry: SubRegistry):
    """Return a function that reads a Sub's pre-shared key from its config.json."""
    def _lookup(sub_id: str) -> str | None:
        rec = registry.get(sub_id)
        if rec is None:
            return None
        try:
            cfg = _json.loads((Path(rec.workspace) / "config.json").read_text(encoding="utf-8"))
            return cfg["providers"]["custom"]["apiKey"]
        except (OSError, KeyError, ValueError):
            return None
    return _lookup


class SubForwarder:
    """Forwards a single User message to a Sub's HTTP ``/v1/chat/completions``."""

    def __init__(
        self,
        registry: SubRegistry,
        *,
        model: str,
        key_lookup=None,
        base_url: str = "http://127.0.0.1",
        post=None,
        timeout: float = 120.0,
    ):
        self.registry = registry
        self.model = model
        self.key_lookup = key_lookup or default_key_lookup(registry)
        self.base_url = base_url
        self._post = post or self._default_post
        self.timeout = timeout

    async def forward(self, sub_id: str, text: str, *, session_id: str | None, task_id: str) -> SubResult:
        rec = self.registry.get(sub_id)
        if rec is None or rec.status != STATUS_RUNNING:
            return SubResult(sub_id, task_id, "", ok=False,
                             error=f"sub {sub_id!r} not running")
        url = f"{self.base_url}:{rec.port}/v1/chat/completions"
        key = self.key_lookup(sub_id)
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": text}],
        }
        if session_id:
            body["session_id"] = session_id
        try:
            status, data = await self._post(url, key, body)
        except Exception as e:  # network failure
            return SubResult(sub_id, task_id, "", ok=False, error=str(e))
        if status != 200:
            return SubResult(sub_id, task_id, "", ok=False, error=f"HTTP {status}")
        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            pass
        return SubResult(sub_id, task_id, content, usage=data.get("usage", {}) or {})

    async def _default_post(self, url: str, key: str | None, body: dict):
        import httpx
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(url, headers=headers, json=body)
            try:
                data = r.json()
            except Exception:
                data = {}
            return r.status_code, data
