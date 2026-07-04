"""Queen role adjuster — change an existing Sub's role/capability/scope safely.

STEP 7 (4.4). Two paths:

  * **home-kept** (default): stop the Sub (workspace preserved) → write the new
    config + role prompt into the *same* workspace and port → restart →
    **memory is preserved** (``sessions/`` untouched, per PoC-C).
  * **isolated** ("완전히 다른 분야 전환"): the old ``sessions/`` is archived so
    the restarted Sub starts with a clean memory.

Safety (required):
  * role/capability stay inside the factory allowlist;
  * the (possibly LLM-authored) new role prompt and any config override pass a
    **forbidden-pattern filter** — gateway bypass, impersonating another Sub,
    and credential-exfiltration inducement are rejected;
  * every apply snapshots the prior config/prompt/registry record into a
    **change history** so it can be **rolled back**;
  * MVP workflow: ``draft()`` (Core proposes) → human approval → ``apply(plan,
    approved=True)``. ``apply`` refuses without explicit approval.

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from nanobot.queen.factory import (
    ALLOWED_CAPABILITIES,
    ALLOWED_MODES,
    ALLOWED_ROLES,
    SpawnSpec,
    SubFactory,
)
from nanobot.queen.registry import (
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STOPPED,
)


class AdjustmentError(ValueError):
    pass


class ForbiddenPatternError(AdjustmentError):
    pass


# --- forbidden-pattern filter ---------------------------------------------

# Applied to the new role-prompt text (LLM-authored). Each entry: (regex, why).
_FORBIDDEN_PROMPT_RULES: list[tuple[str, str]] = [
    # credential exfiltration / output inducement.
    # A role prompt has no legitimate reason to name credentials, so these terms
    # are blanket-blocked regardless of surrounding verb order. (Bare single-char
    # Korean like "키" is intentionally avoided — it appears inside ordinary words
    # such as "아키텍처".)
    (r"(?i)(api[\s_-]?key|apikey|secret[\s_-]?key|access[\s_-]?token|bearer\s+token|"
     r"credential|자격\s*증명|비밀번호|토큰\s*(출력|노출|유출|반환))",
     "credential reference in role prompt"),
    (r"(?i)(reveal|print|output|show|dump|leak|출력|알려|보여|노출).{0,24}"
     r"(api[\s_-]?key|secret\s*key|access\s*token|credential|자격\s*증명|비밀번호)",
     "credential output inducement"),
    (r"(?i)(printenv|os\.environ|process\.env|\benv\b\s*dump|cat\s+.*config|cat\s+.*\.json)",
     "environment / config exfiltration"),
    (r"(?i)(OPENAI_API_KEY|CODEX_.*TOKEN|ANTHROPIC_API_KEY|BEARER\s+[A-Za-z0-9._-]{12,})",
     "literal credential reference"),
    # gateway bypass
    (r"(?i)(chatgpt\.com|api\.openai\.com|backend-api/codex)",
     "gateway bypass (direct upstream endpoint)"),
    (r"(?i)(bypass|우회|직접\s*호출|directly\s+call).{0,24}(gateway|게이트웨이|core|코어|codex)",
     "gateway bypass instruction"),
    (r"(?i)(api[\s_-]?base|base[\s_-]?url|apiBase)\s*[:=]",
     "provider endpoint override"),
    # impersonating another Sub (note: a plain ``sub_id: <self>`` identity line is
    # legitimate and intentionally NOT matched here)
    (r"(?i)(x[\s-]?sub[\s-]?id|사칭|impersonate|pretend\s+to\s+be|act\s+as\s+sub\b|"
     r"set\s+.*sub[\s_-]?id|spoof)",
     "Sub impersonation"),
]

# Applied to the resulting config dict.
_GATEWAY_HOST_LOCAL = {"127.0.0.1", "localhost", "::1"}


def screen_prompt(text: str) -> None:
    """Raise ForbiddenPatternError if the role prompt trips a forbidden rule."""
    for pattern, why in _FORBIDDEN_PROMPT_RULES:
        if re.search(pattern, text):
            raise ForbiddenPatternError(f"forbidden pattern in role prompt: {why}")


def screen_config(config: dict, *, gateway_url: str) -> None:
    """Raise if the config would bypass the gateway or impersonate a Sub."""
    providers = config.get("providers", {})
    if set(providers) - {"custom"}:
        raise ForbiddenPatternError(
            f"only the 'custom' provider is allowed, got {sorted(providers)}")
    custom = providers.get("custom", {})
    if custom.get("apiBase") != gateway_url:
        raise ForbiddenPatternError(
            f"provider apiBase must be the gateway {gateway_url!r}")
    for forbidden in ("extra_headers", "extraHeaders", "extra_body", "extraBody"):
        if forbidden in custom:
            raise ForbiddenPatternError(
                f"custom provider may not set {forbidden!r} (impersonation risk)")
    api = config.get("api", {})
    if api.get("host") not in _GATEWAY_HOST_LOCAL:
        raise ForbiddenPatternError("api.host must bind locally (127.0.0.1)")


# --- draft / plan ----------------------------------------------------------


@dataclass
class AdjustmentDraft:
    """Requested change (Core proposes this; a human approves it)."""

    sub_id: str
    capability: list[str]
    role_label: str | None = None        # defaults to current role
    skills: list[str] = field(default_factory=list)
    prompt_version: str = "v2"
    role_prompt_text: str | None = None  # None => render safe default template
    isolate: bool = False                # True => archive sessions/ (memory reset)
    # Items to promote to Core unified memory BEFORE isolation wipes sub memory.
    # Each item: {"summary": str, "kind": str, "status": str, "task_id": str|None}.
    promote: list[dict] = field(default_factory=list)


@dataclass
class AdjustmentPlan:
    sub_id: str
    workspace: str
    port: int
    spec: SpawnSpec
    agents_md: str
    soul_md: str
    isolate: bool
    prior_capability: list[str]
    prior_prompt_version: str
    promote: list[dict] = field(default_factory=list)


# --- adjuster --------------------------------------------------------------


class RoleAdjuster:
    def __init__(
        self,
        factory: SubFactory,
        *,
        history_dir: str | Path | None = None,
        stopper=None,
        clock=None,
        core_memory=None,
    ):
        self.factory = factory
        self.registry = factory.registry
        self.core_memory = core_memory
        self.history_dir = Path(history_dir) if history_dir is not None else (
            factory.base_dir / ".nbq-core" / "history"
        )
        self.stopper = stopper or self._default_stopper
        self.clock = clock or (lambda: time.strftime("%Y%m%d_%H%M%S"))

    # -- draft (Core proposes; validated + screened, nothing applied) -------

    def draft(self, draft: AdjustmentDraft) -> AdjustmentPlan:
        rec = self.registry.get(draft.sub_id)
        if rec is None:
            raise AdjustmentError(f"unknown sub_id: {draft.sub_id}")

        role_label = draft.role_label or rec.role
        # allowlist
        if role_label not in ALLOWED_ROLES:
            raise AdjustmentError(f"role {role_label!r} not in allowlist")
        if not draft.capability:
            raise AdjustmentError("at least one capability is required")
        bad = [c for c in draft.capability if c not in ALLOWED_CAPABILITIES]
        if bad:
            raise AdjustmentError(f"capabilities not in allowlist: {bad}")

        spec = SpawnSpec(
            role=role_label, capability=list(draft.capability), skills=list(draft.skills),
            mode=rec.mode if rec.mode in ALLOWED_MODES else "on_demand",
            port=rec.port, prompt_version=draft.prompt_version,
        )
        agents_md = (
            draft.role_prompt_text
            if draft.role_prompt_text is not None
            else self.factory._render_agents_md(spec, draft.sub_id)
        )
        soul_md = self.factory._render_soul_md(spec)

        # forbidden-pattern filter on the (possibly LLM-authored) prompt
        screen_prompt(agents_md)
        screen_prompt(soul_md)

        return AdjustmentPlan(
            sub_id=draft.sub_id, workspace=rec.workspace, port=rec.port, spec=spec,
            agents_md=agents_md, soul_md=soul_md, isolate=draft.isolate,
            prior_capability=list(rec.capability), prior_prompt_version=rec.prompt_version,
            promote=list(draft.promote),
        )

    # -- apply (requires explicit human approval) ---------------------------

    def apply(self, plan: AdjustmentPlan, *, approved: bool = False) -> dict:
        if not approved:
            raise AdjustmentError("apply requires approved=True (human approval, MVP)")
        rec = self.registry.get(plan.sub_id)
        if rec is None:
            raise AdjustmentError(f"unknown sub_id: {plan.sub_id}")
        ws = Path(plan.workspace)

        # config screen (defence in depth — config is Core-generated but verify)
        key = self._read_key(ws)
        config_preview = {
            "providers": {"custom": {"apiBase": self.factory.gateway_url, "apiKey": key}},
            "api": {"host": "127.0.0.1", "port": plan.port, "timeout": 120},
        }
        screen_config(config_preview, gateway_url=self.factory.gateway_url)

        # 1) snapshot current state for rollback
        snap = self._snapshot(plan.sub_id, ws, rec)

        # 2) stop the running Sub (workspace preserved)
        if rec.pid:
            self.stopper(rec.pid)
        self.registry.set_status(plan.sub_id, STATUS_STOPPED)

        # 3) promote important memories to Core BEFORE isolation wipes sub memory
        promoted = self._promote_before_isolate(plan)

        # 4) optional memory isolation
        if plan.isolate:
            self._archive_sessions(ws)

        # 5) reprovision in place (same workspace + port; sessions preserved)
        self.factory.provision(
            plan.spec, sub_id=plan.sub_id, key=key, port=plan.port,
            workspace=ws, agents_md=plan.agents_md, soul_md=plan.soul_md,
        )

        # 6) restart + health + registry update
        result = self._relaunch_and_update(plan.sub_id, plan.spec, ws, plan.port, snapshot=snap)
        result["promoted"] = promoted
        return result

    def _promote_before_isolate(self, plan: AdjustmentPlan) -> int:
        """Promote requested items to Core memory; returns count actually stored."""
        if not plan.promote or self.core_memory is None:
            return 0
        n = 0
        for item in plan.promote:
            rec = self.core_memory.promote(
                plan.sub_id, item.get("summary", ""),
                kind=item.get("kind", "task_result"), status=item.get("status", "ok"),
                task_id=item.get("task_id"), tags=tuple(item.get("tags", ())),
                force=bool(item.get("force", False)),
            )
            if rec is not None:
                n += 1
        return n

    # -- rollback -----------------------------------------------------------

    def rollback(self, sub_id: str) -> dict:
        snaps = self._list_snapshots(sub_id)
        if not snaps:
            raise AdjustmentError(f"no history to roll back for {sub_id!r}")
        latest = snaps[-1]
        rec = self.registry.get(sub_id)
        if rec is None:
            raise AdjustmentError(f"unknown sub_id: {sub_id}")
        ws = Path(rec.workspace)

        # stop current
        if rec.pid:
            self.stopper(rec.pid)
        self.registry.set_status(sub_id, STATUS_STOPPED)

        # restore files
        for name in ("config.json", "AGENTS.md", "SOUL.md"):
            src = latest / name
            if src.exists():
                shutil.copy2(src, ws / name)
        prior = json.loads((latest / "record.json").read_text(encoding="utf-8"))

        spec = SpawnSpec(
            role=prior["role"], capability=list(prior["capability"]),
            mode=prior.get("mode", "on_demand"), port=prior["port"],
            prompt_version=prior.get("prompt_version", "v1"),
        )
        result = self._relaunch_and_update(sub_id, spec, ws, prior["port"], snapshot=None)
        result["rolled_back_to"] = latest.name
        return result

    # -- helpers ------------------------------------------------------------

    def _relaunch_and_update(self, sub_id, spec, ws: Path, port: int, *, snapshot) -> dict:
        pid = self.factory.launcher(config_path=ws / "config.json", workspace=ws, port=port)
        healthy = self.factory.health_check(port)
        rec = self.registry.get(sub_id)
        rec.role = spec.role
        rec.capability = list(spec.capability)
        rec.prompt_version = spec.prompt_version
        rec.port = port
        rec.pid = pid
        rec.status = STATUS_RUNNING if healthy else STATUS_ERROR
        self.registry.register(rec)
        return {"sub_id": sub_id, "healthy": healthy, "pid": pid,
                "status": rec.status, "capability": rec.capability,
                "snapshot": (snapshot.name if snapshot else None)}

    def _read_key(self, ws: Path) -> str:
        cfg = json.loads((ws / "config.json").read_text(encoding="utf-8"))
        return cfg["providers"]["custom"]["apiKey"]

    def _snapshot(self, sub_id: str, ws: Path, rec) -> Path:
        dest = self.history_dir / sub_id / self.clock()
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "AGENTS.md", "SOUL.md"):
            src = ws / name
            if src.exists():
                shutil.copy2(src, dest / name)
        (dest / "record.json").write_text(
            json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return dest

    def _list_snapshots(self, sub_id: str) -> list[Path]:
        base = self.history_dir / sub_id
        if not base.is_dir():
            return []
        return sorted((d for d in base.iterdir() if d.is_dir()), key=lambda p: p.name)

    def _archive_sessions(self, ws: Path) -> None:
        sessions = ws / "sessions"
        if sessions.is_dir() and any(sessions.iterdir()):
            archived = ws / f"sessions.archived-{self.clock()}"
            shutil.move(str(sessions), str(archived))
        (ws / "sessions").mkdir(exist_ok=True)

    @staticmethod
    def _default_stopper(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
