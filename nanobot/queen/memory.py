"""Queen unified memory — promote important Sub results to Core-level storage.

A Sub's working memory lives in its own ``<workspace>/sessions/`` (PoC-C) and is
therefore lost when the Sub's workspace is isolated (STEP 7 ``isolate=True``) or
intentionally reset. **Promotion** copies the *important* distillations of a
Sub's work up into Core's unified memory under ``~/.nbq-core/memory/`` so they
survive Sub re-creation and isolation.

What counts as "important" (``ImportancePolicy``):

  * ``task_result`` with status ok/success  -> medium (a task outcome summary);
  * ``task_result`` with status failed/error -> high (a failure to remember);
  * ``pattern``  (a recurring success/failure pattern) -> high;
  * ``decision`` / ``fact``                  -> medium (durable knowledge);
  * anything else, or a too-short summary     -> not promoted (unless forced).

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

IMPORTANCE_HIGH = "high"
IMPORTANCE_MEDIUM = "medium"
IMPORTANCE_LOW = "low"

_PROMOTABLE_KINDS = {"task_result", "pattern", "decision", "fact"}
_MIN_SUMMARY_LEN = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PromotedMemory:
    id: str
    sub_id: str
    kind: str
    importance: str
    reason: str
    summary: str
    task_id: str | None = None
    content: str | None = None
    tags: list[str] = field(default_factory=list)
    created: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ImportancePolicy:
    """Decides whether a Sub memory item is worth promoting to Core."""

    def classify(self, kind: str, status: str, summary: str) -> tuple[bool, str, str]:
        """Return ``(important, importance, reason)``."""
        if not summary or len(summary.strip()) < _MIN_SUMMARY_LEN:
            return False, IMPORTANCE_LOW, "summary too short / trivial"
        if kind == "task_result":
            if status in {"failed", "error"}:
                return True, IMPORTANCE_HIGH, "failure to remember (task failed)"
            if status in {"ok", "success", "done"}:
                return True, IMPORTANCE_MEDIUM, "task result summary"
            return False, IMPORTANCE_LOW, f"task_result with unscored status {status!r}"
        if kind == "pattern":
            return True, IMPORTANCE_HIGH, "recurring success/failure pattern"
        if kind in {"decision", "fact"}:
            return True, IMPORTANCE_MEDIUM, f"durable {kind}"
        return False, IMPORTANCE_LOW, f"kind {kind!r} not promotable"


class CoreMemory:
    """Append-only unified memory store at ``<base>/.nbq-core/memory/promoted.jsonl``."""

    def __init__(self, base_dir: str | Path | None = None, *, policy: ImportancePolicy | None = None):
        base = Path(base_dir) if base_dir is not None else Path.home()
        self.path = base / ".nbq-core" / "memory" / "promoted.jsonl"
        self.policy = policy or ImportancePolicy()

    # -- write --------------------------------------------------------------

    def promote(
        self,
        sub_id: str,
        summary: str,
        *,
        kind: str = "task_result",
        status: str = "ok",
        task_id: str | None = None,
        content: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        force: bool = False,
    ) -> PromotedMemory | None:
        """Promote a Sub memory item if important (or ``force=True``).

        Returns the stored record, or ``None`` if it was judged unimportant.
        """
        important, importance, reason = self.policy.classify(kind, status, summary)
        if not important and not force:
            return None
        if force and not important:
            reason = f"forced ({reason})"
            importance = importance if importance != IMPORTANCE_LOW else IMPORTANCE_MEDIUM

        rec = PromotedMemory(
            id=f"mem_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}",
            sub_id=sub_id, kind=kind, importance=importance, reason=reason,
            summary=summary.strip(), task_id=task_id, content=content,
            tags=list(tags), created=_now_iso(),
        )
        self._append(rec)
        return rec

    def _append(self, rec: PromotedMemory) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec.to_dict(), ensure_ascii=False) + "\n"
        # durable append: write + fsync
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    # -- read ---------------------------------------------------------------

    def query(self, *, sub_id: str | None = None, kind: str | None = None,
              importance: str | None = None) -> list[PromotedMemory]:
        if not self.path.exists():
            return []
        out: list[PromotedMemory] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = PromotedMemory(**{k: v for k, v in d.items()
                                    if k in PromotedMemory.__dataclass_fields__})
            if sub_id is not None and rec.sub_id != sub_id:
                continue
            if kind is not None and rec.kind != kind:
                continue
            if importance is not None and rec.importance != importance:
                continue
            out.append(rec)
        return out
