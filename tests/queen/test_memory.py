"""Unit tests for Queen unified memory promotion."""

from __future__ import annotations

import pytest

from nanobot.queen.memory import (
    IMPORTANCE_HIGH,
    IMPORTANCE_MEDIUM,
    CoreMemory,
    ImportancePolicy,
)


@pytest.fixture
def mem(tmp_path):
    return CoreMemory(tmp_path)


# --- importance policy -----------------------------------------------------


def test_policy_task_result_ok_is_medium():
    ok, imp, _ = ImportancePolicy().classify("task_result", "ok", "refactored auth module")
    assert ok and imp == IMPORTANCE_MEDIUM


def test_policy_task_result_failure_is_high():
    ok, imp, reason = ImportancePolicy().classify("task_result", "failed", "deploy failed: bad migration")
    assert ok and imp == IMPORTANCE_HIGH
    assert "failure" in reason


def test_policy_pattern_is_high():
    ok, imp, _ = ImportancePolicy().classify("pattern", "ok", "retries spike on 429 at >4 concurrency")
    assert ok and imp == IMPORTANCE_HIGH


def test_policy_short_summary_not_important():
    ok, _, _ = ImportancePolicy().classify("task_result", "ok", "ok")
    assert not ok


def test_policy_unknown_kind_not_important():
    ok, _, _ = ImportancePolicy().classify("chatter", "ok", "the weather is nice today")
    assert not ok


# --- promote / query -------------------------------------------------------


def test_promote_skips_unimportant_returns_none(mem):
    assert mem.promote("coder", "hi", kind="chatter") is None
    assert mem.query() == []


def test_promote_important_persists_and_reloads(mem, tmp_path):
    rec = mem.promote("coder", "implemented quicksort and added tests",
                      kind="task_result", status="ok", task_id="task_x")
    assert rec is not None and rec.importance == IMPORTANCE_MEDIUM
    # reload from disk
    again = CoreMemory(tmp_path).query(sub_id="coder")
    assert len(again) == 1
    assert again[0].summary == "implemented quicksort and added tests"
    assert again[0].task_id == "task_x"


def test_force_promotes_unimportant(mem):
    rec = mem.promote("coder", "small note here", kind="note", force=True)
    assert rec is not None
    assert "forced" in rec.reason


def test_query_filters(mem):
    mem.promote("coder", "task A result summary", kind="task_result", status="ok")
    mem.promote("coder", "deploy failed badly: rollback done", kind="task_result", status="failed")
    mem.promote("research", "found durable fact about X", kind="fact")
    assert len(mem.query()) == 3
    assert len(mem.query(sub_id="coder")) == 2
    assert len(mem.query(importance=IMPORTANCE_HIGH)) == 1
    assert len(mem.query(kind="fact")) == 1
