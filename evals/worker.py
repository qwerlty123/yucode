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
from yucode.tools.search import CodeIndex


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


def _scenario_result(
    expect: Any,
    *,
    workspace: Path,
    answer: str,
    metrics: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    if expect is None:
        return [], True
    if not isinstance(expect, dict):
        raise TypeError("scenario.expect must be an object")
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any, passed: bool | None = None) -> None:
        checks.append({"name": name, "actual": actual, "expected": expected, "passed": actual == expected if passed is None else passed})

    for key, expected in expect.items():
        if key in {"files_present", "files_absent"}:
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                raise TypeError(f"scenario.expect.{key} must be an array of paths")
            actual = [item for item in expected if (workspace / item).exists()]
            check(key, actual, expected if key == "files_present" else [], actual == expected if key == "files_present" else not actual)
        elif key == "answer_contains":
            needles = [expected] if isinstance(expected, str) else expected
            if not isinstance(needles, list) or any(not isinstance(item, str) for item in needles):
                raise TypeError("scenario.expect.answer_contains must be a string or array of strings")
            missing = [item for item in needles if item not in answer]
            check(key, missing, [], not missing)
        elif key == "compactions_min":
            if not isinstance(expected, int) or isinstance(expected, bool):
                raise TypeError("scenario.expect.compactions_min must be an integer")
            actual = int(metrics.get("compactions", 0))
            check(key, actual, f">={expected}", actual >= expected)
        elif key in {"builtin_calls_min", "cached_tokens_min", "provider_rounds_min", "provider_distinct_min"}:
            if not isinstance(expected, int) or isinstance(expected, bool):
                raise TypeError(f"scenario.expect.{key} must be an integer")
            metric_name = {
                "builtin_calls_min": "builtin_calls_count",
                "cached_tokens_min": "cached_tokens",
                "provider_rounds_min": "provider_rounds_count",
                "provider_distinct_min": "provider_distinct",
            }[key]
            actual = int(metrics.get(metric_name, 0))
            check(key, actual, f">={expected}", actual >= expected)
        elif key == "index_ready":
            actual = str(metrics.get("index_status", "")).startswith("code_index: rebuilt")
            check(key, actual, bool(expected))
        elif key == "tools_used":
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                raise TypeError("scenario.expect.tools_used must be an array of tool names")
            observed = metrics.get("tool_names", [])
            actual = [name for name in expected if name in observed] if isinstance(observed, list) else []
            check(key, actual, expected)
        elif key in {
            "tool_errors",
            "dangling_tool_results",
            "model_calls",
            "strict_tools_active",
            "attachment_inputs",
        }:
            check(key, metrics.get(key), expected)
        else:
            raise ValueError(f"unsupported scenario expectation: {key}")
    return checks, all(bool(item["passed"]) for item in checks)


class EvaluationToolRunner(ToolRunner):
    def __init__(self, *args: Any, allowed_tools: frozenset[str] | None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.allowed_tools = allowed_tools

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
            return (
                "failed",
                self.reject(
                    call,
                    f"ToolError: tool {call.name} is not allowed by this execution profile",
                    d=ToolDisplay(batch_suffix=str(kwargs.get("batch_suffix", ""))),
                ),
                None,
            )
        return super().run_one(call, *args, **kwargs)


class EvaluationAgent(Agent):
    def __init__(
        self,
        session: Session,
        *,
        allowed_tools: frozenset[str] | None,
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
        input_fn=lambda _prompt="": "",
        output_fn=lambda value: output_lines.append(str(value)),
    )
    builtin_calls: list[dict[str, str]] = []
    if hasattr(agent.model, "on_builtin_call"):
        agent.model.on_builtin_call = lambda name, detail: builtin_calls.append({"name": str(name), "detail": str(detail)})
    scenario = payload.get("_scenario", {})
    if not isinstance(scenario, dict):
        raise TypeError("scenario must be an object")
    provider_rounds: list[dict[str, Any]] = []

    def agent_input(text: str) -> str | UserInput:
        if profile != "vision_attachment" or not attachment_paths:
            return text
        images = tuple(session.images.load(str(workspace / path), source_text=path) for path in attachment_paths)
        return UserInput(text.rstrip() + "\n" + " ".join(IMAGE_MARKER for _image in images), images)

    raw_rounds = scenario.get("rounds", [])
    if not isinstance(raw_rounds, list) or any(not isinstance(item, dict) for item in raw_rounds):
        raise TypeError("scenario.rounds must be an array of objects")
    rounds = raw_rounds or [{"prompt": prompt}]
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
    metrics: dict[str, Any] = {
        "schema_version": 2,
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
        "tool_calls": len(session.tool_records) + len(session.tool_errors),
        "tool_names": [record.name for record in session.tool_records],
        "tool_errors": len(session.tool_errors),
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
    }
    checks, scenario_passed = _scenario_result(scenario.get("expect"), workspace=workspace, answer=answer, metrics=metrics)
    metrics["scenario_checks"] = checks
    metrics["scenario_passed"] = scenario_passed
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
