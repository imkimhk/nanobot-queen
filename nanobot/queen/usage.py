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
from dataclasses import dataclass, field
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
