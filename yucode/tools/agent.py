"""根 Agent 用于委派任务和控制子 Agent 生命周期的工具。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from yucode.base import Json, ToolError
from yucode.tools.base import Tool

if TYPE_CHECKING:
    from yucode.subagent import AgentSpec, AgentTaskView, SubagentRuntime


def runtime_for(session) -> SubagentRuntime:
    """返回根会话唯一运行时，并在任何子会话身份下拒绝控制入口。"""

    if session.subagent_task_id:
        raise ToolError("子 Agent 不允许再启动或控制子 Agent")
    if session.subagents is None:
        from yucode.subagent import SubagentRuntime

        SubagentRuntime(session)
    assert session.subagents is not None
    return session.subagents


class AgentTool(Tool):
    NAME = "Agent"
    DESCRIPTION = (
        "Delegate a complete, independently verifiable task to a local sub-agent. "
        "Use fresh context by default; choose worktree only when isolated changes are needed. "
        "Multiple Agent calls in one response are launched together before foreground results are collected."
    )
    PRODUCES_MODEL_OBSERVATION = True

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        return cls.object_schema({
            "description": {"type": "string", "description": "Short task name"},
            "prompt": {"type": "string", "description": "Complete task contract, constraints, and acceptance requirements"},
            "subagent_type": {"type": "string", "description": "Agent profile name; default general-purpose"},
            "model": {"type": "string", "description": "Model from the current provider; default inherit"},
            "run_in_background": {"type": "boolean", "description": "Return immediately with task_id"},
            "isolation": {"type": "string", "enum": ["shared", "worktree"], "description": "Workspace mode"},
            "context": {"type": "string", "enum": ["fresh", "fork"], "description": "Parent conversation inheritance"},
        }, ["description", "prompt"])
        # fmt: on

    @classmethod
    def session_schema(cls, session, strict: bool = False) -> Json:
        """把当前会话真正可启动的 profile 固化进工具契约。"""

        from yucode.agent_profile import AgentProfileLibrary

        runtime = session.subagents
        library = runtime.profiles if runtime is not None else AgentProfileLibrary.load(session)
        profiles = [profile for profile in library.all() if profile.valid]
        schema = cls.schema(False)
        parameters = schema["function"]["parameters"]
        profile_schema = parameters["properties"]["subagent_type"]
        model_schema = parameters["properties"]["model"]
        if profiles:
            profile_schema["enum"] = [profile.name for profile in profiles]
            choices = "; ".join(
                f"{profile.name}: {profile.description} ({'background' if profile.background else 'foreground'}, {profile.isolation}, {profile.context}, model={profile.model})"
                for profile in profiles
            )
            profile_schema["description"] = "Agent profile; default general-purpose. Available: " + choices
        models = list(dict.fromkeys(filter(None, ("inherit", session.config.provider.model, *session.config.provider.available_models))))
        model_schema["enum"] = models
        model_schema["description"] = "Model from the current provider; default inherit. Available: " + ", ".join(models)
        if strict and cls._strictifiable(parameters):
            schema["function"]["parameters"] = cls._strict_schema(parameters)
            schema["function"]["strict"] = True
        return schema

    def request(self) -> AgentSpec:
        from yucode.subagent import AgentSpec

        payload = self.single_dict_arg("Agent 需要命名参数")
        allowed = {"description", "prompt", "subagent_type", "model", "run_in_background", "isolation", "context"}
        if unexpected := sorted(set(payload) - allowed):
            raise ToolError("Agent unexpected field: " + ", ".join(unexpected))
        background = payload.get("run_in_background")
        if background is not None and not isinstance(background, bool):
            raise ToolError("Agent run_in_background 必须是布尔值")
        return AgentSpec(
            description=str(payload.get("description") or ""),
            prompt=str(payload.get("prompt") or ""),
            subagent_type=str(payload.get("subagent_type") or "general-purpose"),
            model=str(payload["model"]) if payload.get("model") is not None else None,
            run_in_background=background,
            isolation=str(payload["isolation"]) if payload.get("isolation") is not None else None,
            context=str(payload["context"]) if payload.get("context") is not None else None,
        )

    def launch(self) -> AgentTaskView:
        return runtime_for(self.session).start(self.request(), caller_task_id=self.session.subagent_task_id or None)

    def collect(self, launched: AgentTaskView) -> AgentTaskView:
        if launched.run_in_background:
            return launched
        return runtime_for(self.session).wait_foreground(launched.task_id)

    @staticmethod
    def render(task: AgentTaskView) -> str:
        return json.dumps(task.to_json(), ensure_ascii=False, sort_keys=True)

    def call(self) -> str:
        return self.render(self.collect(self.launch()))

    def short_args(self) -> list[str]:
        payload = self.args[0] if self.args and isinstance(self.args[0], dict) else {}
        return [str(payload.get("description") or "")]


class AgentTaskTool(Tool):
    NAME = "AgentTask"
    DESCRIPTION = "List, inspect, wait for, steer, stop, resume, or close a root Agent's local sub-agent tasks."
    PRODUCES_MODEL_OBSERVATION = True
    STORES_RESULT = False

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        return cls.object_schema({
            "action": {"type": "string", "enum": ["list", "get", "wait", "steer", "stop", "resume", "close"], "description": "Task operation"},
            "task_id": {"type": "string", "description": "Task id; stop also accepts all"},
            "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 120, "description": "Maximum wait duration"},
            "message": {"type": "string", "description": "Message for steer or resume"},
        }, ["action"])
        # fmt: on

    def call(self) -> str:
        payload = self.single_dict_arg("AgentTask 需要命名参数")
        if unexpected := sorted(set(payload) - {"action", "task_id", "timeout_seconds", "message"}):
            raise ToolError("AgentTask unexpected field: " + ", ".join(unexpected))
        action = str(payload.get("action") or "").strip().lower()
        runtime = runtime_for(self.session)
        if action == "list":
            return json.dumps({"tasks": [task.to_json() for task in runtime.list()]}, ensure_ascii=False, sort_keys=True)
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise ToolError(f"AgentTask {action or '(empty)'} 需要 task_id")
        if action == "get":
            return json.dumps(runtime.details(task_id), ensure_ascii=False, sort_keys=True)
        elif action == "wait":
            raw_timeout = payload.get("timeout_seconds", 120)
            if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
                raise ToolError("AgentTask timeout_seconds 必须是数字")
            task = runtime.wait_change(task_id, float(raw_timeout))
        elif action == "steer":
            task = runtime.steer(task_id, str(payload.get("message") or ""))
        elif action == "stop":
            if task_id == "all":
                return json.dumps({"tasks": [item.to_json() for item in runtime.stop_all()]}, ensure_ascii=False, sort_keys=True)
            task = runtime.stop(task_id)
        elif action == "resume":
            task = runtime.resume(task_id, str(payload.get("message") or ""))
        elif action == "close":
            task = runtime.close_task(task_id)
        else:
            raise ToolError("AgentTask action 必须是 list、get、wait、steer、stop、resume 或 close")
        return AgentTool.render(task)

    def short_args(self) -> list[str]:
        payload = self.args[0] if self.args and isinstance(self.args[0], dict) else {}
        return [" ".join(filter(None, (str(payload.get("action") or ""), str(payload.get("task_id") or ""))))]
