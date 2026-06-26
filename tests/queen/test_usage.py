"""Unit tests for the Queen usage summariser."""

from __future__ import annotations

import json

from nanobot.queen.usage import summarize_usage


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_summarize_aggregates_per_sub_and_total(tmp_path):
    log = tmp_path / "usage.jsonl"
    _write(log, [
        {"sub_id": "research", "status": "ok", "prompt_tokens": 9000, "completion_tokens": 20, "total_tokens": 9020},
        {"sub_id": "research", "status": "ok", "prompt_tokens": 9100, "completion_tokens": 30, "total_tokens": 9130},
        {"sub_id": "coder", "status": "ok", "prompt_tokens": 8800, "completion_tokens": 10, "total_tokens": 8810},
        {"sub_id": None, "status": "invalid_key"},
        {"sub_id": "coder", "status": "concurrency_limited"},
    ])
    s = summarize_usage(log)

    assert s.total_calls == 5
    assert s.total_ok_calls == 3
    assert s.blocked_calls == 2
    assert s.by_sub["research"].ok_calls == 2
    assert s.by_sub["research"].total_tokens == 9020 + 9130
    assert s.by_sub["coder"].blocked == 1
    # average prompt tokens ~ the fixed system-prompt cost
    assert abs(s.avg_prompt_tokens - (9000 + 9100 + 8800) / 3) < 0.01


def test_fixed_cost_estimate(tmp_path):
    log = tmp_path / "usage.jsonl"
    _write(log, [
        {"sub_id": "a", "status": "ok", "prompt_tokens": 9000, "total_tokens": 9010},
        {"sub_id": "b", "status": "ok", "prompt_tokens": 9000, "total_tokens": 9010},
    ])
    est = summarize_usage(log).fixed_cost_estimate()
    assert est["avg_prompt_tokens_per_call"] == 9000.0
    assert est["ok_calls"] == 2
    assert est["fixed_prompt_tokens_total"] == 18000
    # nearly all prompt tokens are the fixed cost
    assert est["share_of_prompt_tokens"] == 1.0


def test_empty_or_missing_file(tmp_path):
    s = summarize_usage(tmp_path / "nope.jsonl")
    assert s.total_calls == 0
    assert s.fixed_cost_estimate()["avg_prompt_tokens_per_call"] == 0.0
