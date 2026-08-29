import json
import threading
import time

import pytest
from agent_harness import call, session

from yucode.base import ModelRequestRetry, ToolError
from yucode.context import ContextManager
from yucode.engine import Agent
from yucode.runner import ToolRunner
from yucode.subagent import AgentSpec, SubagentRuntime
from yucode.tools import AgentTaskTool, AgentTool


def _wait_for_status(runtime, task_id, status, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = runtime.get(task_id)
        if task.status == status:
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务未进入 {status}: {runtime.get(task_id)}")


def test_agent_tool_fans_out_before_waiting_and_keeps_call_order(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    rendezvous = threading.Barrier(2)

    def execute(_child, prompt):
        rendezvous.wait(timeout=1)
        return "结果-" + prompt

    runtime = SubagentRuntime(root, executor=execute)
    runner = ToolRunner(root, ContextManager(root), output_fn=lambda _text: None)
    messages = runner.run(
        [
            call("Agent", [{"description": "甲", "prompt": "一"}]),
            call("Agent", [{"description": "乙", "prompt": "二"}]),
        ]
    )

    assert [message["tool_call_id"] for message in messages] == ["Agent-id", "Agent-id"]
    assert "结果-一" in messages[0]["content"]
    assert "结果-二" in messages[1]["content"]
    runtime.close()


def test_agent_tool_returns_background_task_and_agenttask_controls_it(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    release = threading.Event()
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: release.wait(timeout=2) or "完成")

    launched = json.loads(AgentTool(root, [{"description": "后台", "prompt": "执行", "run_in_background": True}]).call())
    listed = json.loads(AgentTaskTool(root, [{"action": "list"}]).call())
    stopped = json.loads(AgentTaskTool(root, [{"action": "stop", "task_id": launched["task_id"]}]).call())

    assert listed["tasks"][0]["task_id"] == launched["task_id"]
    assert stopped["status"] in {"running", "cancelled"}
    release.set()
    assert runtime.wait(launched["task_id"], timeout_seconds=2).status == "cancelled"
    runtime.close()


def test_nested_agent_tool_is_rejected_even_if_manually_constructed(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "完成")
    child = session(tmp_path / "child")
    child.subagents = runtime
    child.subagent_task_id = "伪造调用方"

    with pytest.raises(ToolError, match="不允许再启动"):
        AgentTool(child, [{"description": "嵌套", "prompt": "执行"}]).call()
    runtime.close()


def test_background_interaction_waits_for_matching_durable_response(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.subagent_interaction_available = True
    runtime = None

    def execute(child, _prompt):
        assert runtime is not None
        return runtime.interactions.request(
            child.subagent_task_id,
            "ask",
            {"tool_name": "Ask", "question": "继续吗？", "choices": ["继续", "停止"]},
        )

    runtime = SubagentRuntime(root, executor=execute)
    launched = runtime.spawn(AgentSpec("交互", "执行", run_in_background=True))
    waiting = _wait_for_status(runtime, launched.task_id, "waiting_interaction")
    request = waiting.pending_interaction

    with pytest.raises(ToolError, match="digest"):
        runtime.respond_interaction(launched.task_id, request["request_id"], "错误", "继续")
    runtime.respond_interaction(launched.task_id, request["request_id"], request["digest"], "继续")
    completed = runtime.wait(launched.task_id, timeout_seconds=2)

    assert completed.status == "completed"
    assert completed.result == "继续"
    assert completed.pending_interaction["status"] == "answered"
    runtime.close()


def test_background_interaction_fails_closed_without_a_user_channel(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = None

    def execute(child, _prompt):
        assert runtime is not None
        return runtime.interactions.request(
            child.subagent_task_id,
            "approval",
            {"tool_name": "Edit", "arguments": {"path": "a.txt"}, "preview": "diff"},
        )

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("无交互", "执行", run_in_background=True))
    completed = runtime.wait(task.task_id, timeout_seconds=2)

    assert completed.status == "completed"
    assert completed.result == "n"
    assert completed.pending_interaction["status"] == "answered"
    runtime.close()


def test_tool_runner_reads_live_root_yolo_for_child_authorization(tmp_path):
    child = session(tmp_path)
    root = session(tmp_path / "root")
    child.authorization_settings = root.settings
    root.settings.yolo = True
    runner = ToolRunner(child, ContextManager(child), output_fn=lambda _text: None)
    runner.confirmation_fn = lambda *_args: (_ for _ in ()).throw(AssertionError("yolo 不应进入确认"))

    result = runner.run([call("Edit", ["approved.txt", [{"op": "create", "content": "完成\n"}]])])

    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "完成\n"
    assert "status: failed" not in result[0]["content"]


def test_completion_notification_is_injected_until_model_accepts_it(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "后台结果")
    task = runtime.spawn(AgentSpec("后台", "执行", run_in_background=True))
    runtime.wait(task.task_id, timeout_seconds=2)
    agent = Agent(root, output_fn=lambda _text: None)

    class RetryOnce:
        def __init__(self):
            self.calls = 0
            self.messages = []

        def request(self, messages, tools=None):
            self.calls += 1
            self.messages.append(messages)
            if self.calls == 1:
                raise ModelRequestRetry
            return {"role": "assistant", "content": "收到"}, [], "收到"

    agent.model = RetryOnce()

    assert agent.run("检查后台任务") == "收到"
    assert all(any("SUBAGENT COMPLETION" in str(message.get("content") or "") for message in sent) for sent in agent.model.messages)
    assert runtime.get(task.task_id).delivery_state == "consumed"
    assert runtime.claim_notifications() == []
    runtime.close()
