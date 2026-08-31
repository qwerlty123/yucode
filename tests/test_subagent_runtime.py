import os
import threading
import time

import pytest
from agent_harness import session
from PIL import Image

from yucode.base import Config, ProviderConfig, ToolError
from yucode.context import ContextManager
from yucode.engine import Agent
from yucode.session import HistorySegment
from yucode.skill import Skill, SkillLibrary
from yucode.subagent import AgentSpec, SubagentRuntime


def test_background_subagent_runs_behind_runtime_interface_and_is_restorable(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    seen = []

    def execute(child, prompt):
        seen.append((child is root, child.cwd, prompt, child.tool_catalog.names))
        return "已完成: " + prompt

    runtime = SubagentRuntime(root, executor=execute)
    launched = runtime.spawn(AgentSpec("检查实现", "核对工具边界", run_in_background=True))
    completed = runtime.wait(launched.task_id, timeout_seconds=2)

    assert launched.status in {"queued", "running", "completed"}
    assert completed.status == "completed"
    assert completed.result == "已完成: 核对工具边界"
    assert seen and seen[0][:3] == (False, str(tmp_path), "核对工具边界")
    assert "Memory" not in seen[0][3]
    assert "NextHints" not in seen[0][3]

    restored = SubagentRuntime(root, executor=execute)
    assert restored.get(completed.task_id).result == completed.result
    runtime.close()
    restored.close()


def test_spawn_freezes_skill_library_across_persistence_and_resume(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.skills = SkillLibrary({"guide": Skill("guide", "指南", "第一版", str(tmp_path), "project")})
    seen = []

    def execute(child, _prompt):
        assert child.skills is not None
        seen.append(child.skills.get("guide").body)
        child.messages.append({"role": "assistant", "content": "检查点"})
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.spawn(AgentSpec("技能快照", "执行"))
    runtime.close()
    root.skills.skills["guide"] = Skill("guide", "指南", "第二版", str(tmp_path), "project")
    restored = SubagentRuntime(root, executor=execute)
    second = restored.resume(first.task_id, "继续")

    assert second.status == "completed"
    assert seen == ["第一版", "第一版"]
    restored.close()


def test_empty_skill_snapshot_does_not_auto_load_skills_added_while_queued(tmp_path, monkeypatch):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.skills = SkillLibrary({})
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "不执行")
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", lambda _thread: None)
        launched = runtime.start(AgentSpec("空技能", "执行", subagent_type="explore", run_in_background=True))
    root.skills.skills["later"] = Skill("later", "后加", "新技能", str(tmp_path), "project")

    child = runtime._child_session(runtime._tasks[launched.task_id])

    assert child.skills is not None
    assert child.skills.all() == []
    runtime.close()


def test_child_uses_a_persisted_read_only_memory_context_snapshot(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"

    class Memory:
        value = "第一版 memory"
        resets = 0

        def context(self):
            return self.value

        def reset_context(self):
            self.resets += 1

    memory = Memory()
    root.memory = memory
    seen = []

    def execute(child, _prompt):
        seen.append((child.memory, ContextManager(child).memory_context()))
        child.messages.append({"role": "assistant", "content": "检查点"})
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.spawn(AgentSpec("memory 快照", "执行"))
    runtime.close()
    memory.value = "第二版 memory"
    restored = SubagentRuntime(root, executor=execute)
    restored.resume(first.task_id, "继续")

    assert seen == [(None, "第一版 memory"), (None, "第一版 memory")]
    assert memory.resets == 0
    restored.close()


def test_task_store_recovers_from_meta_when_registry_tail_is_damaged(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "持久结果")
    completed = runtime.spawn(AgentSpec("恢复", "执行"))
    registry = runtime.store.registry_path
    runtime.close()
    with open(registry, "w", encoding="utf-8") as handle:
        handle.write('{"task_id":"broken"')

    restored = SubagentRuntime(root, executor=lambda _child, _prompt: "不应执行")

    assert restored.get(completed.task_id).result == "持久结果"
    assert restored.get(completed.task_id).status == "completed"
    restored.close()


def test_fresh_and_fork_context_are_separate_and_nested_spawn_is_rejected(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.messages = [{"role": "user", "content": "父会话事实"}]
    root._active_turn_messages = [
        {"role": "user", "content": "当前委派要求"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "dangling", "function": {"name": "Agent", "arguments": "{}"}}]},
    ]
    contexts = []

    def execute(child, _prompt):
        contexts.append(([message.get("content") for message in child.messages], child.system_prompt))
        return "ok"

    runtime = SubagentRuntime(root, executor=execute)
    fresh = runtime.spawn(AgentSpec("fresh", "执行", context="fresh"))
    forked = runtime.spawn(AgentSpec("fork", "执行", context="fork"))

    assert fresh.status == forked.status == "completed"
    assert contexts[0][0] == []
    assert "Fork context" not in contexts[0][1]
    assert contexts[1][0] == ["父会话事实", "当前委派要求"]
    assert "conversation above is a frozen snapshot" in contexts[1][1]
    assert "detached from the parent" in contexts[1][1]
    with pytest.raises(ToolError, match="不允许再启动"):
        runtime.spawn(AgentSpec("nested", "执行"), caller_task_id=fresh.task_id)
    runtime.close()


def test_fork_context_can_read_parent_session_image_assets(tmp_path):
    image_path = tmp_path / "parent.png"
    Image.new("RGB", (2, 2), (10, 20, 30)).save(image_path)
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.config.provider.image_input = "on"
    root.messages = [root.images.message(root.images.recognize("查看 parent.png"))]
    parent_asset = root.images._asset_path(root.images.refs(root.messages[0])[0])
    seen = []

    def execute(child, _prompt):
        image = child.images.refs(child.messages[0])[0]
        os.unlink(parent_asset)  # child 建立后删除父资产，验证它已有独立副本。
        seen.append(child.images._bytes(image))
        return "已读取"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("图片上下文", "执行", context="fork"))

    assert task.status == "completed"
    assert seen and seen[0].startswith(b"\x89PNG")
    runtime.close()


def test_fork_copies_recall_history_and_notes_then_detaches_them(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    key = root.store_tool_result("Read", [{"path": "a.txt"}], "父工具结果")
    root.history.append(HistorySegment("seg.1", "父历史", "父历史正文"))
    root.state.goal = "父目标"
    seen = []

    def execute(child, _prompt):
        seen.append((child.tool_results[key], child.history[0].text, child.state.goal, child.tool_counter))
        child.tool_results[key] = "子修改"
        child.history[0].text = "子历史"
        child.state.goal = "子目标"
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("完整快照", "执行", context="fork"))

    assert task.status == "completed"
    assert seen == [("父工具结果", "父历史正文", "父目标", 1)]
    assert root.tool_results[key] == "父工具结果"
    assert root.history[0].text == "父历史正文"
    assert root.state.goal == "父目标"
    runtime.close()


def test_context_filter_removes_incomplete_tool_pairs_without_dropping_later_messages():
    messages = [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "tool_calls": [{"id": "ok-1"}, {"id": "ok-2"}]},
        {"role": "tool", "tool_call_id": "ok-2", "content": "二"},
        {"role": "tool", "tool_call_id": "ok-1", "content": "一"},
        {"role": "tool", "tool_call_id": "orphan", "content": "孤立"},
        {"role": "assistant", "tool_calls": [{"id": "missing"}, {"id": "present"}]},
        {"role": "tool", "tool_call_id": "present", "content": "不完整"},
        {"role": "user", "content": "后续合法消息"},
    ]

    filtered = SubagentRuntime._complete_messages(messages)

    assert [message.get("content") for message in filtered] == ["开始", None, "二", "一", "后续合法消息"]


def test_resume_reuses_child_transcript_but_creates_a_new_attempt(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    seen = []

    def execute(child, prompt):
        seen.append(([message.get("content") for message in child.messages], prompt))
        child.messages.append({"role": "assistant", "content": "记录-" + prompt})
        child.save_snapshot()
        return "结果-" + prompt

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.spawn(AgentSpec("可恢复", "第一次"))
    second = runtime.resume(first.task_id, "第二次")

    assert first.attempt == 1
    assert second.attempt == 2
    assert second.status == "completed"
    assert seen[0] == ([], "第一次")
    assert "记录-第一次" in seen[1][0]
    assert seen[1][1] == "第二次"
    runtime.close()


def test_resume_replays_durable_steer_before_the_new_resume_message(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    started = threading.Event()
    release = threading.Event()

    def first_execute(child, _prompt):
        child.messages.append({"role": "assistant", "content": "旧检查点"})
        started.set()
        release.wait(timeout=2)
        raise KeyboardInterrupt

    runtime = SubagentRuntime(root, executor=first_execute)
    task = runtime.start(AgentSpec("持久 steer", "第一次", run_in_background=True))
    assert started.wait(timeout=1)
    runtime.steer(task.task_id, "旧 attempt 的追加要求")
    runtime.stop(task.task_id)
    release.set()
    assert runtime.wait(task.task_id, 2).status == "cancelled"
    seen = []

    def resumed_execute(child, prompt):
        seen.append(([str(message.get("content") or "") for message in child.messages], prompt, list(child.pending_user_inputs)))
        return "完成"

    runtime._executor = resumed_execute
    launched = runtime.resume(task.task_id, "新 resume 要求")
    resumed = runtime.wait(launched.task_id, 2)

    assert resumed.status == "completed"
    assert any("旧 attempt 的追加要求" in content for content in seen[0][0])
    assert seen[0][1:] == ("新 resume 要求", [])
    runtime.close()


def test_resume_obeys_queue_capacity_and_records_worker_start_failure(tmp_path, monkeypatch):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    started = threading.Event()
    release = threading.Event()

    def execute(child, prompt):
        if prompt == "占用并发槽":
            started.set()
            release.wait(timeout=2)
        child.messages.append({"role": "assistant", "content": "已保存"})
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    resumable = runtime.spawn(AgentSpec("可恢复", "第一次"))
    root.settings.max_parallel_agents = 1
    root.settings.max_queued_agents = 0
    running = runtime.start(AgentSpec("占用", "占用并发槽", run_in_background=True))
    assert started.wait(timeout=1)

    with pytest.raises(ToolError, match="队列已满"):
        runtime.resume(resumable.task_id, "第二次")

    release.set()
    assert runtime.wait(running.task_id, 2).status == "completed"
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", lambda _thread: (_ for _ in ()).throw(RuntimeError("无法启动")))
        failed = runtime.resume(resumable.task_id, "第二次")

    assert failed.attempt == 2
    assert failed.status == "failed"
    assert failed.stop_reason == "worker-start"
    assert failed.error == "无法启动"
    runtime.close()


def test_background_notification_is_claimed_and_acknowledged_once(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "后台结果")
    task = runtime.spawn(AgentSpec("后台", "执行", run_in_background=True))
    runtime.wait(task.task_id, timeout_seconds=2)

    first = runtime.claim_notifications()
    repeated = runtime.claim_notifications()

    assert [item.task_id for item in first] == [task.task_id]
    assert [item.task_id for item in repeated] == [task.task_id]
    assert runtime.get(task.task_id).delivery_state == "delivered"
    runtime.acknowledge_notifications([task.task_id])
    assert runtime.claim_notifications() == []
    assert runtime.get(task.task_id).delivery_state == "consumed"
    runtime.close()


def test_stop_is_idempotent_and_cancels_a_running_subagent(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    started = threading.Event()
    release = threading.Event()

    def execute(_child, _prompt):
        started.set()
        release.wait(timeout=2)
        raise KeyboardInterrupt

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("等待", "执行", run_in_background=True))
    assert started.wait(timeout=1)

    first = runtime.stop(task.task_id)
    second = runtime.stop(task.task_id)
    release.set()
    completed = runtime.wait(task.task_id, timeout_seconds=2)

    assert first.status in {"running", "cancelled"}
    assert second.status in {"running", "cancelled"}
    assert completed.status == "cancelled"
    assert completed.stop_reason == "stopped"
    runtime.close()


def test_detach_releases_foreground_wait_but_keeps_the_worker_running(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    started = threading.Event()
    release = threading.Event()

    def execute(_child, _prompt):
        started.set()
        release.wait(timeout=2)
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.start(AgentSpec("前台", "执行"))
    assert started.wait(timeout=1)

    detached = runtime.detach(task.task_id)
    returned = runtime.wait_foreground(task.task_id, timeout_seconds=1)

    assert detached.run_in_background is True
    assert returned.run_in_background is True
    assert returned.status == "running"
    release.set()
    assert runtime.wait(task.task_id, timeout_seconds=2).status == "completed"
    runtime.close()


def test_parallel_limit_uses_current_runtime_setting(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.settings.max_parallel_agents = 1
    started = [threading.Event(), threading.Event()]
    release = threading.Event()
    calls = []

    def execute(_child, prompt):
        index = int(prompt)
        calls.append(index)
        started[index].set()
        release.wait(timeout=2)
        return prompt

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.start(AgentSpec("first", "0", subagent_type="explore", run_in_background=True))
    second = runtime.start(AgentSpec("second", "1", subagent_type="explore", run_in_background=True))
    assert started[0].wait(timeout=1)
    assert not started[1].wait(timeout=0.2)

    root.settings.max_parallel_agents = 2
    assert started[1].wait(timeout=1)
    release.set()
    assert runtime.wait(first.task_id, 2).status == "completed"
    assert runtime.wait(second.task_id, 2).status == "completed"
    assert sorted(calls) == [0, 1]
    runtime.close()


def test_wait_change_returns_when_runtime_publishes_new_activity(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    started = threading.Event()
    release = threading.Event()

    def execute(_child, _prompt):
        started.set()
        release.wait(timeout=2)
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.start(AgentSpec("进度", "执行", run_in_background=True))
    assert started.wait(timeout=1)
    before = runtime.get(task.task_id).revision
    observed = []
    waiter = threading.Thread(target=lambda: observed.append(runtime.wait_change(task.task_id, 1)))
    waiter.start()
    time.sleep(0.02)

    runtime._activity(runtime._tasks[task.task_id], "正在检查")
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert observed[0].revision > before
    assert observed[0].partial_result == "正在检查"
    assert runtime.details(task.task_id)["recent_activity"][-1]["text"] == "正在检查"
    release.set()
    runtime.wait(task.task_id, 2)
    runtime.close()


def test_stopping_queued_worktree_task_does_not_create_workspace(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.settings.max_parallel_agents = 1
    started = threading.Event()
    release = threading.Event()

    def execute(_child, prompt):
        if prompt == "hold":
            started.set()
            release.wait(timeout=2)
        return prompt

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.start(AgentSpec("first", "hold", run_in_background=True))
    assert started.wait(timeout=1)
    prepared = []
    runtime.worktrees.prepare = lambda task_id, previous=None: prepared.append(task_id) or {}
    queued = runtime.start(AgentSpec("queued", "never", run_in_background=True, isolation="worktree"))

    runtime.stop(queued.task_id)
    release.set()

    assert runtime.wait(queued.task_id, 2).status == "cancelled"
    assert runtime.wait(first.task_id, 2).status == "completed"
    queued_thread = runtime._tasks[queued.task_id].thread
    assert queued_thread is not None
    queued_thread.join(timeout=1)
    assert not queued_thread.is_alive()
    assert prepared == []
    runtime.close()


def test_child_uses_and_closes_its_own_mcp_manager(tmp_path, monkeypatch):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    events = []

    monkeypatch.setattr("yucode.mcp.MCPManager.discover_auto", lambda manager: events.append(("discover", manager.session.uid)))
    monkeypatch.setattr("yucode.mcp.MCPManager.close", lambda manager: events.append(("close", manager.session.uid)))
    runtime = SubagentRuntime(root, executor=lambda child, _prompt: "独立" if child.mcp is not root.mcp else "共享")

    task = runtime.spawn(AgentSpec("MCP", "执行"))

    assert task.status == "completed"
    assert task.result == "独立"
    assert events == [("discover", task.task_id), ("close", task.task_id)]
    runtime.close()


def test_worktree_child_keeps_the_root_resolved_data_directory(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.config.data_dir = "relative-data"
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    seen = []
    runtime = SubagentRuntime(root, executor=lambda child, _prompt: seen.append(child.data_path()) or "完成")
    assert runtime.store.directory.startswith(str(tmp_path / "relative-data"))
    runtime.worktrees.prepare = lambda _task_id, _previous=None: {
        "mode": "worktree",
        "path": str(isolated),
        "branch": "task-branch",
        "base_commit": "base",
        "cleanup_state": "active",
    }
    runtime.worktrees.finalize = lambda workspace: ({**workspace, "cleanup_state": "retained"}, [], [])

    task = runtime.spawn(AgentSpec("数据目录", "执行", isolation="worktree"))

    assert task.status == "completed"
    assert seen == [str(tmp_path / "relative-data")]
    runtime.close()


def test_stop_during_child_discovery_never_enters_executor(tmp_path, monkeypatch):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    discovering = threading.Event()
    release = threading.Event()
    executed = []

    def discover(_manager):
        discovering.set()
        release.wait(timeout=2)

    monkeypatch.setattr("yucode.mcp.MCPManager.discover_auto", discover)
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: executed.append(True) or "不应执行")
    task = runtime.start(AgentSpec("发现期间停止", "执行", run_in_background=True))
    assert discovering.wait(timeout=1)

    runtime.stop(task.task_id)
    release.set()
    completed = runtime.wait(task.task_id, 2)

    assert completed.status == "cancelled"
    assert executed == []
    runtime.close()


def test_child_run_outcome_preserves_cancellation_requested_before_start(tmp_path):
    child = session(tmp_path)
    child.subagent_task_id = "agent-cancelled"
    child.cancellation_event = threading.Event()
    child.cancellation_event.set()
    agent = Agent(child, output_fn=lambda _text: None)

    class Model:
        def request(self, _messages, _tools=None):
            raise AssertionError("取消后的 child 不应发起模型请求")

        def cancel(self):
            return None

    agent.model = Model()

    outcome = agent.run_outcome("执行")

    assert outcome.status == "cancelled"
    assert outcome.stop_reason == "cancelled"


def test_shutdown_is_bounded_and_marks_unresponsive_worker_interrupted(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.settings.agent_shutdown_grace_seconds = 0
    started = threading.Event()
    release = threading.Event()
    killed = threading.Event()
    mcp_closed = threading.Event()
    cancelling = threading.Event()
    release_cancel = threading.Event()

    class Job:
        def kill(self):
            killed.set()

    def execute(child, _prompt):
        child.jobs["job-1"] = Job()
        child.mcp.close = mcp_closed.set
        started.set()
        release.wait(timeout=2)
        return "迟到结果"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.start(AgentSpec("退出", "执行", run_in_background=True))
    assert started.wait(timeout=1)

    class BlockingAgent:
        def cancel(self):
            cancelling.set()
            release_cancel.wait(timeout=2)

    runtime._tasks[task.task_id].agent = BlockingAgent()

    before = time.monotonic()
    runtime.close()
    elapsed = time.monotonic() - before

    interrupted = runtime.get(task.task_id)
    assert elapsed < 0.5
    assert interrupted.status == "interrupted"
    assert interrupted.stop_reason == "shutdown-timeout"
    assert killed.wait(timeout=0.5)
    assert mcp_closed.wait(timeout=0.5)
    assert cancelling.wait(timeout=0.5)
    release_cancel.set()
    release.set()
    thread = runtime._tasks[task.task_id].thread
    assert thread is not None
    thread.join(timeout=1)


def test_late_workspace_finalize_updates_terminal_recovery_metadata(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.settings.agent_shutdown_grace_seconds = 0
    finalizing = threading.Event()
    release = threading.Event()
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "完成")
    runtime.worktrees.prepare = lambda _task_id, _previous=None: {
        "mode": "worktree",
        "path": str(tmp_path / "worktree"),
        "branch": "task-branch",
        "base_commit": "base",
        "cleanup_state": "active",
    }

    def finalize(workspace):
        finalizing.set()
        release.wait(timeout=2)
        return ({**workspace, "cleanup_state": "cleaned"}, [], [])

    runtime.worktrees.finalize = finalize
    task = runtime.start(AgentSpec("收口竞态", "执行", isolation="worktree", run_in_background=True))
    assert finalizing.wait(timeout=1)

    runtime.close()
    assert runtime.get(task.task_id).workspace["cleanup_state"] == "retained"
    release.set()
    thread = runtime._tasks[task.task_id].thread
    assert thread is not None
    thread.join(timeout=1)

    restored = SubagentRuntime(root, executor=lambda _child, _prompt: "不执行")
    assert restored.get(task.task_id).workspace["cleanup_state"] == "cleaned"
    restored.close()


def test_late_worktree_creation_after_shutdown_is_persisted_as_retained(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.settings.agent_shutdown_grace_seconds = 0
    preparing = threading.Event()
    release = threading.Event()
    path = tmp_path / "late-worktree"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "不应执行")

    def prepare(_task_id, _previous=None):
        preparing.set()
        release.wait(timeout=2)
        return {
            "mode": "worktree",
            "path": str(path),
            "branch": "late-branch",
            "base_commit": "base",
            "cleanup_state": "active",
        }

    runtime.worktrees.prepare = prepare
    task = runtime.start(AgentSpec("延迟创建", "执行", isolation="worktree", run_in_background=True))
    assert preparing.wait(timeout=1)

    runtime.close()
    assert runtime.get(task.task_id).workspace == {}
    release.set()
    thread = runtime._tasks[task.task_id].thread
    assert thread is not None
    thread.join(timeout=1)

    restored = SubagentRuntime(root, executor=lambda _child, _prompt: "不执行")
    recovered = restored.get(task.task_id)
    assert recovered.workspace["path"] == str(path)
    assert recovered.workspace["cleanup_state"] == "retained"
    restored.close()


def test_attempt_cleanup_kills_child_jobs(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    killed = []

    class Job:
        def kill(self):
            killed.append(True)

    def execute(child, _prompt):
        child.jobs["job-1"] = Job()
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("资源", "执行"))

    assert task.status == "completed"
    assert killed == [True]
    runtime.close()


@pytest.mark.parametrize("api", ["chat", "responses", "anthropic"])
def test_child_keeps_provider_credentials_and_allows_only_its_models(tmp_path, api):
    root = session(tmp_path)
    root.config = Config(
        active_provider="current",
        providers={
            "current": ProviderConfig(
                url="https://current.example/v1",
                key="current-key",
                model="model-a",
                api=api,
                available_models=("model-b",),
            ),
            "other": ProviderConfig(url="https://other.example/v1", key="other-key", model="other-model"),
        },
        data_dir=str(tmp_path / "data"),
    )
    seen = []

    def execute(child, _prompt):
        seen.append(
            (child.config.active_provider, child.config.provider.url, child.config.provider.key, child.config.provider.model, child.config.provider.api)
        )
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    inherited = runtime.spawn(AgentSpec("继承模型", "执行"))
    task = runtime.spawn(AgentSpec("模型", "执行", model="model-b"))

    assert inherited.status == "completed"
    assert task.status == "completed"
    assert seen == [
        ("current", "https://current.example/v1", "current-key", "model-a", api),
        ("current", "https://current.example/v1", "current-key", "model-b", api),
    ]
    with pytest.raises(ToolError, match="当前 provider 不提供模型"):
        runtime.start(AgentSpec("越界", "执行", model="other-model"))
    runtime.close()


def test_resumed_task_keeps_the_provider_selected_at_launch(tmp_path):
    root = session(tmp_path)
    root.config = Config(
        active_provider="first",
        providers={
            "first": ProviderConfig(url="https://first.example/v1", key="first-key", model="model-a"),
            "second": ProviderConfig(url="https://second.example/v1", key="second-key", model="model-b"),
        },
        data_dir=str(tmp_path / "data"),
    )
    seen = []

    def execute(child, _prompt):
        seen.append((child.config.active_provider, child.config.provider.url, child.config.provider.key))
        child.messages.append({"role": "assistant", "content": "检查点"})
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.spawn(AgentSpec("provider 快照", "执行"))
    meta = runtime.store.directory + "/" + first.task_id + ".meta.json"
    with open(meta, encoding="utf-8") as handle:
        assert "first-key" not in handle.read()
    runtime.close()
    root.config.active_provider = "second"
    restored = SubagentRuntime(root, executor=execute)
    resumed = restored.resume(first.task_id, "继续")

    assert resumed.status == "completed"
    assert seen == [
        ("first", "https://first.example/v1", "first-key"),
        ("first", "https://first.example/v1", "first-key"),
    ]
    restored.close()
    root.config.providers["first"].url = "https://changed.example/v1"
    drifted = SubagentRuntime(root, executor=execute)
    failed = drifted.resume(first.task_id, "再继续")

    assert failed.status == "failed"
    assert "API 或 URL 已变更" in failed.error
    drifted.close()


def test_resume_without_a_saved_transcript_fails_explicitly(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "完成")
    task = runtime.spawn(AgentSpec("未保存", "执行"))

    with pytest.raises(ToolError, match="transcript"):
        runtime.resume(task.task_id, "继续")
    runtime.close()


def test_restart_can_resume_a_never_started_fresh_task_without_transcript(tmp_path, monkeypatch):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "旧进程不应执行")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", lambda _thread: None)
        launched = runtime.start(AgentSpec("排队", "原始任务契约", subagent_type="explore", run_in_background=True))

    recovered = SubagentRuntime(root, executor=lambda _child, prompt: "恢复:" + prompt)
    interrupted = recovered.get(launched.task_id)
    resumed = recovered.resume(launched.task_id, "继续执行")
    completed = recovered.wait(resumed.task_id, 2)

    assert interrupted.status == "interrupted"
    assert interrupted.started_at == ""
    assert completed.status == "completed"
    assert completed.attempt == 2
    assert completed.result == "恢复:原始任务契约\n\n追加要求：继续执行"
    recovered.close()


def test_restart_marks_pending_interaction_invalid_instead_of_losing_it(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.subagent_interaction_available = True
    runtime = None

    def execute(child, _prompt):
        assert runtime is not None
        return runtime.interactions.request(child.subagent_task_id, "ask", {"question": "等待回答"})

    runtime = SubagentRuntime(root, executor=execute)
    launched = runtime.spawn(AgentSpec("等待", "执行", run_in_background=True))
    deadline = time.monotonic() + 2
    while runtime.get(launched.task_id).status != "waiting_interaction" and time.monotonic() < deadline:
        time.sleep(0.01)

    recovered = SubagentRuntime(root, executor=execute)
    task = recovered.get(launched.task_id)

    assert task.status == "interrupted"
    assert task.stop_reason == "restart"
    assert task.pending_interaction["status"] == "invalid"
    assert task.pending_interaction["invalid_reason"] == "restart"
    with pytest.raises(ToolError, match="已失效"):
        recovered.respond_interaction(task.task_id, task.pending_interaction["request_id"], task.pending_interaction["digest"], "继续")
    runtime.stop(launched.task_id)
    runtime.close()
    recovered.close()
