"""Queen fleet management — persistent, auto-restored, LRU-capped Sub fleet.

Goals (so the user never hand-launches Subs):
  * **persist** — created Subs stay in the registry across restarts;
  * **auto-restore** — boot relaunches them WITHOUT re-provisioning, so an
    injected working style (idea's AGENTS.md) and memory (sessions/) survive;
  * **LRU cap** — at most ``max_running`` Subs run at once; spawning one more
    stops the least-recently-used Sub first (its workspace/memory are kept).

Relaunch ≠ spawn: spawn (factory) provisions a NEW workspace (default template);
relaunch only starts ``nanobot serve`` on an EXISTING workspace, preserving its
config.json / AGENTS.md / sessions and re-registering its key with the gateway.

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from pathlib import Path

from nanobot.queen.factory import SpawnSpec, SubFactory
from nanobot.queen.registry import (
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STOPPED,
    SubRecord,
    SubRegistry,
)

DEFAULT_MAX_RUNNING = 5


def _last_used_ts(rec: SubRecord) -> float:
    if not rec.last_used:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(rec.last_used).timestamp()
    except ValueError:
        return 0.0


class FleetManager:
    def __init__(self, factory: SubFactory, *, max_running: int = DEFAULT_MAX_RUNNING,
                 stopper=None, port_check=None, clock=None):
        self.factory = factory
        self.registry: SubRegistry = factory.registry
        self.max_running = max_running
        self.stopper = stopper or self._default_stopper
        self._port_in_use = port_check or self._default_port_check
        self.clock = clock or (lambda: time.time())

    # -- helpers ------------------------------------------------------------

    def _running(self) -> list[SubRecord]:
        return [r for r in self.registry.list() if r.status == STATUS_RUNNING]

    def _read_key(self, ws: Path) -> str | None:
        try:
            return json.loads((ws / "config.json").read_text(encoding="utf-8"))[
                "providers"]["custom"]["apiKey"]
        except (OSError, KeyError, ValueError):
            return None

    # -- relaunch an EXISTING sub (preserve workspace/AGENTS.md/memory) -----

    def relaunch(self, rec: SubRecord) -> bool:
        ws = Path(rec.workspace)
        if not (ws / "config.json").exists():
            return False
        key = self._read_key(ws)
        if key:
            self.factory.keystore.add(key, rec.id)  # gateway must recognise it again
        pid = self.factory.launcher(config_path=ws / "config.json", workspace=ws, port=rec.port)
        healthy = self.factory.health_check(rec.port)
        rec.pid = pid
        rec.status = STATUS_RUNNING if healthy else STATUS_ERROR
        self.registry.register(rec)
        self.registry.touch(rec.id)
        return healthy

    # -- LRU eviction -------------------------------------------------------

    def evict_lru(self, *, exclude: str | None = None) -> str | None:
        candidates = [r for r in self._running() if r.id != exclude]
        if not candidates:
            return None
        victim = min(candidates, key=_last_used_ts)  # oldest last_used (None=oldest)
        if victim.pid:
            self.stopper(victim.pid)
        self.registry.set_status(victim.id, STATUS_STOPPED)  # workspace/memory kept
        return victim.id

    # -- capped spawn (the default path for /spawn) -------------------------

    def spawn(self, spec: SpawnSpec) -> dict:
        """Ensure a Sub is up, honouring the LRU cap.

        Existing Sub -> relaunch (preserve injected config/memory).
        New Sub -> factory.spawn (provision). Evicts the LRU Sub if at capacity.
        """
        rec = self.registry.get(spec.role)
        if rec is not None and rec.status == STATUS_RUNNING and self._port_in_use(rec.port):
            self.registry.touch(spec.role)
            return {"action": "already_running", "sub_id": spec.role,
                    "port": rec.port, "healthy": True, "evicted": None}

        evicted = None
        if len([r for r in self._running() if r.id != spec.role]) >= self.max_running:
            evicted = self.evict_lru(exclude=spec.role)

        if rec is not None:
            healthy = self.relaunch(rec)
            return {"action": "restarted", "sub_id": spec.role, "port": rec.port,
                    "healthy": healthy, "evicted": evicted}

        res = self.factory.spawn(spec)
        self.registry.touch(res.sub_id)
        return {"action": "spawned", "sub_id": res.sub_id, "port": res.port,
                "healthy": res.healthy, "evicted": evicted}

    # -- boot: restore the most-recently-used subs (up to the cap) ----------

    def restore_all(self) -> list[tuple[str, str]]:
        subs = sorted(self.registry.list(), key=_last_used_ts, reverse=True)
        out: list[tuple[str, str]] = []
        for rec in subs[: self.max_running]:
            if self._port_in_use(rec.port):
                # already running: still ensure its key is in the gateway keystore
                # (the gateway may have restarted), else its model calls get 401.
                key = self._read_key(Path(rec.workspace))
                if key:
                    self.factory.keystore.add(key, rec.id)
                if rec.status != STATUS_RUNNING:
                    self.registry.set_status(rec.id, STATUS_RUNNING)
                out.append((rec.id, "already_up"))
                continue
            ok = self.relaunch(rec)
            out.append((rec.id, "relaunched" if ok else "error"))
        for rec in subs[self.max_running:]:
            if rec.status == STATUS_RUNNING and not self._port_in_use(rec.port):
                self.registry.set_status(rec.id, STATUS_STOPPED)
            out.append((rec.id, "skipped(cap)"))
        return out

    # -- defaults -----------------------------------------------------------

    @staticmethod
    def _default_port_check(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _default_stopper(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
