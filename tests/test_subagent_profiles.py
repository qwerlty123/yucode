from agent_harness import call, session

from yucode.agent_profile import AgentProfileLibrary
from yucode.base import RuntimeSettings
from yucode.context import ContextManager
from yucode.model import ModelClient
from yucode.runner import ToolRunner
from yucode.subagent import AgentSpec, SubagentRuntime
from yucode.tools import ReadTool, Tool, ToolCatalog


def test_session_tool_catalog_is_the_schema_and_execution_authority(tmp_path):
    s = session(tmp_path)
    s.tool_catalog = ToolCatalog((ReadTool,))

    names = {schema["function"]["name"] for schema in Tool.resolved_schemas(s)}
    result = ToolRunner(s, ContextManager(s), output_fn=lambda _text: None).run([call("Bash", [{"command": "pwd"}])])

    assert names == {"Read"}
    assert "unknown tool Bash" in result[0]["content"]


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
