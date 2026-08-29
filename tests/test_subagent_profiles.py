from agent_harness import call, session

from yucode.agent_profile import AgentProfileLibrary
from yucode.base import RuntimeSettings
from yucode.context import ContextManager
from yucode.runner import ToolRunner
from yucode.tools import ReadTool, Tool, ToolCatalog


def test_session_tool_catalog_is_the_schema_and_execution_authority(tmp_path):
    s = session(tmp_path)
    s.tool_catalog = ToolCatalog((ReadTool,))

    names = {schema["function"]["name"] for schema in Tool.resolved_schemas(s)}
    result = ToolRunner(s, ContextManager(s), output_fn=lambda _text: None).run([call("Bash", [{"command": "pwd"}])])

    assert names == {"Read"}
    assert "unknown tool Bash" in result[0]["content"]


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
