"""Unit tests for the Queen on-demand lifecycle manager."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.queen.factory import SpawnSpec, SubFactory
from nanobot.queen.lifecycle import OnDemandManager
from nanobot.queen.registry import (
    MODE_ALWAYS,
    STATUS_RUNNING,
    STATUS_STOPPED,
    SubRegistry,
)


@pytest.fixture
def env(tmp_path):
    registry = SubRegistry(tmp_path / "subs.json")
    pids = itertools.count(1000)
    factory = SubFactory(
        registry, base_dir=tmp_path,
        keystore_path=tmp_path / ".nbq-core" / "keys.json",
        key_factory=lambda: "K", launcher=lambda **k: next(pids),
        health_check=lambda port: True,
    )
    stops: list[int] = []
    mgr = OnDemandManager(factory, stopper=lambda pid: stops.append(pid))
    return {"registry": registry, "factory": factory, "mgr": mgr, "stops": stops, "root": tmp_path}


def _spec(role="coder", caps=("code.write",), mode="on_demand"):
    return SpawnSpec(role=role, capability=list(caps), mode=mode)


# --- ensure ----------------------------------------------------------------


def test_ensure_first_time_spawns(env):
    res = env["mgr"].ensure(_spec())
    assert res.action == "spawned"
    assert res.status == STATUS_RUNNING
    assert env["registry"].get("coder").status == STATUS_RUNNING


def test_ensure_running_is_noop_but_touches(env):
    env["mgr"].ensure(_spec())
    assert env["registry"].get("coder").last_used is None
    res = env["mgr"].ensure(_spec())
    assert res.action == "already_running"
    assert env["registry"].get("coder").last_used is not None


def test_ensure_stopped_restarts_in_place_preserving_memory(env):
    res1 = env["mgr"].ensure(_spec())
    ws = Path(env["registry"].get("coder").workspace)
    port_before = res1.port
    # accumulate memory + stop
    mem = ws / "sessions" / "api_default.jsonl"
    mem.write_text('{"role":"user","content":"keep ME"}\n', encoding="utf-8")
    env["registry"].set_status("coder", STATUS_STOPPED)

    res2 = env["mgr"].ensure(_spec())
    assert res2.action == "restarted"
    assert res2.port == port_before          # same port
    assert res2.status == STATUS_RUNNING
    # PoC-C: sessions preserved across stop -> restart
    assert mem.exists() and "keep ME" in mem.read_text()


# --- reap idle -------------------------------------------------------------


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def test_reap_idle_stops_old_on_demand(env):
    env["mgr"].ensure(_spec())
    rec = env["registry"].get("coder")
    old = datetime.now(timezone.utc) - timedelta(seconds=600)
    rec.last_used = _iso(old)
    rec.status = STATUS_RUNNING
    env["registry"].register(rec)

    stopped = env["mgr"].reap_idle(idle_seconds=300)
    assert stopped == ["coder"]
    assert env["registry"].get("coder").status == STATUS_STOPPED
    assert env["stops"]  # stopper was invoked
    # workspace + sessions preserved
    assert Path(env["registry"].get("coder").workspace).is_dir()


def test_reap_keeps_recently_used(env):
    env["mgr"].ensure(_spec())
    rec = env["registry"].get("coder")
    rec.last_used = _iso(datetime.now(timezone.utc))
    rec.status = STATUS_RUNNING
    env["registry"].register(rec)
    assert env["mgr"].reap_idle(idle_seconds=300) == []


def test_reap_never_touches_always_on(env):
    env["mgr"].ensure(_spec(role="research", caps=("research.web",), mode=MODE_ALWAYS))
    rec = env["registry"].get("research")
    rec.last_used = _iso(datetime.now(timezone.utc) - timedelta(seconds=9999))
    rec.status = STATUS_RUNNING
    env["registry"].register(rec)
    assert env["mgr"].reap_idle(idle_seconds=300) == []
    assert env["registry"].get("research").status == STATUS_RUNNING
