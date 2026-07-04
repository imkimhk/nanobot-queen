"""Unit tests for the Queen fleet manager (persist, restore, LRU cap)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.queen.factory import SpawnSpec, SubFactory
from nanobot.queen.fleet import FleetManager
from nanobot.queen.registry import STATUS_RUNNING, STATUS_STOPPED, SubRegistry


@pytest.fixture
def env(tmp_path):
    registry = SubRegistry(tmp_path / "subs.json")
    pids = itertools.count(1000)
    launched: list[Path] = []

    def launcher(*, config_path, workspace, port):
        launched.append(Path(config_path))
        return next(pids)

    factory = SubFactory(
        registry, base_dir=tmp_path,
        keystore_path=tmp_path / ".nbq-core" / "keys.json",
        key_factory=lambda: "K", launcher=launcher, health_check=lambda port: True,
    )
    stops: list[int] = []
    # ports never really listen in tests -> port_check False (forces relaunch path)
    fleet = FleetManager(factory, max_running=3, stopper=lambda pid: stops.append(pid),
                         port_check=lambda port: False)
    return {"reg": registry, "factory": factory, "fleet": fleet, "stops": stops,
            "launched": launched, "root": tmp_path}


def _spec(role, caps=("research.web",)):
    return SpawnSpec(role=role, capability=list(caps), mode="always")


def test_spawn_then_relaunch_preserves_config(env):
    # new sub -> spawned (provisions AGENTS.md)
    env["fleet"].spawn(_spec("research", ["research.web", "research.summary"]))
    ws = env["factory"].workspace_for("research")
    # inject a custom marker into AGENTS.md (as idea-style would)
    agents = ws / "AGENTS.md"
    agents.write_text(agents.read_text() + "\n## 작동 스타일\n발산적 사고\n", encoding="utf-8")
    env["reg"].set_status("research", STATUS_STOPPED)

    res = env["fleet"].spawn(_spec("research", ["research.web", "research.summary"]))
    assert res["action"] == "restarted"
    # relaunch must NOT overwrite the injected style
    assert "발산적 사고" in agents.read_text()


def test_lru_cap_evicts_oldest_on_overflow(env):
    now = datetime.now(timezone.utc)

    def spawn_at(role, minutes_ago):
        env["fleet"].spawn(_spec(role))
        rec = env["reg"].get(role)
        rec.last_used = (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        env["reg"].register(rec)

    spawn_at("research", 30)   # oldest
    spawn_at("coder", 20)
    spawn_at("writer", 10)     # at cap (3 running)
    assert len([r for r in env["reg"].list() if r.status == STATUS_RUNNING]) == 3

    res = env["fleet"].spawn(_spec("analyst", ["data.analyze"]))   # 4th -> evict LRU
    assert res["evicted"] == "research"                # oldest last_used
    assert env["reg"].get("research").status == STATUS_STOPPED
    assert env["reg"].get("analyst").status == STATUS_RUNNING
    assert len([r for r in env["reg"].list() if r.status == STATUS_RUNNING]) == 3


def test_already_running_is_noop(env):
    env["fleet"].spawn(_spec("research"))
    # port_check False -> treated as not up -> would relaunch; force "up" to test no-op
    env["fleet"]._port_in_use = lambda port: True
    res = env["fleet"].spawn(_spec("research"))
    assert res["action"] == "already_running"


def test_restore_all_relaunches_recent_up_to_cap(env):
    now = datetime.now(timezone.utc)
    for i, role in enumerate(["research", "coder", "writer", "analyst"]):
        env["fleet"].spawn(_spec(role, ["research.web"] if role == "research" else ["data.analyze"]))
        rec = env["reg"].get(role)
        rec.last_used = (now - timedelta(minutes=i)).isoformat(timespec="seconds")  # research most recent
        rec.status = STATUS_STOPPED
        env["reg"].register(rec)

    restored = dict(env["fleet"].restore_all())
    running = [r.id for r in env["reg"].list() if r.status == STATUS_RUNNING]
    # cap=3 -> 3 most-recently-used relaunched, oldest (analyst) skipped
    assert len(running) == 3
    assert "analyst" not in running
    assert restored["analyst"] == "skipped(cap)"
