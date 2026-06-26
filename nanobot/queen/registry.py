"""Queen Sub registry — JSON-backed catalogue of Sub instances.

Core keeps a small registry describing every Sub it can route to: identity,
role, capabilities, the port it serves on, its workspace, lifecycle status,
whether it is always-on or on-demand, the prompt version it runs, and when it
was last used. The registry is a plain JSON file (default ``~/.nbq-core/subs.json``)
so it is easy to inspect and survives Core restarts.

This is an additive Core-fork module; it does not modify upstream nanobot.

CLI::

    python -m nanobot.queen.registry list  [--file PATH]
    python -m nanobot.queen.registry register --id research --role "리서치 전문가" \
        --capability research.web,research.summary --port 8901 \
        --workspace ~/.nbq-research --mode always --prompt-version v1 [--status running] [--pid N]
    python -m nanobot.queen.registry set-status --id research --status running [--pid N]
    python -m nanobot.queen.registry touch --id research
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path.home() / ".nbq-core" / "subs.json"

# lifecycle status values
STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_ERROR = "error"

# scheduling modes
MODE_ALWAYS = "always"
MODE_ON_DEMAND = "on_demand"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SubRecord:
    id: str
    role: str
    capability: list[str] = field(default_factory=list)
    port: int = 0
    workspace: str = ""
    status: str = STATUS_STOPPED
    mode: str = MODE_ON_DEMAND
    prompt_version: str = "v1"
    last_used: str | None = None
    pid: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class SubRegistry:
    """JSON-backed Sub registry with atomic writes."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
        self._subs: dict[str, SubRecord] = {}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        self._subs = {}
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in data.get("subs", []):
            rec = SubRecord.from_dict(item)
            self._subs[rec.id] = rec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"subs": [r.to_dict() for r in self._subs.values()]}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        # atomic write: tmp file in same dir + os.replace
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- mutations ----------------------------------------------------------

    def register(self, record: SubRecord) -> SubRecord:
        """Insert or update a Sub by id (idempotent upsert)."""
        self._subs[record.id] = record
        self.save()
        return record

    def set_status(self, sub_id: str, status: str, *, pid: int | None = None) -> SubRecord:
        rec = self._require(sub_id)
        rec.status = status
        if pid is not None:
            rec.pid = pid
        self.save()
        return rec

    def touch(self, sub_id: str) -> SubRecord:
        """Update ``last_used`` to now (call when a Sub handles a request)."""
        rec = self._require(sub_id)
        rec.last_used = _now_iso()
        self.save()
        return rec

    def remove(self, sub_id: str) -> None:
        if sub_id in self._subs:
            del self._subs[sub_id]
            self.save()

    # -- queries ------------------------------------------------------------

    def get(self, sub_id: str) -> SubRecord | None:
        return self._subs.get(sub_id)

    def list(self) -> list[SubRecord]:
        return list(self._subs.values())

    def by_capability(self, capability: str) -> list[SubRecord]:
        return [r for r in self._subs.values() if capability in r.capability]

    def always_on(self) -> list[SubRecord]:
        return [r for r in self._subs.values() if r.mode == MODE_ALWAYS]

    def _require(self, sub_id: str) -> SubRecord:
        rec = self._subs.get(sub_id)
        if rec is None:
            raise KeyError(f"unknown sub_id: {sub_id}")
        return rec


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nanobot.queen.registry")
    p.add_argument("--file", default=str(DEFAULT_REGISTRY_PATH), help="registry JSON path")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    r = sub.add_parser("register")
    r.add_argument("--id", required=True)
    r.add_argument("--role", default="")
    r.add_argument("--capability", default="", help="comma-separated")
    r.add_argument("--port", type=int, default=0)
    r.add_argument("--workspace", default="")
    r.add_argument("--mode", default=MODE_ON_DEMAND, choices=[MODE_ALWAYS, MODE_ON_DEMAND])
    r.add_argument("--prompt-version", default="v1")
    r.add_argument("--status", default=STATUS_STOPPED)
    r.add_argument("--pid", type=int, default=None)

    s = sub.add_parser("set-status")
    s.add_argument("--id", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--pid", type=int, default=None)

    t = sub.add_parser("touch")
    t.add_argument("--id", required=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    reg = SubRegistry(args.file)

    if args.cmd == "list":
        for r in reg.list():
            print(json.dumps(r.to_dict(), ensure_ascii=False))
        return 0

    if args.cmd == "register":
        caps = [c.strip() for c in args.capability.split(",") if c.strip()]
        rec = SubRecord(
            id=args.id, role=args.role, capability=caps, port=args.port,
            workspace=os.path.expanduser(args.workspace), status=args.status,
            mode=args.mode, prompt_version=args.prompt_version, pid=args.pid,
        )
        reg.register(rec)
        print(f"registered {rec.id} status={rec.status} mode={rec.mode} port={rec.port}")
        return 0

    if args.cmd == "set-status":
        rec = reg.set_status(args.id, args.status, pid=args.pid)
        print(f"{rec.id} -> status={rec.status} pid={rec.pid}")
        return 0

    if args.cmd == "touch":
        rec = reg.touch(args.id)
        print(f"{rec.id} last_used={rec.last_used}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
