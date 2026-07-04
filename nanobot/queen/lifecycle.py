"""Queen on-demand lifecycle — spawn Subs when needed, reap them when idle.

Always-on Subs (``mode=always``) stay up. On-demand Subs (``mode=on_demand``)
are created lazily by Core when a request needs them (reusing the STEP 6
factory) and stopped when idle. Stopping preserves the workspace and
``sessions/`` so that a later re-creation **continues the same memory**
(PoC-C): same workspace + same port + same pre-shared key.

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from nanobot.queen.factory import SpawnSpec, SubFactory
from nanobot.queen.registry import (
    MODE_ON_DEMAND,
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STOPPED,
    SubRecord,
)


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


@dataclass
class EnsureResult:
    action: str  # "already_running" | "restarted" | "spawned"
    sub_id: str
    port: int
    pid: int | None
    healthy: bool
    status: str


class OnDemandManager:
    def __init__(self, factory: SubFactory, *, stopper=None, clock=None):
        self.factory = factory
        self.registry = factory.registry
        self.stopper = stopper or self._default_stopper
        self.clock = clock or (lambda: time.time())

    # -- ensure a Sub is up (spawn or restart-in-place) ---------------------

    def ensure(self, spec: SpawnSpec) -> EnsureResult:
        rec = self.registry.get(spec.role)
        if rec is not None and rec.status == STATUS_RUNNING:
            self.registry.touch(spec.role)
            return EnsureResult("already_running", rec.id, rec.port, rec.pid, True, rec.status)
        if rec is not None:
            # restart in place: same workspace + port + key => memory continues
            return self._restart(rec, spec)
        # first-time creation
        res = self.factory.spawn(spec)
        return EnsureResult("spawned", res.sub_id, res.port, res.pid, res.healthy, res.record.status)

    def _restart(self, rec: SubRecord, spec: SpawnSpec) -> EnsureResult:
        ws = Path(rec.workspace)
        key = json.loads((ws / "config.json").read_text(encoding="utf-8"))[
            "providers"]["custom"]["apiKey"]
        # keep the existing port; reuse the spec but pin the registered port
        spec = SpawnSpec(role=spec.role, capability=list(rec.capability), skills=list(spec.skills),
                         mode=rec.mode, port=rec.port, prompt_version=rec.prompt_version)
        self.factory.provision(spec, sub_id=rec.id, key=key, port=rec.port, workspace=ws)
        pid = self.factory.launcher(config_path=ws / "config.json", workspace=ws, port=rec.port)
        healthy = self.factory.health_check(rec.port)
        rec.pid = pid
        rec.status = STATUS_RUNNING if healthy else STATUS_ERROR
        self.registry.register(rec)
        self.registry.touch(rec.id)
        return EnsureResult("restarted", rec.id, rec.port, pid, healthy, rec.status)

    # -- reap idle on-demand Subs (preserve workspace/sessions) -------------

    def reap_idle(self, idle_seconds: float, *, now: float | None = None) -> list[str]:
        now = now if now is not None else self.clock()
        stopped: list[str] = []
        for rec in self.registry.list():
            if rec.mode != MODE_ON_DEMAND or rec.status != STATUS_RUNNING:
                continue
            last = _parse_iso(rec.last_used)
            # never-used running Subs are considered idle from registration time is
            # unknown; require an explicit last_used to reap (conservative).
            if last is None or (now - last) < idle_seconds:
                continue
            if rec.pid:
                self.stopper(rec.pid)
            self.registry.set_status(rec.id, STATUS_STOPPED)  # workspace/sessions kept
            stopped.append(rec.id)
        return stopped

    @staticmethod
    def _default_stopper(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
