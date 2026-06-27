"""Queen usage analysis — summarise the gateway's usage.jsonl.

Every relayed call carries a large, near-constant system-prompt cost (each Sub
ships its identity + bootstrap files + tool contract on every turn). This
module aggregates ``usage.jsonl`` so we can see that fixed per-call cost and how
it accumulates across Subs and calls — the empirical basis for deciding whether
prompt caching is worth adding.

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class SubUsage:
    sub_id: str
    calls: int = 0
    ok_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    blocked: int = 0  # invalid_key / concurrency_limited / etc.

    @property
    def avg_prompt_tokens(self) -> float:
        return self.prompt_tokens / self.ok_calls if self.ok_calls else 0.0


@dataclass
class UsageSummary:
    by_sub: dict[str, SubUsage] = field(default_factory=dict)
    total_calls: int = 0
    total_ok_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    blocked_calls: int = 0

    @property
    def avg_prompt_tokens(self) -> float:
        return self.total_prompt_tokens / self.total_ok_calls if self.total_ok_calls else 0.0

    def fixed_cost_estimate(self) -> dict:
        """Estimate the cacheable fixed per-call cost.

        The average prompt-token count on successful calls is dominated by the
        (near-constant) system prompt, so it is a good proxy for the per-call
        fixed cost that prompt caching would eliminate on repeat turns.
        """
        avg = self.avg_prompt_tokens
        return {
            "avg_prompt_tokens_per_call": round(avg, 1),
            "ok_calls": self.total_ok_calls,
            "fixed_prompt_tokens_total": round(avg * self.total_ok_calls),
            "share_of_prompt_tokens": (
                round(avg * self.total_ok_calls / self.total_prompt_tokens, 3)
                if self.total_prompt_tokens else 0.0
            ),
        }


_OK = "ok"
_BLOCKED_STATUSES = {"invalid_key", "missing_key", "sub_id_mismatch", "concurrency_limited"}


def summarize_usage(path: str | Path) -> UsageSummary:
    summary = UsageSummary()
    p = Path(path)
    if not p.exists():
        return summary
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        sub_id = d.get("sub_id") or "<none>"
        status = d.get("status")
        su = summary.by_sub.setdefault(sub_id, SubUsage(sub_id=sub_id))

        summary.total_calls += 1
        su.calls += 1
        if status in _BLOCKED_STATUSES:
            su.blocked += 1
            summary.blocked_calls += 1
            continue
        if status == _OK:
            su.ok_calls += 1
            summary.total_ok_calls += 1
            pt = int(d.get("prompt_tokens") or 0)
            ct = int(d.get("completion_tokens") or 0)
            tt = int(d.get("total_tokens") or 0)
            su.prompt_tokens += pt
            su.completion_tokens += ct
            su.total_tokens += tt
            summary.total_prompt_tokens += pt
            summary.total_completion_tokens += ct
            summary.total_tokens += tt
    return summary


# ---------------------------------------------------------------------------
# Operational metrics (for real-use observation; informs next priority)
# ---------------------------------------------------------------------------

_RATE_LIMIT_STATUSES = {"concurrency_limited", "upstream_429"}


@dataclass
class OpsSummary:
    """Day-by-day and routing/task mix metrics over the gateway usage log."""

    daily_tokens: dict[str, int] = field(default_factory=dict)   # date -> total_tokens
    daily_calls: dict[str, int] = field(default_factory=dict)    # date -> call count
    routing_mix: dict[str, int] = field(default_factory=dict)    # rule/llm/core_direct -> count
    task_mix: dict[str, int] = field(default_factory=dict)       # single/multi -> count
    queen_calls: int = 0          # total User->Sub calls
    escalated_calls: int = 0      # User->Sub calls that spent Core routing tokens
    rate_limit_events: int = 0
    rate_limit_by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def routing_llm_ratio(self) -> float:
        total = sum(self.routing_mix.values())
        return round(self.routing_mix.get("llm", 0) / total, 3) if total else 0.0

    @property
    def paid_routing_ratio(self) -> float:
        """Share of User->Sub calls that paid Core routing tokens (llm classify
        and/or core_direct/integrate) — the true cost of non-rule routing."""
        return round(self.escalated_calls / self.queen_calls, 3) if self.queen_calls else 0.0

    @property
    def multi_ratio(self) -> float:
        total = sum(self.task_mix.values())
        return round(self.task_mix.get("multi", 0) / total, 3) if total else 0.0


def _date_of(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "unknown"


def analyze_operations(path: str | Path) -> OpsSummary:
    """Aggregate operational metrics from the gateway usage.jsonl.

    Tracks (per design): daily cumulative tokens, rule/llm routing mix,
    single/multi task mix, and rate-limit events. Routing/task mix come from
    the User->Sub path (``sub_id == "queen"``); daily tokens span all calls.
    """
    s = OpsSummary()
    daily_tokens: dict[str, int] = defaultdict(int)
    daily_calls: dict[str, int] = defaultdict(int)
    routing: dict[str, int] = defaultdict(int)
    task: dict[str, int] = defaultdict(int)
    rl_kind: dict[str, int] = defaultdict(int)

    p = Path(path)
    if not p.exists():
        return s
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        date = _date_of(d.get("ts"))
        daily_calls[date] += 1
        daily_tokens[date] += int(d.get("total_tokens") or 0)

        status = d.get("status")
        if status in _RATE_LIMIT_STATUSES:
            s.rate_limit_events += 1
            rl_kind[status] += 1

        if d.get("sub_id") == "queen":
            s.queen_calls += 1
            if int(d.get("routing_tokens") or 0) > 0:
                s.escalated_calls += 1
            r = d.get("routing")
            if r:
                routing[r] += 1
            task["multi" if d.get("multi") else "single"] += 1

    s.daily_tokens = dict(daily_tokens)
    s.daily_calls = dict(daily_calls)
    s.routing_mix = dict(routing)
    s.task_mix = dict(task)
    s.rate_limit_by_kind = dict(rl_kind)
    return s


def format_ops_report(summary: OpsSummary) -> str:
    """Render a short human-readable operational report."""
    lines = ["=== Queen 운영 지표 ===", "", "[하루 누적 토큰]"]
    for date in sorted(summary.daily_tokens):
        lines.append(f"  {date}: {summary.daily_tokens[date]:,} tokens "
                     f"({summary.daily_calls.get(date, 0)} calls)")
    lines += ["", "[라우팅 mix (User->Sub)]"]
    for k in ("rule", "llm", "core_direct"):
        lines.append(f"  {k}: {summary.routing_mix.get(k, 0)}")
    lines.append(f"  llm 비율: {summary.routing_llm_ratio}  "
                 f"유료 라우팅(LLM 토큰 발생) 비율: {summary.paid_routing_ratio}")
    lines += ["", "[단일/다중 mix]",
              f"  single: {summary.task_mix.get('single', 0)}  "
              f"multi: {summary.task_mix.get('multi', 0)}  (multi 비율: {summary.multi_ratio})"]
    lines += ["", "[rate-limit]",
              f"  events: {summary.rate_limit_events}  by_kind: {summary.rate_limit_by_kind or '{}'}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m nanobot.queen.usage [usage.jsonl]`` — print the ops report."""
    import sys
    args = argv if argv is not None else sys.argv[1:]
    path = args[0] if args else str(Path.home() / ".nbq-core" / "usage.jsonl")
    print(format_ops_report(analyze_operations(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
