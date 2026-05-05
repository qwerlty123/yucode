import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from yucode.base import Config, ModelError, ToolError
from yucode.context import ContextManager
from yucode.memory import MemoryConsolidator, ProjectMemory
from yucode.session import Session, SessionSnapshotStore
from yucode.tools import MemoryTool


def memory(tmp_path):
    return ProjectMemory(str(tmp_path / "memory"))


def memory_context(messages):
    return next((str(message.get("content") or "") for message in messages if "--- Project Memory" in str(message.get("content") or "")), "")


def compactable_messages():
    return [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        *({"role": "assistant", "content": f"step {index}"} for index in range(9)),
        {"role": "user", "content": "latest request"},
    ]


def saved_historical_session(config, cwd, uid, updated_at):
    session = Session(cwd=str(cwd), config=config, uid=uid)
    session.state.round_count = 1
    session.messages = [
        {"role": "user", "content": f"Session {uid}: keep answers concise."},
        {"role": "assistant", "content": "Understood."},
    ]
    session.save_snapshot()
    path = SessionSnapshotStore.session_path(config.data_dir, str(cwd), uid)
    timestamp = updated_at.timestamp()
    os.utime(path, (timestamp, timestamp))
    return session


def test_project_memory_persists_topics_and_rebuilds_index(tmp_path):
    store = memory(tmp_path)

    saved = store.remember(
        "feedback-real-database-tests",
        "feedback",
        "Integration tests must use a real database",
        "Use a real database for migration and persistence integration tests.\n\nWhy: mocks previously hid a broken migration.",
    )
    assert saved.id == "feedback-real-database-tests"
    assert saved.freshness == "fresh"
    assert saved.modified_at.endswith("Z")
    assert saved.expires_at.endswith("Z")
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
    with pytest.raises(ToolError, match="expires_at"):
        store.remember("topic", "feedback", "description", "content", expires_at="not-a-date")

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
    assert saved["ok"] is True
    assert saved["memory"]["id"] == "user-response-style"
    assert saved["memory"]["type"] == "user"
    assert saved["memory"]["description"] == "User prefers concise answers"
    assert saved["memory"]["freshness"] == "fresh"
    assert saved["memory"]["age_days"] == 0

    second = Session(cwd=str(tmp_path / "project"), config=config)
    listed = json.loads(MemoryTool(second, [{"action": "list"}]).call())
    recalled = json.loads(MemoryTool(second, [{"action": "get", "id": "user-response-style"}]).call())

    assert listed["memories"] == [saved["memory"]]
    assert recalled["memory"]["content"] == "Keep final answers concise and avoid repetitive summaries."
    assert MemoryTool(second, [{"action": "remember"}]).needs_confirmation() is False


def test_memory_remember_rejects_blind_overwrite_from_a_new_session(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"))
    cwd = str(tmp_path / "project")
    MemoryTool(
        Session(cwd=cwd, config=config),
        [
            {
                "action": "remember",
                "id": "user-response-style",
                "type": "user",
                "description": "User prefers concise answers",
                "content": "Keep answers concise.",
            }
        ],
    ).call()

    resumed = Session(cwd=cwd, config=config)  # 新会话:还没读过任何正文
    with pytest.raises(ToolError, match="freshly read"):
        MemoryTool(
            resumed,
            [
                {
                    "action": "remember",
                    "id": "user-response-style",
                    "type": "user",
                    "description": "User prefers examples",
                    "content": "Give examples when explaining.",
                }
            ],
        ).call()

    recalled = json.loads(MemoryTool(resumed, [{"action": "get", "id": "user-response-style"}]).call())
    assert recalled["memory"]["content"] == "Keep answers concise."


def test_memory_tool_requires_get_before_updating_an_existing_topic(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"))
    cwd = str(tmp_path / "project")
    MemoryTool(
        Session(cwd=cwd, config=config),
        [
            {
                "action": "remember",
                "id": "user-response-style",
                "type": "user",
                "description": "User prefers concise answers",
                "content": "Keep final answers concise.",
            }
        ],
    ).call()

    resumed = Session(cwd=cwd, config=config)
    overwrite = [
        {
            "action": "remember",
            "id": "user-response-style",
            "type": "user",
            "description": "User prefers concise answers with examples",
            "content": "Keep final answers concise. Use short examples to clarify.",
        }
    ]
    with pytest.raises(ToolError, match="freshly read"):
        MemoryTool(resumed, overwrite).call()

    recalled = json.loads(MemoryTool(resumed, [{"action": "get", "id": "user-response-style"}]).call())
    assert recalled["memory"]["content"] == "Keep final answers concise."

    updated = json.loads(MemoryTool(resumed, overwrite).call())
    assert updated["ok"] is True
    merged = json.loads(MemoryTool(resumed, [{"action": "get", "id": "user-response-style"}]).call())
    assert merged["memory"]["content"] == "Keep final answers concise. Use short examples to clarify."

    again = json.loads(
        MemoryTool(
            resumed,
            [
                {
                    "action": "remember",
                    "id": "user-response-style",
                    "type": "user",
                    "description": "User prefers concise answers with examples and bullets",
                    "content": "Keep final answers concise. Use short examples to clarify. Prefer bullet lists.",
                }
            ],
        ).call()
    )
    assert again["ok"] is True  # 刚写入的正文视为已读,同会话内的后续更新无需重复 get


def test_memory_remember_rejects_update_when_topic_changed_since_read(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"))
    session = Session(cwd=str(tmp_path / "project"), config=config)
    MemoryTool(
        session,
        [
            {
                "action": "remember",
                "id": "project-release",
                "type": "project",
                "description": "Release freeze",
                "content": "Freeze starts Friday.",
            }
        ],
    ).call()
    MemoryTool(session, [{"action": "get", "id": "project-release"}]).call()

    timestamp = datetime(2026, 8, 20, 12, tzinfo=UTC).timestamp()  # 模拟其他进程改动了文件
    os.utime(os.path.join(session.memory.directory, "project-release.md"), (timestamp, timestamp))

    with pytest.raises(ToolError, match="changed since"):
        MemoryTool(
            session,
            [
                {
                    "action": "remember",
                    "id": "project-release",
                    "type": "project",
                    "description": "New release policy",
                    "content": "New policy",
                }
            ],
        ).call()


def test_project_memory_exposes_freshness_and_omits_expired_topics_from_automatic_context(tmp_path):
    written_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    current = [written_at]
    store = ProjectMemory(str(tmp_path / "memory"), now=lambda: current[0])

    store.remember(
        "project-release",
        "project",
        "Release freeze",
        "Release freeze begins on 2026-08-05.",
        expires_at="2026-08-04T12:00:00Z",
    )
    topic = tmp_path / "memory" / "project-release.md"
    timestamp = written_at.timestamp()
    os.utime(topic, (timestamp, timestamp))
    current[0] = written_at + timedelta(days=2)
    aging = store.find(ids=["project-release"])[0]

    assert aging.age_days == 2
    assert aging.freshness == "aging"
    assert "2 days old" in aging.freshness_warning
    assert "aging, updated 2 days ago" in store.context()

    store.reset_context()
    current[0] = written_at + timedelta(days=4)
    expired = store.find(ids=["project-release"])[0]

    assert expired.freshness == "expired"
    assert "expired at 2026-08-04T12:00:00Z" in expired.freshness_warning
    assert store.context() == ""
    assert 'expires_at: "2026-08-04T12:00:00Z"' in topic.read_text()


def test_project_memory_uses_type_default_expiration_for_legacy_files(tmp_path):
    directory = tmp_path / "memory"
    directory.mkdir()
    path = directory / "legacy-project.md"
    path.write_text('---\ntype: project\ndescription: "Legacy project state"\n---\n\nOld state\n')
    written_at = datetime(2026, 8, 1, tzinfo=UTC)
    timestamp = written_at.timestamp()
    os.utime(path, (timestamp, timestamp))
    store = ProjectMemory(str(directory), now=lambda: written_at)

    legacy = store.find(ids=["legacy-project"])[0]

    assert legacy.modified_at == "2026-08-01T00:00:00Z"
    assert legacy.expires_at == "2026-08-31T00:00:00Z"
    assert legacy.freshness == "fresh"


def test_memory_consolidator_runs_after_24_hours_and_five_historical_sessions(tmp_path):
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    config = Config(data_dir=str(tmp_path / "data"))
    cwd = tmp_path / "project"
    for index in range(5):
        saved_historical_session(config, cwd, f"history-{index}", now - timedelta(hours=index + 1))
    current = Session(cwd=str(cwd), config=config, uid="current")
    assert current.memory is not None
    store = ProjectMemory(current.memory.directory, now=lambda: now)
    current.memory = store
    current.messages = [{"role": "user", "content": "Correction: I now prefer detailed explanations."}]
    store.remember("user-response-style", "user", "User prefers concise answers", "Keep answers concise.")
    frozen = store.context()

    class Model:
        def __init__(self):
            self.calls = []

        def consolidate_memory(self, context):
            self.calls.append(context)
            return {
                "operations": [
                    {
                        "action": "upsert",
                        "id": "user-response-style",
                        "type": "user",
                        "description": "User prefers detailed explanations",
                        "content": "Provide detailed explanations; this supersedes the earlier concise-answer preference.",
                    }
                ]
            }

    model = Model()
    outcome = MemoryConsolidator(store).run_if_due(current, model)

    assert outcome.attempted is True
    assert (outcome.upserted, outcome.forgotten, outcome.error) == (1, 0, "")
    assert len(model.calls) == 1
    assert "Correction: I now prefer detailed explanations." in model.calls[0]
    assert all(f'historical_session id="history-{index}"' in model.calls[0] for index in range(5))
    assert store.find(ids=["user-response-style"])[0].description == "User prefers detailed explanations"
    assert store.context() == frozen  # 回合后整理只写磁盘，不打断当前 cache generation。
    assert store.last_consolidated_at() == now

    repeated = MemoryConsolidator(store).run_if_due(current, model)
    assert repeated.attempted is False
    assert len(model.calls) == 1

    current.messages = compactable_messages()
    context = ContextManager(current)
    compacted, keep = context.compaction_parts()
    context.apply_compaction({"summary": "compacted"}, keep, compacted=compacted)

    refreshed = store.context()
    assert refreshed != frozen
    assert "User prefers detailed explanations" in refreshed


def test_memory_consolidator_excludes_current_session_from_five_session_gate(tmp_path):
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    config = Config(data_dir=str(tmp_path / "data"))
    cwd = tmp_path / "project"
    for index in range(4):
        saved_historical_session(config, cwd, f"history-{index}", now - timedelta(hours=index + 1))
    current = saved_historical_session(config, cwd, "current", now)
    assert current.memory is not None
    store = ProjectMemory(current.memory.directory, now=lambda: now)
    current.memory = store

    class Model:
        def consolidate_memory(self, _context):
            raise AssertionError("four historical sessions plus current must not satisfy the gate")

    outcome = MemoryConsolidator(store).run_if_due(current, Model())

    assert outcome.attempted is False
    assert not os.path.exists(os.path.join(store.directory, store.CONSOLIDATION_LOCK_NAME))


def test_memory_consolidator_waits_for_24_hour_interval(tmp_path):
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    last_success = now - timedelta(hours=23)
    config = Config(data_dir=str(tmp_path / "data"))
    cwd = tmp_path / "project"
    for index in range(5):
        saved_historical_session(config, cwd, f"history-{index}", now - timedelta(hours=index + 1))
    current = Session(cwd=str(cwd), config=config, uid="current")
    assert current.memory is not None
    store = ProjectMemory(current.memory.directory, now=lambda: now)
    current.memory = store
    with store.consolidation_lock() as acquired:
        assert acquired is True
    timestamp = last_success.timestamp()
    os.utime(os.path.join(store.directory, store.CONSOLIDATION_LOCK_NAME), (timestamp, timestamp))

    class Model:
        def consolidate_memory(self, _context):
            raise AssertionError("five sessions must not bypass the 24-hour interval")

    outcome = MemoryConsolidator(store).run_if_due(current, Model())

    assert outcome.attempted is False
    assert store.last_consolidated_at() == last_success


def test_memory_consolidator_counts_only_sessions_updated_after_last_success(tmp_path):
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    last_success = now - timedelta(hours=25)
    config = Config(data_dir=str(tmp_path / "data"))
    cwd = tmp_path / "project"
    for index in range(4):
        saved_historical_session(config, cwd, f"recent-{index}", now - timedelta(hours=index + 1))
    saved_historical_session(config, cwd, "too-old", now - timedelta(hours=30))
    current = Session(cwd=str(cwd), config=config, uid="current")
    assert current.memory is not None
    store = ProjectMemory(current.memory.directory, now=lambda: now)
    current.memory = store
    with store.consolidation_lock() as acquired:
        assert acquired is True
    lock_path = os.path.join(store.directory, store.CONSOLIDATION_LOCK_NAME)
    timestamp = last_success.timestamp()
    os.utime(lock_path, (timestamp, timestamp))

    class Model:
        def consolidate_memory(self, _context):
            raise AssertionError("only four sessions were updated after the previous consolidation")

    outcome = MemoryConsolidator(store).run_if_due(current, Model())

    assert outcome.attempted is False
    assert store.last_consolidated_at() == last_success


def test_memory_consolidator_failure_keeps_memory_and_last_success_time(tmp_path):
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    config = Config(data_dir=str(tmp_path / "data"))
    cwd = tmp_path / "project"
    for index in range(5):
        saved_historical_session(config, cwd, f"history-{index}", now - timedelta(hours=index + 1))
    current = Session(cwd=str(cwd), config=config, uid="current")
    assert current.memory is not None
    store = ProjectMemory(current.memory.directory, now=lambda: now)
    current.memory = store
    original = store.remember("user-response-style", "user", "User prefers concise answers", "Keep answers concise.")

    class Model:
        def consolidate_memory(self, _context):
            raise ModelError("bad consolidation response")

    outcome = MemoryConsolidator(store).run_if_due(current, Model())

    assert outcome.attempted is True
    assert "bad consolidation response" in outcome.error
    assert store.find(ids=[original.id]) == [original]
    assert store.last_consolidated_at() == datetime.fromtimestamp(0, UTC)


def test_project_memory_validates_all_consolidation_operations_before_writing(tmp_path):
    store = memory(tmp_path)
    revision = store.revision()

    with pytest.raises(ToolError, match="action"):
        store.apply_consolidation(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "id": "valid-topic",
                        "type": "project",
                        "description": "Would be valid",
                        "content": "This operation must not be partially applied.",
                    },
                    {"action": "rename", "id": "invalid-topic"},
                ]
            },
            expected_revision=revision,
        )

    assert store.find() == []


def test_project_memory_rejects_consolidation_of_omitted_existing_body(tmp_path):
    store = memory(tmp_path)
    original = store.remember("hidden-topic", "project", "Hidden from prompt", "Preserve this body.")

    with pytest.raises(ToolError, match="full bodies were omitted"):
        store.apply_consolidation(
            {"operations": [{"action": "forget", "id": original.id}]},
            expected_revision=store.revision(),
            allowed_existing_ids=frozenset(),
        )

    assert store.find(ids=[original.id]) == [original]


def test_project_memory_consolidation_lock_is_exclusive_across_instances(tmp_path):
    first = memory(tmp_path)
    second = ProjectMemory(first.directory)

    with first.consolidation_lock() as first_acquired, second.consolidation_lock() as second_acquired:
        assert first_acquired is True
        assert second_acquired is False

    with second.consolidation_lock() as acquired_after_release:
        assert acquired_after_release is True


def test_memory_tool_validates_action_specific_fields(tmp_path):
    session = Session(cwd=str(tmp_path), config=Config(data_dir=str(tmp_path / "data")))

    with pytest.raises(ToolError, match="Memory search requires query"):
        MemoryTool(session, [{"action": "search"}]).call()
    with pytest.raises(ToolError, match="Memory remember requires"):
        MemoryTool(session, [{"action": "remember", "id": "topic"}]).call()
    with pytest.raises(ToolError, match="Memory unexpected field"):
        MemoryTool(session, [{"action": "list", "content": "noise"}]).call()


def test_memory_context_is_stable_during_ordinary_turns_and_new_sessions_see_updates(tmp_path):
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


def test_successful_compaction_refreshes_frozen_memory_context(tmp_path):
    session = Session(cwd=str(tmp_path / "project"), config=Config(data_dir=str(tmp_path / "data")))
    assert session.memory is not None
    session.memory.remember("project-release", "project", "Old release policy", "Old policy")
    context = ContextManager(session)

    frozen = memory_context(context.model_messages("system"))
    assert "Old release policy" in frozen

    session.memory.remember("project-release", "project", "New release policy", "New policy")
    assert memory_context(context.model_messages("system")) == frozen

    session.messages = compactable_messages()
    compacted, keep = context.compaction_parts()
    context.apply_compaction({"summary": "compacted"}, keep, compacted=compacted)

    refreshed = memory_context(context.model_messages("system"))
    assert "New release policy" in refreshed
    assert "Old release policy" not in refreshed


def test_successful_compaction_loads_memory_created_after_empty_snapshot(tmp_path):
    session = Session(cwd=str(tmp_path / "project"), config=Config(data_dir=str(tmp_path / "data")))
    assert session.memory is not None
    context = ContextManager(session)

    assert memory_context(context.model_messages("system")) == ""
    session.memory.remember("user-response-style", "user", "User prefers concise answers", "Keep answers concise.")
    assert memory_context(context.model_messages("system")) == ""

    session.messages = compactable_messages()
    compacted, keep = context.compaction_parts()
    context.apply_compaction({"summary": "compacted"}, keep, compacted=compacted)

    assert "User prefers concise answers" in memory_context(context.model_messages("system"))


def test_compactor_failure_refreshes_memory_after_deterministic_trim(tmp_path):
    session = Session(cwd=str(tmp_path / "project"), config=Config(data_dir=str(tmp_path / "data")))
    assert session.memory is not None
    session.memory.remember("project-release", "project", "Old release policy", "Old policy")
    context = ContextManager(session)
    assert "Old release policy" in memory_context(context.model_messages("system"))

    session.memory.remember("project-release", "project", "New release policy", "New policy")
    session.messages = compactable_messages()
    session.usage.last_prompt_budget = 1
    session.usage.last_prompt_tokens = 1

    class FailingModel:
        def compact(self, _text):
            raise ModelError("failed")

    projected = context.prepare_messages(FailingModel(), "system", [{"role": "user", "content": "continue"}])

    refreshed = memory_context(projected)
    assert "New release policy" in refreshed
    assert "Old release policy" not in refreshed
    assert session.state.compaction_count == 1
