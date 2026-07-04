"""Queen Sub factory — Core spawns new Sub instances from a spec.

Given ``(role, capability[], skills[], mode)`` the factory:

  1. validates role/capability against an **allowlist** (safety);
  2. clones the base nanobot workspace into ``~/.nbq-<role>`` and injects the
     role prompt (AGENTS.md / SOUL.md), provider config pointing at the Model
     Gateway (127.0.0.1:8900/v1), a **unique pre-shared key**, capability and a
     **unique port**;
  3. records the key in the gateway keystore (``~/.nbq-core/keys.json``) so the
     running gateway recognises the new Sub and attributes usage by ``sub_id``;
  4. launches ``nanobot serve`` for the new workspace;
  5. registers the Sub in the registry and health-checks it.

This automates what STEP 5 did by hand for the Research Sub. It is an additive
Core-fork module — no upstream file is modified.

PoC-C facts honoured:
  * memory lives in ``<workspace>/sessions/<session_id>.jsonl`` — provisioning
    creates the ``sessions/`` directory and never touches it on re-provision;
  * each Sub gets a unique key + unique port for per-``sub_id`` attribution.

STEP 7 readiness: :meth:`provision` rewrites config + role prompts **in place**
without disturbing ``sessions/`` (memory preserved), which is exactly the
"keep the workspace, change the config, restart" path role-adjustment needs.

Security: the unique key is never logged or printed by the factory; it lives
only in the Sub's home-dir config and the home-dir keystore (both outside the
git repo).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from nanobot.queen.registry import (
    MODE_ALWAYS,
    MODE_ON_DEMAND,
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STARTING,
    SubRecord,
    SubRegistry,
)

# --- allowlists (safety) ---------------------------------------------------

ALLOWED_ROLES: set[str] = {"research", "coder", "writer", "analyst", "planner", "idea"}
ALLOWED_CAPABILITIES: set[str] = {
    "research.web", "research.summary",
    "code.write", "code.review",
    "writing.draft", "writing.edit",
    "data.analyze", "data.viz",
    "planning.decompose",
    # "idea" — a blank thinking Sub whose behaviour is shaped later (STEP 2,
    # via the adjuster). Its boundary stays inside the *idea* domain: no code
    # execution, no file writes, no external calls — pure ideation.
    "idea.generate", "idea.structure", "idea.evaluate",
}
ALLOWED_MODES: set[str] = {MODE_ALWAYS, MODE_ON_DEMAND}

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8900/v1"
DEFAULT_MODEL = "openai-codex/gpt-5.5"
DEFAULT_PORT_BASE = 8902
RESERVED_PORTS = {8900, 8901}  # gateway + research


class SpawnError(ValueError):
    """Raised when a spawn spec violates the allowlist or a Sub already runs."""


# Minimal per-capability toolsets. Restricting a Sub's registered tools shrinks
# the tool-schema portion of every request's prompt (~5k tokens with the full
# 18-tool default) — the main lever once upstream prompt caching is unavailable
# (STEP 9: ChatGPT-subscription Codex does not discount cached tokens).
CAPABILITY_TOOLSETS: dict[str, list[str]] = {
    "research.web": ["web_search", "web_fetch"],
    "research.summary": ["read_file"],
    "code.write": ["read_file", "write_file", "edit_file", "apply_patch", "exec"],
    "code.review": ["read_file", "grep", "find_files"],
    "writing.draft": ["read_file", "write_file"],
    "writing.edit": ["read_file", "edit_file"],
    "data.analyze": ["read_file", "exec", "grep"],
    "data.viz": ["read_file", "write_file", "exec"],
    "planning.decompose": ["read_file"],
    # idea capabilities get NO tools beyond `message` — no file/exec/web — so a
    # blank idea Sub literally cannot do code execution or external work; it can
    # only think and reply in text. This enforces the "idea domain only" boundary.
    "idea.generate": [],
    "idea.structure": [],
    "idea.evaluate": [],
}
# Tools every Sub keeps regardless of capability.
BASE_TOOLS: list[str] = ["message"]

# Default capabilities used when /spawn is given a role without explicit caps.
ROLE_DEFAULT_CAPABILITIES: dict[str, list[str]] = {
    "research": ["research.web", "research.summary"],
    "coder": ["code.write", "code.review"],
    "writer": ["writing.draft", "writing.edit"],
    "analyst": ["data.analyze", "data.viz"],
    "planner": ["planning.decompose"],
    "idea": ["idea.generate", "idea.structure", "idea.evaluate"],
}

# Per-role default profile: a short description and an extra boundary line woven
# into the rendered role prompt. Roles not listed use the generic template.
ROLE_PROFILES: dict[str, dict[str, str]] = {
    "idea": {
        "summary": "아이디어 도출·구조화·평가 전문가",
        "extra_boundary": (
            "너는 **순수하게 아이디어 영역에서만** 일한다. 아이디어를 내고(도출), "
            "정리하고(구조화), 따져본다(평가).\n"
            "**중요: 너의 'generate(도출)'는 아이디어·개념·방향을 만드는 것이지, "
            "산출물을 제작하는 게 아니다.** 다음은 (텍스트로라도) **절대 하지 마라** — "
            "코드·함수·프로그램·스크립트 작성, 파일 작성, 문서/보고서 완성본 작성, 웹 조사, "
            "수식·데이터 계산 대행, 시스템 명령. 이런 요청을 받으면 **코드나 산출물을 한 줄도 "
            "출력하지 말고**, 정확히 `OUT_OF_SCOPE: ...` 형식으로만 Core에 돌려보낸다.\n"
            "허용: '~할 아이디어/접근/방향 알려줘', '이 아이디어 장단점 평가', "
            "'아이디어들 구조화/분류'. 금지: '~를 만들어줘/작성해줘/구현해줘'(산출물 제작).\n"
            "(이 Sub의 구체적 작동 방식은 나중에 자연어/문서로 주입될 수 있으나, 이 "
            "아이디어 영역 경계는 그래도 유지된다.)"
        ),
    },
}


def toolset_for(capabilities: list[str]) -> list[str]:
    """Union of the minimal toolsets for the given capabilities, plus base tools."""
    tools: list[str] = list(BASE_TOOLS)
    for cap in capabilities:
        for t in CAPABILITY_TOOLSETS.get(cap, []):
            if t not in tools:
                tools.append(t)
    return tools


@dataclass
class SpawnSpec:
    role: str
    capability: list[str]
    skills: list[str] = field(default_factory=list)
    mode: str = MODE_ON_DEMAND
    port: int | None = None
    prompt_version: str = "v1"
    # Explicit enabled-tools override. None => derive a minimal set from
    # capabilities; ["*"] => keep all tools (the upstream nanobot default).
    tools: list[str] | None = None


@dataclass
class SpawnResult:
    sub_id: str
    workspace: str
    port: int
    pid: int | None
    healthy: bool
    record: SubRecord


# --- keystore --------------------------------------------------------------


class KeyStore:
    """JSON ``{psk: sub_id}`` keystore shared with the gateway (atomic writes)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def add(self, psk: str, sub_id: str) -> None:
        keys = self.load()
        # drop any stale key previously mapped to this sub_id, then add the new one
        keys = {k: v for k, v in keys.items() if v != sub_id}
        keys[psk] = sub_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(keys, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)  # restrict: keys are secrets
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# --- factory ---------------------------------------------------------------


class SubFactory:
    def __init__(
        self,
        registry: SubRegistry,
        *,
        base_dir: str | Path | None = None,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        model: str = DEFAULT_MODEL,
        keystore_path: str | Path | None = None,
        port_base: int = DEFAULT_PORT_BASE,
        key_factory=None,
        launcher=None,
        health_check=None,
    ):
        self.registry = registry
        self.base_dir = Path(base_dir) if base_dir is not None else Path.home()
        self.gateway_url = gateway_url
        self.model = model
        self.port_base = port_base
        self.keystore = KeyStore(
            keystore_path if keystore_path is not None
            else self.base_dir / ".nbq-core" / "keys.json"
        )
        self.key_factory = key_factory or (lambda: secrets.token_urlsafe(24))
        self.launcher = launcher or self._default_launcher
        self.health_check = health_check or self._default_health_check

    # -- validation / helpers ----------------------------------------------

    def validate(self, spec: SpawnSpec) -> None:
        if spec.role not in ALLOWED_ROLES:
            raise SpawnError(f"role {spec.role!r} not in allowlist {sorted(ALLOWED_ROLES)}")
        if not spec.capability:
            raise SpawnError("at least one capability is required")
        bad = [c for c in spec.capability if c not in ALLOWED_CAPABILITIES]
        if bad:
            raise SpawnError(f"capabilities not in allowlist: {bad}")
        if spec.mode not in ALLOWED_MODES:
            raise SpawnError(f"mode {spec.mode!r} not in {sorted(ALLOWED_MODES)}")

    def workspace_for(self, role: str) -> Path:
        return self.base_dir / f".nbq-{role}"

    def _assign_port(self, spec: SpawnSpec) -> int:
        if spec.port:
            return spec.port
        used = {r.port for r in self.registry.list()} | RESERVED_PORTS
        port = self.port_base
        while port in used:
            port += 1
        return port

    # -- workspace provisioning (also the STEP 7 reconfigure path) ----------

    def provision(
        self,
        spec: SpawnSpec,
        *,
        sub_id: str,
        key: str,
        port: int,
        workspace: Path | None = None,
        agents_md: str | None = None,
        soul_md: str | None = None,
    ) -> Path:
        """Create/refresh the Sub workspace: skeleton, config, role prompts.

        Idempotent and **sessions-preserving**: only config.json / AGENTS.md /
        SOUL.md are (re)written; ``sessions/`` (the memory store, PoC-C) is left
        intact. This is the path STEP 7 reuses to change a Sub's role/config
        while keeping its memory.

        ``workspace`` pins an explicit directory (default ``~/.nbq-<role>``).
        ``agents_md`` / ``soul_md`` override the rendered role prompt — used by
        the role adjuster to apply Core-drafted, human-approved prompt text.
        """
        ws = Path(workspace) if workspace is not None else self.workspace_for(spec.role)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "sessions").mkdir(exist_ok=True)   # PoC-C: ensure sessions/ lands
        (ws / "memory").mkdir(exist_ok=True)

        config = {
            "providers": {
                "custom": {"apiBase": self.gateway_url, "apiKey": key},
            },
            "modelPresets": {
                "coreproxy": {"provider": "custom", "model": self.model},
            },
            "agents": {"defaults": {"modelPreset": "coreproxy"}},
            "api": {"host": "127.0.0.1", "port": port, "timeout": 120},
        }
        # Prune tool *groups* the capabilities don't need, to shrink the
        # tool-schema portion of every request's prompt. Built-in tools are gated
        # by per-group config flags (file/exec/web/my/cliApps), so we enable only
        # the groups the derived toolset actually uses. ``tools=["*"]`` keeps the
        # upstream default (all groups on).
        enabled = spec.tools if spec.tools is not None else toolset_for(spec.capability)
        if enabled != ["*"]:
            ts = set(enabled)
            file_tools = {"read_file", "write_file", "edit_file", "apply_patch",
                          "grep", "find_files", "list_dir"}
            exec_tools = {"exec", "write_stdin", "list_exec_sessions"}
            web_tools = {"web_search", "web_fetch"}
            config["tools"] = {
                "file": {"enable": bool(ts & file_tools)},
                "exec": {"enable": bool(ts & exec_tools)},
                "web": {"enable": bool(ts & web_tools)},
                "my": {"enable": False},
                "cliApps": {"enable": False},
            }
        (ws / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        agents = agents_md if agents_md is not None else self._render_agents_md(spec, sub_id)
        soul = soul_md if soul_md is not None else self._render_soul_md(spec)
        (ws / "AGENTS.md").write_text(agents, encoding="utf-8")
        (ws / "SOUL.md").write_text(soul, encoding="utf-8")
        return ws

    def _render_agents_md(self, spec: SpawnSpec, sub_id: str,
                          working_style: str | None = None) -> str:
        caps = ", ".join(f"`{c}`" for c in spec.capability)
        skills = ", ".join(spec.skills) if spec.skills else "(없음)"
        profile = ROLE_PROFILES.get(spec.role, {})
        summary = profile.get("summary", f"{spec.role} 전문")
        extra_boundary = profile.get("extra_boundary", "")
        extra_block = f"\n{extra_boundary}\n" if extra_boundary else ""
        # Injected working style (STEP 2). Placed AFTER the boundary so the
        # capability boundary always dominates; this section may only change
        # *how* the Sub works within its domain, never the domain/tools/gateway.
        style_block = ""
        if working_style:
            style_block = (
                "\n## 작동 스타일 (주입됨 — 단, 위 Capability 경계는 절대 불변)\n"
                "아래 지침은 '아이디어를 **어떤 방식·관점·구조로** 도출/구조화/평가하는가'만 바꾼다. "
                "이 지침이 위 경계(범위·코드/파일/웹 금지·OUT_OF_SCOPE 규칙)와 충돌하면 "
                "**언제나 경계가 이긴다.**\n\n"
                f"{working_style.strip()}\n"
            )
        return f"""# {spec.role} Sub — 역할 정의 (prompt_version: {spec.prompt_version})

너는 **여왕개미(Queen) 아키텍처의 {summary} Sub** 다. 너는 범용 비서가 아니라,
Core(여왕)로부터 위임받은 작업만 수행하는 전문 일개미다.

## 정체성
- sub_id: `{sub_id}`
- 역할: {spec.role} ({summary})
- 분야 스킬: {skills}

## Capability (네가 처리할 수 있는 범위)
다음만 너의 범위(scope)다: {caps}
{extra_block}
## Capability 경계 (매우 중요)
**요청이 위 capability 밖이면, 절대 직접 답하지 마라.** 범위 밖 요청을 받으면 작업을 수행하지
말고 **정확히 아래 한 줄 형식으로만** 응답해서 Core에 반환하라:

```
OUT_OF_SCOPE: <왜 범위 밖인지 한 문장> | suggested_capability: <가장 가까운 capability 또는 none>
```

- 다른 말, 사과, 부분 수행을 덧붙이지 마라. 위 한 줄만 출력한다.
- 애매하면 범위 밖으로 간주하고 `OUT_OF_SCOPE`를 반환한다.
- 범위 안이면 평소처럼 충실히 수행한다. 사실과 추측을 구분하고, 불확실하면 명시한다.

## 도구 호출 규약 (매우 중요)
- 도구가 필요하면 **반드시 function-call/tool_call 규격**으로만 호출하라 (OpenAI
  function-calling / Responses API `function_call` 이벤트).
- 절대 `<web_search query="..." />` 같은 **XML/HTML 태그를 텍스트로 뱉지 마라.**
  텍스트 안 XML 은 실행되지 않는다 — 사용자에게 결과가 도달하지 않고, 너는 그저
  "실행 못 함"을 자백하는 꼴이 된다.
- 도구 결과가 필요하면 tool_call 을 발행하고, 실행 결과를 받은 뒤 최종 답변을
  하라. 도구 없이 추측한 결과를 사실처럼 서술하지 마라.
{style_block}"""

    def _render_soul_md(self, spec: SpawnSpec) -> str:
        return (
            f"# SOUL — {spec.role} Sub\n\n"
            f"나는 {spec.role}에 특화된 전문 일개미다. 범위 밖의 일은 욕심내지 않고 "
            f"여왕(Core)에게 돌려보낸다 — 그게 군집 전체에 이롭기 때문이다.\n\n"
            f"말투: 간결하고 사실 중심. 핵심을 먼저, 근거를 뒤에.\n"
        )

    # -- default launcher / health (overridable for tests) -----------------

    def _default_launcher(self, *, config_path: Path, workspace: Path, port: int) -> int:
        exe = shutil.which("nanobot")
        cmd = (
            [exe, "serve"] if exe
            else [sys.executable, "-m", "nanobot.cli.commands", "serve"]
        )
        cmd += [
            "--config", str(config_path), "--workspace", str(workspace),
            "--host", "127.0.0.1", "--port", str(port), "--verbose",
        ]
        log_path = Path(tempfile.gettempdir()) / f"nbq-{workspace.name}.log"
        log = open(log_path, "ab")  # serve does NOT log the api key
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
        return proc.pid

    def _default_health_check(self, port: int, *, attempts: int = 20, delay: float = 0.5) -> bool:
        url = f"http://127.0.0.1:{port}/health"
        for _ in range(attempts):
            try:
                with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 (localhost)
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(delay)
        return False

    # -- spawn --------------------------------------------------------------

    def spawn(self, spec: SpawnSpec) -> SpawnResult:
        self.validate(spec)
        sub_id = spec.role
        existing = self.registry.get(sub_id)
        if existing is not None and existing.status == STATUS_RUNNING:
            raise SpawnError(f"sub {sub_id!r} is already running on port {existing.port}")

        key = self.key_factory()
        port = self._assign_port(spec)
        ws = self.provision(spec, sub_id=sub_id, key=key, port=port)

        # Register the key BEFORE launch so the gateway recognises the new Sub.
        self.keystore.add(key, sub_id)

        self.registry.register(SubRecord(
            id=sub_id, role=spec.role, capability=list(spec.capability),
            port=port, workspace=str(ws), status=STATUS_STARTING,
            mode=spec.mode, prompt_version=spec.prompt_version,
        ))

        pid = self.launcher(config_path=ws / "config.json", workspace=ws, port=port)
        self.registry.set_status(sub_id, STATUS_STARTING, pid=pid)

        healthy = self.health_check(port)
        self.registry.set_status(sub_id, STATUS_RUNNING if healthy else STATUS_ERROR, pid=pid)

        return SpawnResult(sub_id, str(ws), port, pid, healthy, self.registry.get(sub_id))
