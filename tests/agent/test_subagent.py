"""Tests for SubagentManager."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunResult
from nanobot.agent.subagent import SubagentManager, SubagentStatus
from nanobot.agent.tools.filesystem import FileToolsConfig
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMProvider


@pytest.mark.asyncio
async def test_subagent_uses_tool_loader():
    """Verify subagent registers tools via ToolLoader, not hard-coded imports."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=Path("/tmp"),
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )
    tools = sm._build_tools()
    assert tools.has("read_file")
    assert tools.has("write_file")
    assert not tools.has("message")
    assert not tools.has("spawn")


@pytest.mark.asyncio
async def test_subagent_build_tools_isolates_file_read_state(tmp_path):
    """Each spawned subagent needs a fresh file-state cache."""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )

    first_read = sm._build_tools().get("read_file")
    second_read = sm._build_tools().get("read_file")

    assert first_read is not second_read
    assert (await first_read.execute(path="note.txt")).startswith("1| hello")
    second_result = await second_read.execute(path="note.txt")
    assert second_result.startswith("1| hello")
    assert "File unchanged" not in second_result


def test_subagent_respects_file_tool_toggle(tmp_path):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
        tools_config=ToolsConfig(file=FileToolsConfig(enable=False)),
    )

    tools = sm._build_tools()

    file_tools = {
        "apply_patch",
        "edit_file",
        "find_files",
        "grep",
        "list_dir",
        "read_file",
        "write_file",
    }
    assert file_tools.isdisjoint(tools.tool_names)


@pytest.mark.asyncio
async def test_subagent_forwards_fail_on_tool_error_to_runner(tmp_path):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
        fail_on_tool_error=False,
    )
    sm.runner.run = AsyncMock(
        return_value=AgentRunResult(final_content="ok", messages=[], stop_reason="completed")
    )
    sm._announce_result = AsyncMock()

    status = SubagentStatus(
        task_id="t1",
        label="label",
        task_description="task",
        started_at=0.0,
    )

    await sm._run_subagent("t1", "task", "label", {"channel": "cli", "chat_id": "direct"}, status)

    spec = sm.runner.run.call_args.args[0]
    assert spec.fail_on_tool_error is False


@pytest.mark.asyncio
async def test_subagent_inherits_disabled_tool_groups():
    """A subagent must not regain tool groups the parent disabled.

    Regression for the cli_apps inheritance gap: _subagent_tools_config only
    copied exec/web/file, so a parent with cli_apps disabled spawned a subagent
    that got run_cli_app back via ToolsConfig defaults.
    """
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"

    # parent with cli_apps disabled (like a Queen idea Sub)
    restricted = ToolsConfig.model_validate({"cliApps": {"enable": False}})
    sm = SubagentManager(
        provider=provider, workspace=Path("/tmp"), bus=MessageBus(),
        model="test", max_tool_result_chars=16_000, tools_config=restricted,
    )
    sub_cfg = sm._subagent_tools_config()
    assert sub_cfg.cli_apps.enable is False          # inherited, not reset to default
    assert not sm._build_tools().has("run_cli_app")  # tool group not regained

    # parent with cli_apps enabled still passes it through (no regression)
    allowed = ToolsConfig.model_validate({"cliApps": {"enable": True}})
    sm2 = SubagentManager(
        provider=provider, workspace=Path("/tmp"), bus=MessageBus(),
        model="test", max_tool_result_chars=16_000, tools_config=allowed,
    )
    assert sm2._subagent_tools_config().cli_apps.enable is True
    assert sm2._build_tools().has("run_cli_app")
