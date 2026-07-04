"""Unit tests for the Queen Sub registry."""

from __future__ import annotations

import json

import pytest

from nanobot.queen.registry import (
    MODE_ALWAYS,
    STATUS_RUNNING,
    STATUS_STOPPED,
    SubRecord,
    SubRegistry,
)


@pytest.fixture
def reg_path(tmp_path):
    return tmp_path / "subs.json"


def _research(**over) -> SubRecord:
    base = dict(
        id="research", role="리서치 전문가",
        capability=["research.web", "research.summary"],
        port=8901, workspace="/ws/research", mode=MODE_ALWAYS, prompt_version="v1",
    )
    base.update(over)
    return SubRecord(**base)


def test_register_and_get_roundtrip(reg_path):
    reg = SubRegistry(reg_path)
    reg.register(_research())
    # reload from disk to prove persistence
    reg2 = SubRegistry(reg_path)
    rec = reg2.get("research")
    assert rec is not None
    assert rec.capability == ["research.web", "research.summary"]
    assert rec.port == 8901
    assert rec.mode == MODE_ALWAYS
    assert rec.prompt_version == "v1"
    assert rec.status == STATUS_STOPPED


def test_register_is_idempotent_upsert(reg_path):
    reg = SubRegistry(reg_path)
    reg.register(_research())
    reg.register(_research(role="updated role"))
    assert len(reg.list()) == 1
    assert reg.get("research").role == "updated role"


def test_set_status_and_pid(reg_path):
    reg = SubRegistry(reg_path)
    reg.register(_research())
    reg.set_status("research", STATUS_RUNNING, pid=4321)
    rec = SubRegistry(reg_path).get("research")
    assert rec.status == STATUS_RUNNING
    assert rec.pid == 4321


def test_touch_updates_last_used(reg_path):
    reg = SubRegistry(reg_path)
    reg.register(_research())
    assert reg.get("research").last_used is None
    reg.touch("research")
    assert reg.get("research").last_used is not None


def test_queries_by_capability_and_mode(reg_path):
    reg = SubRegistry(reg_path)
    reg.register(_research())
    reg.register(SubRecord(id="coder", role="coder", capability=["code.write"], mode="on_demand"))
    assert [r.id for r in reg.by_capability("research.web")] == ["research"]
    assert [r.id for r in reg.always_on()] == ["research"]


def test_unknown_sub_raises(reg_path):
    reg = SubRegistry(reg_path)
    with pytest.raises(KeyError):
        reg.set_status("ghost", STATUS_RUNNING)


def test_atomic_write_is_valid_json(reg_path):
    reg = SubRegistry(reg_path)
    reg.register(_research())
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert data["subs"][0]["id"] == "research"
