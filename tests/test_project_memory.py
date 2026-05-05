import json
import os

import pytest

from yucode.base import Config, ToolError
from yucode.context import ContextManager
from yucode.memory import ProjectMemory
from yucode.session import Session
from yucode.tools import MemoryTool


def memory(tmp_path):
    return ProjectMemory(str(tmp_path / "memory"))


def test_project_memory_persists_topics_and_rebuilds_index(tmp_path):
    store = memory(tmp_path)

    saved = store.remember(
        "feedback-real-database-tests",
        "feedback",
        "Integration tests must use a real database",
        "Use a real database for migration and persistence integration tests.\n\nWhy: mocks previously hid a broken migration.",
    )

    assert saved.id == "feedback-real-database-tests"
    assert ProjectMemory(str(tmp_path / "memory")).find(ids=[saved.id]) == [saved]
    assert "(feedback-real-database-tests.md) [feedback]" in (tmp_path / "memory" / "MEMORY.md").read_text()

    updated = store.remember(saved.id, "feedback", "Real database integration tests", "Updated guidance")
    assert store.find(ids=[saved.id]) == [updated]
    assert len(store.find(query="database")) == 1

    assert store.forget(saved.id) is True
    assert store.forget(saved.id) is False
    assert store.find() == []
    assert "feedback-real-database-tests" not in (tmp_path / "memory" / "MEMORY.md").read_text()


def test_project_memory_rejects_unsafe_or_oversized_values(tmp_path):
    store = memory(tmp_path)

    with pytest.raises(ToolError, match="memory id"):
        store.remember("../escape", "feedback", "description", "content")
    with pytest.raises(ToolError, match="memory type"):
        store.remember("topic", "code", "description", "content")
    with pytest.raises(ToolError, match="description"):
        store.remember("topic", "feedback", "", "content")
    with pytest.raises(ToolError, match="content"):
        store.remember("topic", "feedback", "description", "x" * (ProjectMemory.MAX_CONTENT_CHARS + 1))

    assert not os.path.exists(tmp_path / "escape.md")


def test_memory_tool_is_project_persistent_and_supports_recall(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"))
    first = Session(cwd=str(tmp_path / "project"), config=config)

    saved = json.loads(
        MemoryTool(
            first,
            [
                {
                    "action": "remember",
                    "id": "user-response-style",
                    "type": "user",
                    "description": "User prefers concise answers",
                    "content": "Keep final answers concise and avoid repetitive summaries.",
                }
            ],
        ).call()
    )
    assert saved == {
        "ok": True,
        "memory": {"id": "user-response-style", "type": "user", "description": "User prefers concise answers"},
    }

    second = Session(cwd=str(tmp_path / "project"), config=config)
    listed = json.loads(MemoryTool(second, [{"action": "list"}]).call())
    recalled = json.loads(MemoryTool(second, [{"action": "get", "id": "user-response-style"}]).call())

    assert listed["memories"] == [saved["memory"]]
    assert recalled["memory"]["content"] == "Keep final answers concise and avoid repetitive summaries."
    assert MemoryTool(second, [{"action": "remember"}]).needs_confirmation() is False


def test_memory_tool_validates_action_specific_fields(tmp_path):
    session = Session(cwd=str(tmp_path), config=Config(data_dir=str(tmp_path / "data")))

    with pytest.raises(ToolError, match="Memory search requires query"):
        MemoryTool(session, [{"action": "search"}]).call()
    with pytest.raises(ToolError, match="Memory remember requires"):
        MemoryTool(session, [{"action": "remember", "id": "topic"}]).call()
    with pytest.raises(ToolError, match="Memory unexpected field"):
        MemoryTool(session, [{"action": "list", "content": "noise"}]).call()


def test_memory_context_is_session_stable_and_new_sessions_see_updates(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"))
    first = Session(cwd=str(tmp_path / "project"), config=config)
    context = ContextManager(first)

    before = context.model_messages("system")
    assert not any("--- Project Memory" in str(message.get("content") or "") for message in before)

    first.memory.remember("project-release", "project", "Release freeze starts Friday", "Release freeze starts on 2026-08-21.")
    after = context.model_messages("system")
    assert after == before

    resumed_context = ContextManager(Session(cwd=str(tmp_path / "project"), config=config)).model_messages("system")
    contents = [str(message.get("content") or "") for message in resumed_context]
    assert any("--- Project Memory (session-start snapshot) ---" in content for content in contents)
    assert any("project-release" in content for content in contents)
