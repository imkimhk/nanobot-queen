"""Unit tests for the Queen Sub factory: allowlist, provisioning, registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.queen.factory import (
    SpawnError,
    SpawnSpec,
    SubFactory,
    toolset_for,
)
from nanobot.queen.registry import STATUS_ERROR, STATUS_RUNNING, SubRegistry


@pytest.fixture
def env(tmp_path):
    registry = SubRegistry(tmp_path / "subs.json")
    launches: list[dict] = []

    def launcher(*, config_path, workspace, port):
        launches.append({"config": Path(config_path), "ws": Path(workspace), "port": port})
        return 4242

    factory = SubFactory(
        registry,
        base_dir=tmp_path,
        keystore_path=tmp_path / ".nbq-core" / "keys.json",
        key_factory=lambda: "UNIQUE-KEY-123",
        launcher=launcher,
        health_check=lambda port: True,
    )
    return {"registry": registry, "factory": factory, "launches": launches, "root": tmp_path}


def _coder_spec(**over):
    base = dict(role="coder", capability=["code.write", "code.review"], mode="on_demand")
    base.update(over)
    return SpawnSpec(**base)


# --- happy path ------------------------------------------------------------


def test_spawn_registers_and_is_healthy(env):
    res = env["factory"].spawn(_coder_spec())
    assert res.sub_id == "coder"
    assert res.healthy is True
    assert res.pid == 4242

    rec = env["registry"].get("coder")
    assert rec is not None
    assert rec.status == STATUS_RUNNING
    assert rec.capability == ["code.write", "code.review"]
    assert rec.port >= 8902 and rec.port not in (8900, 8901)
    assert rec.workspace.endswith(".nbq-coder")


def test_spawn_creates_sessions_dir_and_config_to_gateway(env):
    res = env["factory"].spawn(_coder_spec())
    ws = Path(res.workspace)
    # PoC-C: sessions/ must exist for memory persistence
    assert (ws / "sessions").is_dir()
    assert (ws / "memory").is_dir()

    cfg = json.loads((ws / "config.json").read_text())
    assert cfg["providers"]["custom"]["apiBase"] == "http://127.0.0.1:8900/v1"
    assert cfg["providers"]["custom"]["apiKey"] == "UNIQUE-KEY-123"
    assert cfg["api"]["port"] == res.port


def test_role_prompt_has_capability_boundary(env):
    res = env["factory"].spawn(_coder_spec())
    agents = (Path(res.workspace) / "AGENTS.md").read_text()
    assert "OUT_OF_SCOPE" in agents
    assert "code.write" in agents and "code.review" in agents
    assert "sub_id: `coder`" in agents


def test_unique_key_recorded_in_keystore_not_in_registry(env):
    res = env["factory"].spawn(_coder_spec())
    keystore = json.loads((env["root"] / ".nbq-core" / "keys.json").read_text())
    assert keystore == {"UNIQUE-KEY-123": "coder"}
    # the secret must NOT be stored in the registry record
    assert "UNIQUE-KEY-123" not in json.dumps(res.record.to_dict())


def test_spawned_config_is_loadable_by_nanobot(env):
    # Proves the generated config is a valid nanobot config (provider wiring ok).
    res = env["factory"].spawn(_coder_spec())
    from nanobot.config.loader import load_config
    cfg = load_config(Path(res.workspace) / "config.json")
    preset = cfg.resolve_preset()
    assert preset.provider == "custom"
    assert cfg.providers.custom.api_base == "http://127.0.0.1:8900/v1"


# --- tool pruning (STEP 9: shrink per-request prompt) ----------------------


def test_toolset_for_derives_minimal_set():
    ts = toolset_for(["research.web", "research.summary"])
    assert "web_search" in ts and "read_file" in ts and "message" in ts
    assert "exec" not in ts and "apply_patch" not in ts


def test_provision_disables_unneeded_tool_groups(env):
    res = env["factory"].spawn(_coder_spec())  # code.write/code.review
    cfg = json.loads((Path(res.workspace) / "config.json").read_text())
    tools = cfg["tools"]
    assert tools["file"]["enable"] is True      # read/write/edit needed
    assert tools["exec"]["enable"] is True       # code.write -> exec
    assert tools["web"]["enable"] is False       # coder needs no web
    assert tools["my"]["enable"] is False        # self-mod off
    assert tools["cliApps"]["enable"] is False


def test_research_disables_exec(tmp_path):
    registry = SubRegistry(tmp_path / "subs.json")
    f = SubFactory(registry, base_dir=tmp_path,
                   keystore_path=tmp_path / ".nbq-core" / "keys.json",
                   key_factory=lambda: "K", launcher=lambda **k: 1, health_check=lambda p: True)
    res = f.spawn(SpawnSpec(role="research", capability=["research.web", "research.summary"]))
    tools = json.loads((Path(res.workspace) / "config.json").read_text())["tools"]
    assert tools["web"]["enable"] is True
    assert tools["exec"]["enable"] is False


def test_star_tools_keeps_all_groups(tmp_path):
    registry = SubRegistry(tmp_path / "subs.json")
    f = SubFactory(registry, base_dir=tmp_path,
                   keystore_path=tmp_path / ".nbq-core" / "keys.json",
                   key_factory=lambda: "K", launcher=lambda **k: 1, health_check=lambda p: True)
    res = f.spawn(SpawnSpec(role="coder", capability=["code.write"], tools=["*"]))
    cfg = json.loads((Path(res.workspace) / "config.json").read_text())
    assert "tools" not in cfg  # upstream default: all tools registered


# --- allowlist (safety) ----------------------------------------------------


def test_reject_role_not_in_allowlist(env):
    with pytest.raises(SpawnError, match="role"):
        env["factory"].spawn(SpawnSpec(role="hacker", capability=["code.write"]))


def test_reject_capability_not_in_allowlist(env):
    with pytest.raises(SpawnError, match="capabilities"):
        env["factory"].spawn(SpawnSpec(role="coder", capability=["system.exec"]))


def test_reject_empty_capability(env):
    with pytest.raises(SpawnError, match="capability"):
        env["factory"].spawn(SpawnSpec(role="coder", capability=[]))


def test_reject_bad_mode(env):
    with pytest.raises(SpawnError, match="mode"):
        env["factory"].spawn(_coder_spec(mode="sometimes"))


# --- lifecycle / ports -----------------------------------------------------


def test_unique_ports_for_distinct_roles(env):
    r1 = env["factory"].spawn(_coder_spec())
    r2 = env["factory"].spawn(SpawnSpec(role="writer", capability=["writing.draft"]))
    assert r1.port != r2.port
    assert {r1.port, r2.port}.isdisjoint({8900, 8901})


def test_spawn_twice_running_raises(env):
    env["factory"].spawn(_coder_spec())
    with pytest.raises(SpawnError, match="already running"):
        env["factory"].spawn(_coder_spec())


def test_health_failure_marks_error(tmp_path):
    registry = SubRegistry(tmp_path / "subs.json")
    factory = SubFactory(
        registry, base_dir=tmp_path,
        keystore_path=tmp_path / ".nbq-core" / "keys.json",
        key_factory=lambda: "K", launcher=lambda **k: 1,
        health_check=lambda port: False,   # health never comes up
    )
    res = factory.spawn(_coder_spec())
    assert res.healthy is False
    assert registry.get("coder").status == STATUS_ERROR


# --- STEP 7 readiness: re-provision preserves sessions/ --------------------


def test_reprovision_preserves_sessions(env):
    res = env["factory"].spawn(_coder_spec())
    ws = Path(res.workspace)
    # simulate an accumulated memory file
    mem = ws / "sessions" / "api_default.jsonl"
    mem.write_text('{"role":"user","content":"remember X"}\n', encoding="utf-8")

    # re-provision with a changed role prompt (STEP 7 style) — sessions kept
    env["factory"].provision(_coder_spec(prompt_version="v2"), sub_id="coder",
                             key="UNIQUE-KEY-123", port=res.port)
    assert mem.exists()
    assert "remember X" in mem.read_text()
    assert "v2" in (ws / "AGENTS.md").read_text()
