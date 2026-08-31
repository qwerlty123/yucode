import os

import pytest
from agent_harness import call, session

from yucode.agent_profile import AgentProfileLibrary
from yucode.base import RuntimeSettings, ToolError
from yucode.context import ContextManager
from yucode.engine import Agent
from yucode.model import ModelClient
from yucode.runner import ToolRunner
from yucode.subagent import AgentSpec, SubagentRuntime
from yucode.tools import AgentTaskTool, AgentTool, ReadTool, Tool, ToolCatalog


def test_agent_tool_describes_the_complete_delegation_contract():
    agent_description = AgentTool.DESCRIPTION
    task_description = AgentTaskTool.DESCRIPTION

    assert "fresh child has no parent conversation" in agent_description
    assert "do not poll, race the child, or invent its result" in agent_description
    assert "multiple independent Agent calls in one response" in agent_description
    assert "worktree isolates changes and excludes parent dirty content" in agent_description
    assert "Do not poll background tasks" in task_description


def test_builtin_profiles_define_role_process_and_output_contracts(tmp_path):
    library = AgentProfileLibrary.load(session(tmp_path))
    general = library.get("general-purpose")
    explore = library.get("explore")
    plan = library.get("plan")

    assert general is not None and "Preserve unrelated working-tree changes" in general.prompt
    assert "Verify changes in proportion to their risk" in general.prompt
    assert explore is not None and "read-only codebase exploration specialist" in explore.prompt
    assert "exact file paths and symbols" in explore.prompt
    assert plan is not None and "decision-complete plan" in plan.prompt
    assert "concrete files and symbols" in plan.prompt


def test_session_tool_catalog_is_the_schema_and_execution_authority(tmp_path):
    s = session(tmp_path)
    s.tool_catalog = ToolCatalog((ReadTool,))

    names = {schema["function"]["name"] for schema in Tool.resolved_schemas(s)}
    result = ToolRunner(s, ContextManager(s), output_fn=lambda _text: None).run([call("Bash", [{"command": "pwd"}])])

    assert names == {"Read"}
    assert "unknown tool Bash" in result[0]["content"]


def test_empty_tool_catalog_does_not_fall_back_to_global_tools(tmp_path):
    s = session(tmp_path)
    s.tool_catalog = ToolCatalog(())

    names = {schema["function"]["name"] for schema in Tool.resolved_schemas(s)}
    result = ToolRunner(s, ContextManager(s), input_fn=lambda _prompt: "n", output_fn=lambda _text: None).run([call("Bash", [{"command": "pwd"}])])

    assert names == set()
    assert "unknown tool Bash" in result[0]["content"]


def test_explicit_empty_profile_allowlist_grants_no_tools(tmp_path):
    directory = tmp_path / ".yucode" / "agents" / "no-tools"
    directory.mkdir(parents=True)
    (directory / "AGENT.md").write_text(
        "---\nname: no-tools\ndescription: 不使用工具\ntools:\n---\n只返回文字。\n",
        encoding="utf-8",
    )
    s = session(tmp_path)
    s.config.provider.model = "test-model"
    seen = []
    runtime = SubagentRuntime(s, executor=lambda child, _prompt: seen.extend(child.tool_catalog.names) or "完成")

    task = runtime.spawn(AgentSpec("空能力", "执行", subagent_type="no-tools"))

    assert task.status == "completed"
    assert seen == []
    runtime.close()


def test_model_argument_parser_uses_the_same_session_catalog(tmp_path):
    s = session(tmp_path)
    s.tool_catalog = ToolCatalog((ReadTool,))
    client = ModelClient(s)
    message = {
        "tool_calls": [
            {"id": "1", "function": {"name": "Bash", "arguments": '{"command":"pwd"}'}},
            {"id": "2", "function": {"name": "Read", "arguments": '{"path":"a.txt"}'}},
        ]
    }

    calls = client.tool_calls(message)

    assert calls[0].args == [{"command": "pwd"}]
    assert calls[1].args == [{"path": "a.txt", "ranges": [[0, 0]]}]


def test_child_catalog_requires_an_explicit_child_safe_capability(tmp_path):
    class RootOnlyTool(Tool):
        NAME = "RootOnly"

    s = session(tmp_path)
    s.config.provider.model = "test-model"
    s.tool_catalog = ToolCatalog((ReadTool, RootOnlyTool))
    seen = []
    runtime = SubagentRuntime(s, executor=lambda child, _prompt: seen.extend(child.tool_catalog.names) or "完成")

    task = runtime.spawn(AgentSpec("边界", "执行"))

    assert task.status == "completed"
    assert "Read" in seen
    assert "RootOnly" not in seen
    runtime.close()


def test_root_agent_schema_exposes_valid_profiles_but_child_has_no_agent_tool(tmp_path):
    custom = tmp_path / ".yucode" / "agents" / "reviewer"
    custom.mkdir(parents=True)
    (custom / "AGENT.md").write_text(
        "---\nname: reviewer\ndescription: 审查变更\ntools: Read\n---\n检查。\n",
        encoding="utf-8",
    )
    root = session(tmp_path)
    root.config.provider.model = "test-model"

    Agent(root, output_fn=lambda _text: None)
    schemas = Tool.resolved_schemas(root)
    agent_schema = next(schema for schema in schemas if schema["function"]["name"] == "Agent")
    profile_schema = agent_schema["function"]["parameters"]["properties"]["subagent_type"]
    model_schema = agent_schema["function"]["parameters"]["properties"]["model"]

    assert root.subagents is not None
    assert "reviewer" in profile_schema["enum"]
    assert "reviewer: 审查变更" in profile_schema["description"]
    assert model_schema["enum"] == ["inherit", "test-model"]

    child_indexes = []
    root.subagents._executor = lambda child, _prompt: child_indexes.append({schema["function"]["name"] for schema in Tool.resolved_schemas(child)}) or "完成"
    root.subagents.spawn(AgentSpec("子任务", "执行"))
    assert "Agent" not in child_indexes[0]
    assert "AgentTask" not in child_indexes[0]
    root.subagents.close()


def test_agent_profiles_follow_builtin_user_project_precedence(tmp_path):
    s = session(tmp_path)
    user = tmp_path / "data" / "agents" / "general-purpose"
    project = tmp_path / ".yucode" / "agents" / "review"
    user.mkdir(parents=True)
    project.mkdir(parents=True)
    (user / "AGENT.md").write_text(
        "---\nname: general-purpose\ndescription: 用户覆盖\ntools: Read, Search\n---\n只做研究。\n",
        encoding="utf-8",
    )
    (project / "AGENT.md").write_text(
        "---\nname: review\ndescription: 项目审查\ntools: Read, Missing\n---\n检查实现。\n",
        encoding="utf-8",
    )

    library = AgentProfileLibrary.load(s)

    assert {profile.name for profile in library.all()} >= {"general-purpose", "explore", "plan", "review"}
    general = library.get("GENERAL-PURPOSE")
    assert general is not None
    assert general.source == "user"
    assert general.tools == ("Read", "Search")
    assert general.prompt == "只做研究。"
    review = library.get("review")
    assert review is not None
    assert review.source == "project"
    assert review.valid is False
    assert review.errors == ("未知工具: Missing",)


def test_unreadable_custom_profile_root_does_not_block_builtin_profiles(tmp_path, monkeypatch):
    s = session(tmp_path)
    root = tmp_path / ".yucode" / "agents"
    root.mkdir(parents=True)
    real_listdir = os.listdir

    def listdir(path):
        if os.path.abspath(path) == os.path.abspath(root):
            raise PermissionError("不可读")
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", listdir)

    library = AgentProfileLibrary.load(s)

    assert library.get("general-purpose").valid


def test_agent_profile_names_are_case_insensitive_and_ambiguous_peers_are_invalid(tmp_path):
    s = session(tmp_path)
    first = tmp_path / ".yucode" / "agents" / "one"
    second = tmp_path / ".yucode" / "agents" / "two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    body = "---\nname: Review\ndescription: 审查\ntools: Read\n---\n检查。\n"
    (first / "AGENT.md").write_text(body, encoding="utf-8")
    (second / "AGENT.md").write_text(body.replace("Review", "review"), encoding="utf-8")

    profile = AgentProfileLibrary.load(s).get("REVIEW")

    assert profile is not None
    assert profile.valid is False
    assert any("名称歧义" in error for error in profile.errors)


def test_agent_profile_rejects_a_name_outside_the_library_namespace(tmp_path):
    s = session(tmp_path)
    directory = tmp_path / ".yucode" / "agents" / "unsafe"
    directory.mkdir(parents=True)
    (directory / "AGENT.md").write_text(
        "---\nname: ../unsafe\ndescription: 非法名称\ntools: Read\n---\n检查。\n",
        encoding="utf-8",
    )

    profile = AgentProfileLibrary.load(s).get("../unsafe")

    assert profile is not None
    assert profile.valid is False
    assert any("最长 64 字符" in error for error in profile.errors)


def test_running_task_keeps_profile_snapshot_across_reload(tmp_path):
    s = session(tmp_path)
    s.config.provider.model = "test-model"
    directory = tmp_path / ".yucode" / "agents" / "worker"
    directory.mkdir(parents=True)
    path = directory / "AGENT.md"
    path.write_text("---\nname: worker\ndescription: worker\ntools: Read\n---\n第一版。\n", encoding="utf-8")
    seen = []
    runtime = Agent(s, output_fn=lambda _text: None).session.subagents
    assert runtime is not None
    runtime._executor = lambda child, _prompt: seen.append(child.system_prompt) or "完成"

    first = runtime.start(AgentSpec("快照", "执行", subagent_type="worker", run_in_background=True))
    path.write_text("---\nname: worker\ndescription: worker\ntools: Read\n---\n第二版。\n", encoding="utf-8")
    runtime.reload_profiles()
    second = runtime.start(AgentSpec("快照", "执行", subagent_type="worker", run_in_background=True))
    runtime.wait(first.task_id, 2)
    runtime.wait(second.task_id, 2)

    assert any("第一版。" in prompt for prompt in seen)
    assert any("第二版。" in prompt for prompt in seen)
    runtime.close()


def test_agent_profile_library_creates_copies_and_deletes_custom_profiles(tmp_path):
    s = session(tmp_path)
    library = AgentProfileLibrary.load(s)

    created = library.create("review", source="project")
    copied = library.copy("review", "review-copy", source="user")

    assert created.source == "project"
    assert copied.source == "user"
    assert (tmp_path / ".yucode" / "agents" / "review" / "AGENT.md").is_file()
    assert (tmp_path / "data" / "agents" / "review-copy" / "AGENT.md").is_file()
    with pytest.raises(ToolError, match="只读"):
        library.delete("explore")
    library.delete("review")
    assert library.get("review") is None
    assert not (tmp_path / ".yucode" / "agents" / "review" / "AGENT.md").exists()


def test_agent_profile_library_can_override_a_lower_layer_and_wrap_write_failures(tmp_path):
    s = session(tmp_path)
    library = AgentProfileLibrary.load(s)

    overridden = library.create("explore", source="project")

    assert overridden.source == "project"
    assert library.get("EXPLORE").source == "project"
    blocked = tmp_path / ".yucode" / "agents" / "blocked"
    blocked.mkdir(parents=True)
    with pytest.raises(ToolError, match="创建 Agent profile 失败"):
        library.create("blocked", source="project")


def test_subagent_runtime_settings_have_bounded_defaults_and_config_overrides():
    defaults = RuntimeSettings()
    configured = RuntimeSettings.from_dict(
        {
            "runtime": {
                "max_parallel_agents": 0,
                "max_queued_agents": -2,
                "max_subagent_steps": 12,
                "agent_shutdown_grace_seconds": -1,
            }
        }
    )

    assert (defaults.max_parallel_agents, defaults.max_queued_agents, defaults.max_subagent_steps) == (4, 16, 80)
    assert configured.max_parallel_agents == 1
    assert configured.max_queued_agents == 0
    assert configured.max_subagent_steps == 12
    assert configured.agent_shutdown_grace_seconds == 0
