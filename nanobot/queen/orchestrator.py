"""Queen orchestration — minimal direct-vs-delegate routing for Core.

Core decides, per inbound request, whether to handle it itself ("direct") or to
delegate it to a specialist Sub instance ("delegate"). The policy is
**rule-first**: a small ordered set of keyword rules maps a request to a Sub.

  * exactly one Sub matches  -> delegate to that Sub
  * no Sub matches           -> direct (Core handles it)
  * more than one Sub matches -> ambiguous -> direct (Core handles it)

Delegation generates a ``task_id`` (``task_{YYYYMMDD}_{HHMMSS}_{rand4}``), issues
a standard OpenAI call to each target Sub, and gathers the result(s). A single
Sub result is returned verbatim — no extra "integration" LLM call is made.

This module is transport-agnostic and fully unit-testable: the actual Sub call
and the direct-handling call are injected as async callables, so tests can run
without any network or live model.
"""

from __future__ import annotations

import asyncio
import random
import string
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

_RAND_ALPHABET = string.ascii_lowercase + string.digits


def generate_task_id(now: datetime | None = None) -> str:
    """Return a task id of the form ``task_{YYYYMMDD}_{HHMMSS}_{rand4}``."""
    now = now or datetime.now()
    rand4 = "".join(random.choices(_RAND_ALPHABET, k=4))
    return f"task_{now:%Y%m%d}_{now:%H%M%S}_{rand4}"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingRule:
    """Maps a request to a Sub when any keyword is present (case-insensitive)."""

    sub_id: str
    keywords: tuple[str, ...]

    def matches(self, text: str) -> bool:
        low = text.lower()
        return any(kw.lower() in low for kw in self.keywords)


@dataclass(frozen=True)
class RoutingDecision:
    kind: str  # "direct" | "delegate"
    reason: str
    sub_ids: tuple[str, ...] = ()


class Router:
    """Rule-first router. Ambiguous (multi-Sub) requests fall back to direct."""

    def __init__(self, rules: Sequence[RoutingRule] | None = None):
        self.rules: list[RoutingRule] = list(rules or [])

    def decide(self, text: str) -> RoutingDecision:
        matched: list[str] = [r.sub_id for r in self.rules if r.matches(text)]
        # de-duplicate while preserving order (a Sub may match via several rules)
        unique = list(dict.fromkeys(matched))
        if len(unique) == 1:
            return RoutingDecision("delegate", f"single rule match -> {unique[0]}", tuple(unique))
        if not unique:
            return RoutingDecision("direct", "no rule matched")
        return RoutingDecision("direct", f"ambiguous: multiple subs matched {unique}")


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


@dataclass
class SubResult:
    sub_id: str
    task_id: str
    content: str
    usage: dict[str, int] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None


# (sub_id, task_id, text) -> SubResult
SubCall = Callable[[str, str, str], Awaitable[SubResult]]
# (text) -> str   (Core handles the request itself)
DirectCall = Callable[[str], Awaitable[str]]


@dataclass
class OrchestrationResult:
    handled: str  # "direct" | "delegate"
    decision: RoutingDecision
    content: str
    task_id: str | None = None
    sub_results: list[SubResult] = field(default_factory=list)
    integrated: bool = False  # True only if an integration step combined >1 result


class Orchestrator:
    """Coordinates direct handling vs. delegation to Subs."""

    def __init__(
        self,
        router: Router,
        sub_call: SubCall,
        direct_call: DirectCall,
        *,
        id_factory: Callable[[], str] = generate_task_id,
    ):
        self.router = router
        self.sub_call = sub_call
        self.direct_call = direct_call
        self.id_factory = id_factory

    async def handle(self, text: str) -> OrchestrationResult:
        decision = self.router.decide(text)

        if decision.kind == "direct":
            content = await self.direct_call(text)
            return OrchestrationResult("direct", decision, content)

        task_id = self.id_factory()
        results = await asyncio.gather(
            *(self.sub_call(sub_id, task_id, text) for sub_id in decision.sub_ids)
        )

        # Single Sub result: return verbatim, no integration LLM call (per spec).
        if len(results) == 1:
            r = results[0]
            return OrchestrationResult(
                "delegate", decision, r.content, task_id=task_id,
                sub_results=list(results), integrated=False,
            )

        # Multiple results: minimal concatenation placeholder (no LLM call here).
        merged = "\n\n".join(f"[{r.sub_id}] {r.content}" for r in results)
        return OrchestrationResult(
            "delegate", decision, merged, task_id=task_id,
            sub_results=list(results), integrated=True,
        )
