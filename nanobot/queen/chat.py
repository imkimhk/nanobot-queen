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

import os
import re
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
from nanobot.queen.session_state import STICKY_CORE, SessionRouterStore

ROUTE_RULE = "rule"            # resolved by rule-first router (0 routing tokens)
ROUTE_LLM = "llm"              # resolved by Core LLM classifier
ROUTE_CORE_DIRECT = "core_direct"  # no Sub fit -> Core answers directly
ROUTE_STICKY = "sticky"        # reused sticky Sub for follow-up turn (0 routing tokens)
ROUTE_HANDOFF = "handoff"      # sticky Sub returned OUT_OF_SCOPE -> re-classified

# Prefix returned by a Sub whose prompt tells it to reject out-of-scope requests
# with ``OUT_OF_SCOPE: <reason> | suggested_capability: <cap>`` (see factory.py).
_OUT_OF_SCOPE_PREFIX = "OUT_OF_SCOPE"

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

# Short high-collision Korean nouns — enforce boundary match so "코드" does not
# match "코드명", "함수" does not match "함수화" etc. Only these keywords are
# boundary-checked; longer or more specific keywords keep plain substring
# matching (they rarely collide with unrelated compound words).
_BOUNDARY_KEYWORDS: frozenset[str] = frozenset({
    "코드", "함수", "구현", "리뷰", "버그", "분석", "요약", "계획", "분해",
})

# Korean particles that legitimately attach right after a noun. If the boundary
# keyword is followed by one of these particles, the match is still considered
# valid — e.g. "코드를", "코드가", "함수는" should match while "코드명" should not.
_KOREAN_PARTICLES: tuple[str, ...] = (
    "를", "을", "이", "가", "은", "는", "의", "에", "와", "과", "만", "도",
    "로", "으로", "부터", "까지", "에서", "께", "한테", "보다", "처럼", "같이",
    "마다", "조차", "마저", "라도", "이나", "나",
)

# Precomputed boundary-checked regex per keyword. Uses Unicode \w so hangul is
# treated as word chars, then requires either (a) a non-word/EOF right after the
# keyword, or (b) one of the allowed particles as an immediate suffix.
_BOUNDARY_PARTICLES_RE = "|".join(re.escape(p) for p in _KOREAN_PARTICLES)
_BOUNDARY_PATTERNS: dict[str, "re.Pattern[str]"] = {
    kw: re.compile(
        rf"(?:^|(?<=\W)){re.escape(kw.lower())}(?=$|\W|{_BOUNDARY_PARTICLES_RE})",
        re.UNICODE,
    )
    for kw in _BOUNDARY_KEYWORDS
}


def _keyword_matches(keyword: str, text_lower: str) -> bool:
    """Substring match with boundary strengthening for short high-collision nouns.

    ``keyword`` is compared case-insensitively against ``text_lower`` (which the
    caller has already lowered exactly once). For keywords in
    ``_BOUNDARY_KEYWORDS`` the match must be preceded by a non-word / start
    and followed by a non-word / end / one of the allowed Korean particles.
    All other keywords keep plain substring matching so English words and
    longer Korean phrases are unaffected.
    """
    pattern = _BOUNDARY_PATTERNS.get(keyword)
    if pattern is not None:
        return pattern.search(text_lower) is not None
    return keyword.lower() in text_lower


class _BoundaryRoutingRule(RoutingRule):
    """RoutingRule whose ``matches`` uses ``_keyword_matches`` (boundary-aware).

    Substring vs. boundary decision is made per-keyword (see
    ``_BOUNDARY_KEYWORDS``) so this stays fully backward compatible with the
    original substring-based semantics for every keyword *not* in that set.
    """

    def matches(self, text: str) -> bool:  # type: ignore[override]
        low = text.lower()
        return any(_keyword_matches(kw, low) for kw in self.keywords)


def build_rule_router(registry: SubRegistry) -> Router:
    """Build a rule-first Router from the running Subs' capabilities.

    Uses the boundary-aware ``_BoundaryRoutingRule`` so short Korean nouns like
    "코드"/"함수" no longer match inside compound words like "코드명"/"함수화".
    Semantics are unchanged for every keyword not in ``_BOUNDARY_KEYWORDS``.
    """
    rules: list[RoutingRule] = []
    for rec in registry.list():
        if rec.status != STATUS_RUNNING:
            continue
        kws: list[str] = []
        for cap in rec.capability:
            kws.extend(CAPABILITY_KEYWORDS.get(cap, ()))
        if kws:
            rules.append(_BoundaryRoutingRule(sub_id=rec.id, keywords=tuple(kws)))
    return Router(rules)


# ---------------------------------------------------------------------------
# Routing-input normalization (Telegram bracket-tag stripping)
# ---------------------------------------------------------------------------

# Known Telegram-generated bracket-prefix tags. Any leading bracket whose tag
# name starts with one of these is stripped from the routing text so its inner
# substring does not misroute the turn (e.g. the "코드" inside a reply-to quote
# should not drag the message to the coder Sub).
#
#   [Reply to bot: …] / [Reply to @user: …] / [Reply to Alice: …] / [Reply to: …]
#   [transcription: …] / [image: …] / [voice: …] / [audio: …] / [video: …]
#   [animation: …] / [file: …] / [document: …] / [location: …]
_ROUTING_STRIP_TAG_PREFIXES: tuple[str, ...] = (
    "reply to", "transcription", "image", "voice", "audio",
    "video", "animation", "file", "document", "location",
)

# Match a leading ``[tag: inner]`` block terminated by a newline or end-of-input.
# ``tag`` does not contain ``:`` or ``]``. ``inner`` is non-greedy but requires
# the closing ``]`` to be followed by whitespace-then-newline (or EOF), so a
# nested bracket like "[Reply to bot: [Research] ZEBRA…]" is captured whole
# instead of being cut off at the inner ``]``. Anchored to the start of input
# so only leading prefix tags are stripped.
_PREFIX_TAG_RE = re.compile(
    r"^\s*\[(?P<tag>[^\]:]+?)\s*:\s*(?P<inner>.*?)\]\s*(?:\n|\Z)",
    re.DOTALL,
)


def _normalize_for_routing(text: str) -> tuple[str, str]:
    """Strip leading Telegram-style bracket prefix tags for routing decisions.

    Returns ``(primary_text, quoted_text)`` where:

    * ``primary_text`` is the user's actual new-turn content with all known
      prefix tags removed. This is what the rule-first router should see.
    * ``quoted_text`` is the concatenated inner text of any ``[Reply to …]``
      tags — useful context that the LLM classifier already receives via the
      original message, but the rule router intentionally ignores so a bot's
      earlier "예시 코드" quote does not force this turn to the coder Sub.

    The Sub itself always receives the *original* text (with tags intact) —
    this normalization only affects routing decisions inside QueenChat.
    """
    if not text:
        return "", ""
    quoted_parts: list[str] = []
    remaining = text
    while True:
        m = _PREFIX_TAG_RE.match(remaining)
        if not m:
            break
        tag_low = m.group("tag").strip().lower()
        if not any(tag_low.startswith(pref) for pref in _ROUTING_STRIP_TAG_PREFIXES):
            break  # unknown bracket — leave the whole prefix as user content
        if tag_low.startswith("reply to"):
            quoted_parts.append(m.group("inner").strip())
        remaining = remaining[m.end():]
    return remaining.strip(), "\n".join(quoted_parts).strip()


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


# Search window for the tolerant OUT_OF_SCOPE match. Small enough that a
# legitimate answer that happens to discuss the marker in a later paragraph
# will not accidentally trigger a handoff, large enough to accommodate a short
# apology / preface that the LLM sometimes adds before the marker.
_OOS_TOLERANT_WINDOW = 200


def _is_out_of_scope(content: str) -> bool:
    """True iff the Sub reply looks like the OUT_OF_SCOPE marker from factory.py.

    The prompt asks each Sub to reply with exactly
    ``OUT_OF_SCOPE: <reason> | suggested_capability: <cap>`` when a request
    is out of scope. Two matching tiers, both case-insensitive:

    1. **Strict** — after stripping leading whitespace and common markdown
       decorations (``` ` * _ > ``) the reply starts with ``OUT_OF_SCOPE``.
       This is the original contract kept for backward compatibility.
    2. **Tolerant** — the substring ``OUT_OF_SCOPE`` appears anywhere in the
       first ``_OOS_TOLERANT_WINDOW`` characters. LLMs sometimes add a short
       Korean/English preface (e.g. "죄송합니다. 이건 제 범위 밖입니다.
       OUT_OF_SCOPE: …") that the strict rule misses; the window keeps false
       positives away from legitimate mid-answer mentions of the marker.
    """
    if not content:
        return False
    stripped = content.lstrip().lstrip("`*_>").lstrip()
    upper = stripped.upper()
    if upper.startswith(_OUT_OF_SCOPE_PREFIX):
        return True
    return _OUT_OF_SCOPE_PREFIX in upper[:_OOS_TOLERANT_WINDOW]


def _detect_explicit_mention(text: str, running_sub_ids: list[str]) -> str | None:
    """Return the mentioned sub_id if the message opens with ``@<sub_id>``.

    Explicit ``@coder ...`` prefix is the user's escape hatch to override the
    sticky Sub for one turn (and pin the new Sub going forward). Case
    insensitive; only the very first token is honored so mid-message ``@…``
    references are ignored. Returns None when no mention is present or the
    mentioned Sub is not currently running.
    """
    if not text:
        return None
    head = text.lstrip().split(None, 1)[0]  # first whitespace-separated token
    if not head.startswith("@") or len(head) < 2:
        return None
    candidate = head[1:].rstrip(":,.-").lower()
    for sid in running_sub_ids:
        if sid.lower() == candidate:
            return sid
    return None


class QueenChat:
    """Routes a User message to the right Sub(s) and returns who answered.

    Sticky routing (added on top of the original rule-first + LLM classifier
    pipeline): when both ``session_state`` and ``session_id`` are provided, a
    session that has already resolved to a Sub reuses that Sub on follow-up
    turns instead of re-classifying every message. Only two conditions break
    the sticky bond:

      (a) the sticky Sub returns ``OUT_OF_SCOPE`` — the STEP 10-3 handoff runs
          a fresh classifier call (excluding that Sub) and re-routes this turn;
      (b) the user explicitly requests another Sub with an ``@<sub_id>``
          prefix at the very start of the message.

    Passing neither ``session_state`` nor ``session_id`` keeps the original
    stateless behavior so existing callers/tests are unaffected.
    """

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
        session_state: SessionRouterStore | None = None,
        session_id: str | None = None,
        first_turn_rule_first: bool | None = None,
    ):
        self.registry = registry
        self.sub_call = sub_call
        self.rule_router = rule_router if rule_router is not None else build_rule_router(registry)
        self.classify = classify
        self.integrate = integrate
        self.core_answer = core_answer
        self.id_factory = id_factory
        self.session_state = session_state
        self.session_id = session_id
        # B: On the *first* turn of a session-active chat, skip the rule-first
        # router and go straight to the LLM classifier. Rule-first uses short
        # substring matches that are prone to Telegram-style false positives
        # (e.g. "[Reply to bot: … 코드 …]"), so trading a few classifier tokens
        # for correctness on the *first* turn is worth it. Follow-up turns
        # continue to skip routing entirely thanks to sticky.
        #
        # Priority: explicit constructor arg > QUEEN_FIRST_TURN_RULE_FIRST env
        # var > default (False, i.e. LLM-first). Set to True to restore the
        # pre-fix behavior for tests / diagnostics.
        if first_turn_rule_first is None:
            env_val = os.environ.get("QUEEN_FIRST_TURN_RULE_FIRST", "").strip().lower()
            first_turn_rule_first = env_val in ("1", "true", "yes", "on")
        self.first_turn_rule_first = bool(first_turn_rule_first)

    # -- routing helpers ----------------------------------------------------

    def _sticky_active(self) -> bool:
        return self.session_state is not None and bool(self.session_id)

    def _remember_sticky(self, sub_ids: list[str]) -> None:
        """Pin single-Sub or core-direct outcomes; multi-Sub replies do not stick.

        Multi-Sub answers usually reflect an ambiguous first turn; leaving them
        un-sticky lets the next turn re-classify cleanly instead of always
        fanning out to all previously-matched Subs.
        """
        if not self._sticky_active():
            return
        if len(sub_ids) == 1:
            self.session_state.set_sticky(self.session_id, sub_ids)  # type: ignore[union-attr]

    async def _classify_running(self, text: str, exclude: list[str] | None = None) -> tuple[list[str], dict]:
        """Ask the Core LLM classifier over the current running Subs (minus exclusions)."""
        if self.classify is None:
            return [], {}
        subs = [r for r in self.registry.list() if r.status == STATUS_RUNNING]
        if exclude:
            excluded = {s.lower() for s in exclude}
            subs = [r for r in subs if r.id.lower() not in excluded]
        sub_ids, cls_usage = await self.classify(text, subs)
        return list(sub_ids), (cls_usage or {})

    async def _core_direct(
        self,
        text: str,
        routing_usage: dict,
        started: float,
    ) -> ChatResult:
        """Common path when Core answers a turn directly. Pins sticky to Core."""
        content, usage = ("", {})
        if self.core_answer is not None:
            content, usage = await self.core_answer(text)
        for k in routing_usage:
            routing_usage[k] += int((usage or {}).get(k, 0) or 0)
        self._remember_sticky([STICKY_CORE])
        return ChatResult(
            content, [STICKY_CORE], ROUTE_CORE_DIRECT, False, None,
            {}, routing_usage, int((time.monotonic() - started) * 1000),
        )

    async def _run_delegation(self, text: str, sub_ids: list[str], routing_usage: dict):
        """Delegate to sub_ids via the Orchestrator, integrating multi results."""
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
            content, int_usage = await self.integrate(text, result.sub_results)
            for k in routing_usage:
                routing_usage[k] += int((int_usage or {}).get(k, 0) or 0)
        return result, multi, content

    # -- entry point --------------------------------------------------------

    async def handle(self, text: str) -> ChatResult:
        started = time.monotonic()
        routing_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # A: strip Telegram-style bracket prefix tags for routing decisions
        # only. ``text`` itself (passed to the Sub) is unchanged. If the entire
        # message was prefix tags (e.g. a lone ``[transcription: …]``),
        # ``primary_text`` is empty — that is intentional: rule / @mention then
        # have no user body to bite on and routing escalates to the LLM
        # classifier, which still sees the full original ``text``.
        primary_text, _quoted_text = _normalize_for_routing(text)
        routing_text = primary_text

        running_sub_ids = [r.id for r in self.registry.list() if r.status == STATUS_RUNNING]
        mention = _detect_explicit_mention(routing_text, running_sub_ids)
        sticky_sub_ids = self.session_state.get_sticky(self.session_id) if self._sticky_active() else None  # type: ignore[union-attr]

        # ---- Route selection --------------------------------------------
        # (b) user-side explicit switch: ``@<sub_id> …`` takes priority even
        # over the sticky bond so the user can always override.
        if mention is not None:
            sub_ids: list[str] = [mention]
            routing = ROUTE_RULE
        elif sticky_sub_ids is not None and sticky_sub_ids != [STICKY_CORE]:
            # Named-Sub sticky: reuse the resolved Sub without re-classifying.
            # This prevents the ZEBRA-style false-switch on follow-ups like
            # "그 코드명 뭐였지?" — no rule-first substring match runs here.
            sub_ids = list(sticky_sub_ids)
            routing = ROUTE_STICKY
        elif sticky_sub_ids == [STICKY_CORE]:
            # Sticky pinned to the Core-direct sentinel is treated as provisional
            # ("no Sub fit *last* turn"): we re-ask the classifier so a follow-up
            # that actually needs a Sub (e.g. "웹 검색 해줘" → research) can
            # escape the Core bond. If the classifier still returns [] the code
            # below falls into the Core-direct branch and keeps sticky=[core].
            routing = ROUTE_LLM
            sub_ids, cls_usage = await self._classify_running(text)
            for k in routing_usage:
                routing_usage[k] += int(cls_usage.get(k, 0) or 0)
        elif self._sticky_active() and not self.first_turn_rule_first:
            # B: session-active first turn — skip the rule-first router and go
            # straight to the LLM classifier. Rule-first substring matching is
            # too fragile for Telegram content (media tags, reply-to quotes),
            # and once the first turn sticks we never rerun rule anyway.
            routing = ROUTE_LLM
            sub_ids, cls_usage = await self._classify_running(text)
            for k in routing_usage:
                routing_usage[k] += int(cls_usage.get(k, 0) or 0)
        else:
            # Stateless mode (no session_state) OR
            # QUEEN_FIRST_TURN_RULE_FIRST=1 override: original rule-first path.
            # Rule uses the normalized primary text so prefix tags do not
            # trigger substring hits; classifier still sees the full text.
            decision = self.rule_router.decide(routing_text)
            if decision.kind == "delegate":
                routing = ROUTE_RULE
                sub_ids = list(decision.sub_ids)
            else:
                routing = ROUTE_LLM
                sub_ids, cls_usage = await self._classify_running(text)
                for k in routing_usage:
                    routing_usage[k] += int(cls_usage.get(k, 0) or 0)

        # ---- Core-direct path -------------------------------------------
        # No Sub fit, or a sticky ``core`` thread continues on Core.
        if not sub_ids or sub_ids == [STICKY_CORE]:
            return await self._core_direct(text, routing_usage, started)

        # ---- Delegate to Sub(s) -----------------------------------------
        result, multi, content = await self._run_delegation(text, sub_ids, routing_usage)

        # (a) STEP 10-3 handoff: on a single-Sub reply that is OUT_OF_SCOPE,
        # drop the sticky bond and re-classify this turn against the remaining
        # Subs. Multi-Sub replies are not re-tried — an ambiguous fan-out that
        # returns some OOS marker is left for the integrator to handle.
        if not multi and _is_out_of_scope(result.sub_results[0].content):
            oos_sub = sub_ids[0]
            if self._sticky_active():
                self.session_state.clear_sticky(self.session_id)  # type: ignore[union-attr]
            new_sub_ids, cls_usage = await self._classify_running(text, exclude=[oos_sub])
            for k in routing_usage:
                routing_usage[k] += int(cls_usage.get(k, 0) or 0)

            if not new_sub_ids:
                # No other Sub fits — fall back to Core-direct (also pins Core).
                return await self._core_direct(text, routing_usage, started)

            result2, multi2, content2 = await self._run_delegation(text, new_sub_ids, routing_usage)
            responders_after = [r.sub_id for r in result2.sub_results]
            self._remember_sticky(responders_after)
            return ChatResult(
                content=content2,
                responder=responders_after,
                routing=ROUTE_HANDOFF,
                multi=multi2,
                task_id=result2.task_id,
                sub_usage=_agg_usage(result2.sub_results),
                routing_usage=routing_usage,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        responders = [r.sub_id for r in result.sub_results]
        self._remember_sticky(responders)
        return ChatResult(
            content=content,
            responder=responders,
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
