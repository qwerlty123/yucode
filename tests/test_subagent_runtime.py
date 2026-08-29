import threading

import pytest
from agent_harness import session

from yucode.base import ToolError
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


def test_fresh_and_fork_context_are_separate_and_nested_spawn_is_rejected(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.messages = [{"role": "user", "content": "父会话事实"}]
    contexts = []

    def execute(child, _prompt):
        contexts.append([message.get("content") for message in child.messages])
        return "ok"

    runtime = SubagentRuntime(root, executor=execute)
    fresh = runtime.spawn(AgentSpec("fresh", "执行", context="fresh"))
    forked = runtime.spawn(AgentSpec("fork", "执行", context="fork"))

    assert fresh.status == forked.status == "completed"
    assert contexts == [[], ["父会话事实"]]
    with pytest.raises(ToolError, match="不允许再启动"):
        runtime.spawn(AgentSpec("nested", "执行"), caller_task_id=fresh.task_id)
    runtime.close()


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


def test_resume_without_a_saved_transcript_fails_explicitly(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "完成")
    task = runtime.spawn(AgentSpec("未保存", "执行"))

    with pytest.raises(ToolError, match="transcript"):
        runtime.resume(task.task_id, "继续")
    runtime.close()
