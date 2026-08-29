"""Headless yucode worker used by the evaluation runner.

The worker accepts one JSON object on stdin. Provider credentials therefore do
not need to be placed in a task container's command line, environment, or
workspace. It writes only non-secret metrics and the yucode session transcript.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from yucode.base import Config, RuntimeSettings, ToolCall
from yucode.engine import Agent
from yucode.image import IMAGE_MARKER, UserInput
from yucode.model import PreparedRequest
from yucode.runner import ToolDisplay, ToolRunner
from yucode.session import Session, SessionSnapshotStore
from yucode.skill import SkillLibrary
from yucode.tools import TOOL_REGISTRY
from yucode.tools.search import CodeIndex

from .trajectory import ScriptedInteractionController, TrajectoryRecorder, scenario_from_dict


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _prepare_attachments(workspace: Path, raw_attachments: Any) -> tuple[str, ...]:
    if not isinstance(raw_attachments, list) or any(not isinstance(item, str) or not item for item in raw_attachments):
        raise TypeError("attachments must be an array of paths")
    prepared: list[str] = []
    for value in raw_attachments:
        source = (workspace / value).resolve()
        try:
            source.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"attachment escapes workspace: {value}") from exc
        if not source.is_file():
            raise FileNotFoundError(f"attachment does not exist: {value}")
        target = source
        if source.suffix == ".b64":
            target = source.with_suffix("")
            try:
                encoded = "".join(source.read_text(encoding="ascii").split())
                target.write_bytes(base64.b64decode(encoded, validate=True))
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid base64 attachment: {value}") from exc
        prepared.append(str(target.relative_to(workspace)))
    return tuple(prepared)


class EvaluationToolRunner(ToolRunner):
    def __init__(
        self,
        *args: Any,
        allowed_tools: frozenset[str] | None,
        recorder: TrajectoryRecorder,
        interactions: ScriptedInteractionController,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.allowed_tools = allowed_tools
        self.recorder = recorder
        self.interactions = interactions
        self._interaction_call: ToolCall | None = None
        self._current_model_call_id = ""
        self._approval_by_call: dict[str, str] = {}
        self.question_fn = self._question

    def _question(self, spec: Any, _position: str) -> str:
        call = self._interaction_call
        return self.interactions.question(
            self._current_model_call_id,
            call.id if call is not None else "",
            str(spec.question),
        )

    def confirm(self, call: ToolCall, tool: Any, batch_suffix: str = "", planned_edit: Any = None) -> tuple[bool, str]:
        self.output_fn(self.approval_display(call, tool, "confirm", batch_suffix=batch_suffix, planned_edit=planned_edit))
        approved, reason = self.interactions.approval(self._current_model_call_id, call.id, call.name, call.args)
        self._approval_by_call[call.id] = "approved" if approved else "refused"
        return approved, reason

    def _allowed(self, call: ToolCall) -> bool:
        return self.allowed_tools is None or call.name in self.allowed_tools

    def parallel_safe(self, call: ToolCall) -> bool:
        return self._allowed(call) and super().parallel_safe(call)

    def execute_readonly(self, call: ToolCall) -> tuple[str, str, str | None, float]:
        if not self._allowed(call):
            return "reject", f"ToolError: tool {call.name} is not allowed by this execution profile", None, 0.0
        return super().execute_readonly(call)

    def run_one(self, call: ToolCall, *args: Any, **kwargs: Any) -> tuple[str, str, dict[str, Any] | None]:
        if not self._allowed(call):
            output = f"ToolError: tool {call.name} is not allowed by this execution profile"
            d = ToolDisplay(batch_suffix=str(kwargs.get("batch_suffix", "")))
            content = self.reject(call, output, d=d)
            return (
                "failed",
                content,
                None,
            )
        self._interaction_call = call
        try:
            return super().run_one(call, *args, **kwargs)
        finally:
            self._interaction_call = None

    def run(self, calls: list[ToolCall], batch_suffix: str = "", model_call_id: str = "") -> list[dict[str, Any]]:
        """Adapt the public ToolRunner result protocol into deterministic eval events.

        This deliberately lives in the eval worker: production ToolRunner remains
        unchanged, while its one-result-per-call contract gives the evaluator a
        stable observation boundary for serial, parallel, malformed, unknown,
        refused, skipped, and Provider-builtin calls.
        """

        self._current_model_call_id = model_call_id or f"model-call.{self.session.usage.calls}"
        started = time.monotonic()
        try:
            messages = super().run(calls, batch_suffix=batch_suffix)
        except BaseException as exc:
            elapsed = (time.monotonic() - started) / max(1, len(calls))
            for call in calls:
                self.recorder.record_tool(
                    model_call_id=self._current_model_call_id,
                    tool_call_id=call.id,
                    name=call.name,
                    args=call.args,
                    mutates=bool((tool_class := TOOL_REGISTRY.get(call.name)) and tool_class.MUTATES),
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_seconds=elapsed,
                    approval=self._approval_by_call.pop(call.id, "not_required"),
                )
            raise

        tool_messages = {
            str(message.get("tool_call_id")): message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id")
        }
        elapsed = (time.monotonic() - started) / max(1, len(calls))
        builtins = self.session.config.provider.builtin_function_names()
        for call in calls:
            message = tool_messages.get(call.id, {})
            content = str(message.get("content") or "")
            approval = self._approval_by_call.pop(call.id, "not_required")
            if call.name in builtins:
                status = "builtin_echoed"
            elif "Skipped: previous tool call was refused" in content:
                status = "skipped"
            elif approval == "refused" or "Cancelled: user refused tool call" in content:
                status = "refused"
                approval = "refused"
            elif "status: failed" in content:
                status = "failed"
            else:
                status = "succeeded"
            tool_class = TOOL_REGISTRY.get(call.name)
            mutates = bool(tool_class and tool_class.MUTATES)
            if approval == "not_required" and status == "succeeded" and mutates and self.session.settings.yolo:
                try:
                    if tool_class is not None and tool_class(self.session, call.args).needs_confirmation():
                        approval = "auto_approved"
                except Exception:  # noqa: BLE001 - approval metadata must not affect tool execution
                    approval = "not_required"
            self.recorder.record_tool(
                model_call_id=self._current_model_call_id,
                tool_call_id=call.id,
                name=call.name,
                args=call.args,
                mutates=mutates,
                status=status,
                error=content if status in {"failed", "refused", "skipped"} else "",
                elapsed_seconds=elapsed,
                approval=approval,
                result=content,
            )
        return messages


class EvaluationAgent(Agent):
    def __init__(
        self,
        session: Session,
        *,
        allowed_tools: frozenset[str] | None,
        recorder: TrajectoryRecorder,
        interactions: ScriptedInteractionController,
        input_fn: Any,
        output_fn: Any,
    ):
        super().__init__(session, input_fn=input_fn, output_fn=output_fn)
        self.allowed_tools = allowed_tools
        self.tools = EvaluationToolRunner(
            session,
            self.context,
            input_fn=input_fn,
            output_fn=output_fn,
            allowed_tools=allowed_tools,
            recorder=recorder,
            interactions=interactions,
        )

    def prepare_request(self, turn_messages: list[dict[str, Any]]) -> PreparedRequest:
        prepared = super().prepare_request(turn_messages)
        if self.allowed_tools is None:
            return prepared
        filtered = [schema for schema in prepared.tools if str((schema.get("function") or {}).get("name", "")) in self.allowed_tools]
        return PreparedRequest(prepared.messages, filtered, prepared.pending)


def _run_evaluation(payload: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(str(payload["workspace"])).resolve()
    artifact_dir = Path(str(payload["artifact_dir"])).resolve()
    prompt = str(payload["prompt"])
    max_steps = int(payload.get("max_steps", 200))
    driver = str(payload.get("driver", "yucode"))
    if driver != "yucode":
        raise ValueError(f"unsupported evaluation driver: {driver}")
    profile = str(payload.get("profile", "coding_default"))
    raw_allowed = payload.get("allowed_tools")
    if raw_allowed is not None and (not isinstance(raw_allowed, list) or any(not isinstance(item, str) for item in raw_allowed)):
        raise TypeError("allowed_tools must be an array of strings")
    allowed_tools = frozenset(raw_allowed) if isinstance(raw_allowed, list) else None
    config_data = payload.get("config")
    if not isinstance(config_data, dict):
        raise TypeError("config must be an object")
    scenario_value = payload.get("_scenario", {})
    if not isinstance(scenario_value, dict):
        raise TypeError("scenario must be an object")
    scenario_spec = scenario_from_dict(scenario_value)

    secret_values: list[str] = []

    def collect_secrets(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for name, item in value.items():
                collect_secrets(item, str(name))
        elif isinstance(value, list):
            for item in value:
                collect_secrets(item, key)
        elif isinstance(value, str) and value and any(token in key.lower() for token in ("key", "token", "secret", "password")):
            secret_values.append(value)

    collect_secrets(config_data)
    recorder = TrajectoryRecorder(secrets=secret_values)
    interactions = ScriptedInteractionController.from_dict(scenario_spec.interactions, recorder)

    # Evaluation policy is stricter than an interactive config: no MCP, provider
    # tools, quick hints, or skills unless a benchmark explicitly opts in.
    normalized = dict(config_data)
    normalized["mcp"] = {}
    paths = dict(normalized.get("paths") or {})
    paths["data_dir"] = str(artifact_dir / "data")
    normalized["paths"] = paths
    runtime = dict(normalized.get("runtime") or {})
    runtime["max_agent_steps"] = max_steps
    runtime["quick_hints"] = False
    normalized["runtime"] = runtime

    config = Config.from_dict(normalized)
    if profile != "provider_tools":
        for provider in config.providers.values():
            provider.builtin_tools = ()
    settings = RuntimeSettings.from_dict(normalized, yolo=bool(payload.get("yolo", True)))
    session = Session(cwd=str(workspace), config=config, settings=settings)
    session.skills = SkillLibrary({})

    attachment_paths = _prepare_attachments(workspace, payload.get("attachments", []))
    index_status = CodeIndex(session).sync(force=True) if profile == "coding_indexed" else ""

    output_lines: list[str] = []

    agent = EvaluationAgent(
        session,
        allowed_tools=allowed_tools,
        recorder=recorder,
        interactions=interactions,
        input_fn=lambda _prompt="": "",
        output_fn=lambda value: output_lines.append(str(value)),
    )
    builtin_calls: list[dict[str, str]] = []
    if hasattr(agent.model, "on_builtin_call"):
        agent.model.on_builtin_call = lambda name, detail: builtin_calls.append({"name": str(name), "detail": str(detail)})
    provider_rounds: list[dict[str, Any]] = []

    def agent_input(text: str) -> str | UserInput:
        if profile != "vision_attachment" or not attachment_paths:
            return text
        images = tuple(session.images.load(str(workspace / path), source_text=path) for path in attachment_paths)
        return UserInput(text.rstrip() + "\n" + " ".join(IMAGE_MARKER for _image in images), images)

    rounds = list(scenario_spec.rounds) or [{"prompt": prompt}]
    answer = ""
    for position, item in enumerate(rounds, start=1):
        provider_name = item.get("provider")
        if provider_name is not None:
            if provider_name == "@active":
                provider_name = config.active_provider
            elif provider_name == "@alternate":
                provider_name = next((name for name in sorted(config.providers) if name != config.active_provider), None)
            if not isinstance(provider_name, str) or provider_name not in config.providers:
                raise ValueError(f"scenario round {position} names an unknown provider: {provider_name}")
            config.active_provider = provider_name
        round_prompt = item.get("prompt", prompt)
        if not isinstance(round_prompt, str) or not round_prompt:
            raise TypeError(f"scenario round {position}.prompt must be a non-empty string")
        answer = agent.run(agent_input(round_prompt))
        provider_rounds.append(
            {
                "position": position,
                "provider": config.active_provider,
                "wire": config.provider.resolve().api,
                "session_uid": session.uid,
            }
        )
    session.save_snapshot()
    trajectory_digest = recorder.write(artifact_dir / "trajectory.jsonl")

    session_path = Path(SessionSnapshotStore.session_path(config.data_dir, session.cwd, session.uid))
    if session_path.is_file():
        shutil.copyfile(session_path, artifact_dir / "session.jsonl")
    (artifact_dir / "agent.log").write_text(
        "\n".join(output_lines).rstrip() + ("\n" if output_lines else ""),
        encoding="utf-8",
    )
    usage = asdict(session.usage)
    tool_call_ids = {
        str(call.get("id"))
        for message in session.messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
        if isinstance(call, dict) and call.get("id")
    }
    tool_result_ids = {str(message.get("tool_call_id")) for message in session.messages if message.get("role") == "tool" and message.get("tool_call_id")}
    tool_events = [event for event in recorder.events if event.get("event_type") == "tool"]
    tool_status_counts = {
        status: sum(event.get("status") == status for event in tool_events) for status in ("succeeded", "failed", "refused", "skipped", "builtin_echoed")
    }
    metrics: dict[str, Any] = {
        "schema_version": 3,
        "status": "ok",
        "driver": driver,
        "profile": profile,
        "answer": answer,
        "max_steps_exhausted": answer.startswith("Stopped after max_agent_steps="),
        "usage": {
            "model_calls": usage["calls"],
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "cached_read_tokens": usage["cached_prompt_tokens"],
            "cached_write_tokens": usage["cache_write_prompt_tokens"],
        },
        "model_calls": usage["calls"],
        "tool_calls": len(tool_events),
        "tool_status_counts": tool_status_counts,
        "tool_names": [str(event.get("name")) for event in tool_events if event.get("status") in {"succeeded", "builtin_echoed"}],
        "tool_errors": sum(event.get("status") in {"failed", "refused"} for event in tool_events),
        "compactions": session.state.compaction_count,
        "retries": session.state.model_retry_count,
        "session_uid": session.uid,
        "effective_tools": sorted(allowed_tools) if allowed_tools is not None else None,
        "dangling_tool_results": len(tool_call_ids - tool_result_ids),
        "attachment_paths": list(attachment_paths),
        "index_status": index_status,
        "provider_rounds": provider_rounds,
        "provider_rounds_count": len(provider_rounds),
        "provider_distinct": len({item["provider"] for item in provider_rounds}),
        "builtin_calls": builtin_calls,
        "builtin_calls_count": len(builtin_calls),
        "cached_tokens": usage["cached_prompt_tokens"] + usage["cache_write_prompt_tokens"],
        "strict_tools_active": config.provider.resolve().strict_tools_active,
        "attachment_inputs": len(attachment_paths) if profile == "vision_attachment" else 0,
        "trajectory_digest": trajectory_digest,
        "trajectory_events": len(recorder.events),
        "interaction_checks": [asdict(item) for item in interactions.outcomes()],
    }
    if session.mcp is not None:
        session.mcp.close()
    return metrics


def run(payload: dict[str, Any]) -> dict[str, Any]:
    scenario = payload.get("scenario", {})
    if not isinstance(scenario, dict):
        raise TypeError("scenario must be an object")
    normalized_payload = dict(payload)
    normalized_payload["_scenario"] = scenario
    return _run_evaluation(normalized_payload)


def main() -> int:
    artifact_dir: Path | None = None
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise TypeError("worker input must be a JSON object")
        artifact_dir = Path(str(payload["artifact_dir"])).resolve()
        result = run(payload)
        _write_json(artifact_dir / "worker.json", result)
        return 0
    except BaseException as exc:  # noqa: BLE001 - worker boundary always emits diagnostics
        if artifact_dir is not None:
            _write_json(
                artifact_dir / "worker.json",
                {
                    "schema_version": 2,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        traceback.print_exc(file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
