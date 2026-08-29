from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .models import CheckOutcome
from .trace import redact, sha256_bytes

ToolStatus = Literal["succeeded", "failed", "refused", "skipped", "builtin_echoed"]


@dataclass(frozen=True)
class ScenarioSpec:
    schema_version: int
    rounds: tuple[dict[str, Any], ...] = ()
    checks: tuple[dict[str, Any], ...] = ()
    interactions: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, dict[str, Any]] = field(default_factory=dict)

    def worker_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema_version": self.schema_version}
        if self.rounds:
            payload["rounds"] = list(self.rounds)
        if self.interactions:
            payload["interactions"] = self.interactions
        return payload


class TrajectoryRecorder:
    """Canonical in-memory recorder for tool and scripted-interaction events."""

    def __init__(self, *, secrets: Iterable[str] = ()):
        self.secrets = tuple(item for item in secrets if item)
        self._events: list[dict[str, Any]] = []
        self._tool_sequence = 0

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    def _append(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "schema_version": 1,
            "event_index": len(self._events) + 1,
            **redact(payload, secrets=self.secrets),
        }
        self._events.append(event)
        return event

    def record_tool_execution(self, raw: Any) -> dict[str, Any]:
        value = raw if isinstance(raw, dict) else vars(raw)
        return self.record_tool(
            model_call_id=str(value.get("model_call_id") or ""),
            tool_call_id=str(value.get("tool_call_id") or ""),
            name=str(value.get("name") or ""),
            args=value.get("args", []),
            mutates=bool(value.get("mutates", False)),
            status=str(value.get("status") or "failed"),  # type: ignore[arg-type]
            error=str(value.get("error") or ""),
            elapsed_seconds=float(value.get("elapsed_seconds") or 0.0),
            approval=str(value.get("approval") or "not_required"),
            result=str(value.get("result") or ""),
        )

    def record_tool(
        self,
        *,
        model_call_id: str,
        tool_call_id: str,
        name: str,
        args: Any,
        mutates: bool,
        status: ToolStatus,
        error: str = "",
        elapsed_seconds: float = 0.0,
        approval: str = "not_required",
        result: str = "",
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "refused", "skipped", "builtin_echoed"}:
            raise ValueError(f"unsupported tool event status: {status}")
        self._tool_sequence += 1
        safe_result = str(redact(result, secrets=self.secrets))
        result_summary = " ".join(safe_result.split())
        if len(result_summary) > 240:
            result_summary = result_summary[:237].rstrip() + "..."
        return self._append(
            {
                "event_type": "tool",
                "tool_sequence": self._tool_sequence,
                "model_call_id": model_call_id,
                "tool_call_id": tool_call_id,
                "name": name,
                "args": args,
                "mutates": mutates,
                "status": status,
                "error": error or None,
                "latency_ms": round(max(0.0, elapsed_seconds) * 1000, 3),
                "approval": approval,
                "result_summary": result_summary,
                "result_summary_hash": sha256_bytes(result_summary.encode("utf-8")),
                "result_chars": len(safe_result),
            }
        )

    def record_interaction(
        self,
        *,
        interaction_type: Literal["approval", "question"],
        model_call_id: str,
        tool_call_id: str,
        rule_id: str | None,
        prompt: str,
        reply: str,
        matched: bool,
    ) -> dict[str, Any]:
        return self._append(
            {
                "event_type": "interaction",
                "interaction_type": interaction_type,
                "model_call_id": model_call_id,
                "tool_call_id": tool_call_id,
                "rule_id": rule_id,
                "prompt": prompt,
                "reply": reply,
                "matched": matched,
            }
        )

    def write(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in self._events)
        path.write_text(content, encoding="utf-8")
        return sha256_bytes(content.encode("utf-8"))


def load_trajectory(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        return ()
    events: list[dict[str, Any]] = []
    for position, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid trajectory event at line {position}: {exc}") from exc
        if not isinstance(value, dict):
            raise TypeError(f"trajectory event at line {position} must be an object")
        events.append(value)
    return tuple(events)


def _legacy_assertions(expect: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize V0 expect entries into the V1 declarative assertion model."""

    checks: list[dict[str, Any]] = []
    minimum_metrics = {
        "compactions_min": "compactions",
        "builtin_calls_min": "builtin_calls_count",
        "cached_tokens_min": "cached_tokens",
        "provider_rounds_min": "provider_rounds_count",
        "provider_distinct_min": "provider_distinct",
    }
    exact_metrics = {"tool_errors", "dangling_tool_results", "model_calls", "strict_tools_active", "attachment_inputs"}
    for key, expected in expect.items():
        base: dict[str, Any] = {
            "id": key,
            "kind": "assertion",
            "domain": "protocol",
            "category": "state",
            "failure_code": "protocol.scenario",
        }
        if key in {"files_present", "files_absent"}:
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                raise TypeError(f"scenario.expect.{key} must be an array of paths")
            checks.append(
                {
                    **base,
                    "source": "workspace_paths",
                    "paths": list(expected),
                    "operator": "all_true" if key == "files_present" else "all_false",
                }
            )
        elif key == "answer_contains":
            needles = [expected] if isinstance(expected, str) else expected
            if not isinstance(needles, list) or any(not isinstance(item, str) for item in needles):
                raise TypeError("scenario.expect.answer_contains must be a string or array of strings")
            checks.append({**base, "source": "answer", "operator": "contains_all", "value": list(needles)})
        elif key in minimum_metrics:
            checks.append({**base, "source": "metric", "metric": minimum_metrics[key], "operator": "gte", "value": int(expected)})
        elif key == "index_ready":
            if not isinstance(expected, bool):
                raise TypeError("scenario.expect.index_ready must be a boolean")
            checks.append(
                {
                    **base,
                    "source": "metric",
                    "metric": "index_status",
                    "operator": "starts_with" if expected else "not_starts_with",
                    "value": "code_index: rebuilt",
                }
            )
        elif key == "tools_used":
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                raise TypeError("scenario.expect.tools_used must be an array of tool names")
            checks.append({**base, "source": "metric", "metric": "tool_names", "operator": "contains_all", "value": list(expected)})
        elif key in exact_metrics:
            checks.append({**base, "source": "metric", "metric": key, "operator": "equals", "value": expected})
        else:
            raise ValueError(f"unsupported scenario expectation: {key}")
    return checks


def scenario_from_dict(value: dict[str, Any] | None) -> ScenarioSpec:
    data = dict(value or {})
    version = data.get("schema_version", 0)
    if version not in {0, 1}:
        raise ValueError(f"unsupported scenario schema_version: {version}")
    rounds = data.get("rounds", [])
    checks = data.get("checks", [])
    interactions = data.get("interactions", {})
    safety = data.get("safety", {})
    expect = data.get("expect", {})
    if not isinstance(rounds, list) or any(not isinstance(item, dict) for item in rounds):
        raise TypeError("scenario.rounds must be an array of objects")
    if not isinstance(checks, list) or any(not isinstance(item, dict) for item in checks):
        raise TypeError("scenario.checks must be an array of objects")
    if not isinstance(interactions, dict):
        raise TypeError("scenario.interactions must be an object")
    if not isinstance(safety, dict) or any(not isinstance(name, str) or not isinstance(config, dict) for name, config in safety.items()):
        raise TypeError("scenario.safety must map checker names to objects")
    if not isinstance(expect, dict):
        raise TypeError("scenario.expect must be an object")
    normalized_checks = [dict(item) for item in checks]
    normalized_checks.extend(_legacy_assertions(expect))
    seen: set[str] = set()
    for position, check in enumerate(normalized_checks, start=1):
        check_id = check.get("id")
        kind = check.get("kind")
        if not isinstance(check_id, str) or not check_id:
            raise ValueError(f"scenario.checks[{position}].id must be a non-empty string")
        if check_id in seen:
            raise ValueError(f"duplicate scenario check id: {check_id}")
        if kind not in {"count", "arguments", "order", "recovery", "duplicates", "metric", "assertion"}:
            raise ValueError(f"unsupported scenario check kind: {kind}")
        if kind == "assertion" and check.get("source") not in {"workspace_paths", "answer", "metric"}:
            raise ValueError(f"scenario assertion {check_id} has an invalid source")
        seen.add(check_id)
    # Parse interaction rules during manifest validation, not after a model run
    # has already started. The temporary recorder remains empty.
    ScriptedInteractionController.from_dict(interactions, TrajectoryRecorder())
    return ScenarioSpec(version or 1, tuple(rounds), tuple(normalized_checks), dict(interactions), dict(safety))


def load_scenario(path: Path | None) -> ScenarioSpec:
    if path is None:
        return scenario_from_dict({})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid scenario {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"scenario must be an object: {path}")
    return scenario_from_dict(value)


def _path(value: Any, dotted: str) -> Any:
    current = value
    for token in dotted.split(".") if dotted else []:
        if isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return None
    return current


def _compare(actual: Any, operator: str, expected: Any, *, workspace: Path | None = None) -> bool:
    if operator in {"eq", "equals"}:
        return actual == expected
    if operator in {"ne", "not_equals"}:
        return actual != expected
    if operator == "contains":
        return str(expected) in str(actual)
    if operator == "contains_all":
        if not isinstance(expected, list):
            return False
        if isinstance(actual, list):
            return all(item in actual for item in expected)
        return all(str(item) in str(actual) for item in expected)
    if operator == "regex":
        return re.search(str(expected), str(actual)) is not None
    if operator == "glob":
        return fnmatch.fnmatch(str(actual), str(expected))
    if operator == "starts_with":
        return str(actual).startswith(str(expected))
    if operator == "not_starts_with":
        return not str(actual).startswith(str(expected))
    if operator == "all_true":
        return isinstance(actual, list) and all(item is True for item in actual)
    if operator == "all_false":
        return isinstance(actual, list) and all(item is False for item in actual)
    if operator == "lte":
        return isinstance(actual, int | float) and not isinstance(actual, bool) and actual <= expected
    if operator == "gte":
        return isinstance(actual, int | float) and not isinstance(actual, bool) and actual >= expected
    if operator == "lt":
        return isinstance(actual, int | float) and not isinstance(actual, bool) and actual < expected
    if operator == "gt":
        return isinstance(actual, int | float) and not isinstance(actual, bool) and actual > expected
    if operator == "within_workspace":
        if workspace is None or not isinstance(actual, str):
            return False
        candidate = (workspace / actual).resolve() if not Path(actual).is_absolute() else Path(actual).resolve()
        try:
            candidate.relative_to(workspace.resolve())
            return True
        except ValueError:
            return False
    raise ValueError(f"unsupported assertion operator: {operator}")


def _event_match(event: dict[str, Any], match: Any) -> bool:
    if not isinstance(match, dict):
        raise TypeError("trajectory match must be an object")
    for key, expected in match.items():
        field = "name" if key == "tool" else key
        if field == "event":
            field = "event_type"
        if event.get(field) != expected:
            return False
    return True


def _argument_condition(args: Any, condition: Any, *, workspace: Path | None = None) -> bool:
    if not isinstance(condition, dict):
        raise TypeError("argument matcher must be an object")
    dotted = condition.get("path", "args")
    if not isinstance(dotted, str):
        raise TypeError("argument matcher path must be a string")
    root = {"args": args}
    actual = _path(root, dotted)
    operator = condition.get("operator", "equals")
    if not isinstance(operator, str):
        raise TypeError("argument matcher operator must be a string")
    return _compare(actual, operator, condition.get("value"), workspace=workspace)


def _outcome(
    check: dict[str, Any],
    *,
    category: str,
    passed: bool,
    code: str,
    actual: Any,
    expected: Any,
    evidence: tuple[str, ...] = ("trajectory.jsonl",),
) -> CheckOutcome:
    return CheckOutcome(
        id=str(check.get("id") or category),
        domain=str(check.get("domain") or ("budget" if category == "budget" else "trajectory")),
        category=category,
        passed=passed,
        code=str(check.get("failure_code") or code),
        actual=actual,
        expected=expected,
        evidence_refs=evidence,
    )


def evaluate_scenario(
    spec: ScenarioSpec,
    events: Iterable[dict[str, Any]],
    *,
    workspace: Path,
    answer: str,
    metrics: dict[str, Any],
) -> tuple[CheckOutcome, ...]:
    rows = tuple(events)
    tools = tuple(event for event in rows if event.get("event_type") == "tool")
    outcomes: list[CheckOutcome] = []
    for check in spec.checks:
        kind = check["kind"]
        if kind == "count":
            matched = [event for event in tools if _event_match(event, check.get("match", {}))]
            minimum = int(check.get("min", 0))
            maximum = check.get("max")
            passed = len(matched) >= minimum and (maximum is None or len(matched) <= int(maximum))
            forbidden = maximum == 0
            outcomes.append(
                _outcome(
                    check,
                    category="tool" if forbidden else "trigger",
                    passed=passed,
                    code="trajectory.tool.forbidden" if forbidden else "trajectory.trigger.missing",
                    actual=len(matched),
                    expected={"min": minimum, "max": maximum},
                )
            )
        elif kind == "arguments":
            matched = [event for event in tools if _event_match(event, check.get("match", {}))]
            results = [
                _compare(
                    _path(event, str(check.get("path") or "args")),
                    str(check.get("operator") or "equals"),
                    check.get("value"),
                    workspace=workspace,
                )
                for event in matched
            ]
            quantifier = check.get("quantifier", "all")
            passed = bool(results) and (all(results) if quantifier == "all" else any(results))
            outcomes.append(
                _outcome(
                    check,
                    category="arguments",
                    passed=passed,
                    code="trajectory.arguments.invalid",
                    actual=results,
                    expected={"quantifier": quantifier, "value": check.get("value")},
                )
            )
        elif kind == "order":
            first = next((index for index, event in enumerate(tools) if _event_match(event, check.get("first", {}))), None)
            then = next(
                (index for index, event in enumerate(tools) if first is not None and index > first and _event_match(event, check.get("then", {}))), None
            )
            outcomes.append(
                _outcome(
                    check,
                    category="order",
                    passed=first is not None and then is not None,
                    code="trajectory.order.invalid",
                    actual={"first": first, "then": then},
                    expected="first before then",
                )
            )
        elif kind == "recovery":
            failure = next((index for index, event in enumerate(tools) if _event_match(event, check.get("failure", {}))), None)
            success = next(
                (index for index, event in enumerate(tools) if failure is not None and index > failure and _event_match(event, check.get("success", {}))),
                None,
            )
            changed = True
            if check.get("arguments_changed") and failure is not None and success is not None:
                changed = tools[failure].get("args") != tools[success].get("args")
            passed = failure is not None and success is not None and changed
            outcomes.append(
                _outcome(
                    check,
                    category="recovery",
                    passed=passed,
                    code="trajectory.recovery.failed",
                    actual={"failure": failure, "success": success, "arguments_changed": changed},
                    expected="a later successful recovery",
                )
            )
        elif kind == "duplicates":
            matched = [event for event in tools if _event_match(event, check.get("match", {}))]
            signatures = [json.dumps([event.get("name"), event.get("args")], ensure_ascii=False, sort_keys=True) for event in matched]
            duplicates = len(signatures) - len(set(signatures))
            maximum = int(check.get("max", 0))
            outcomes.append(
                _outcome(
                    check,
                    category="redundant",
                    passed=duplicates <= maximum,
                    code="trajectory.redundant",
                    actual=duplicates,
                    expected=f"<={maximum}",
                )
            )
        elif kind == "metric":
            name = str(check.get("metric") or "")
            actual = metrics.get(name)
            operator = str(check.get("operator") or "lte")
            expected = check.get("value")
            passed = _compare(actual, operator, expected)
            suffix = {
                "tool_calls": "tool_calls",
                "model_calls": "model_calls",
                "tool_errors": "tool_errors",
                "total_tokens": "tokens",
                "estimated_cost_usd": "cost",
            }.get(name, name or "metric")
            outcomes.append(
                _outcome(
                    check,
                    category="budget",
                    passed=passed,
                    code="budget." + suffix,
                    actual=actual,
                    expected={"operator": operator, "value": expected},
                    evidence=("worker.json",),
                )
            )
        elif kind == "assertion":
            source = str(check.get("source") or "")
            evidence = ("worker.json",)
            if source == "workspace_paths":
                paths = check.get("paths", [])
                if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
                    raise TypeError(f"scenario assertion {check['id']} paths must be an array of strings")
                actual = [(workspace / item).exists() for item in paths]
                evidence = ("patch.diff",)
            elif source == "answer":
                actual = answer
            elif source == "metric":
                metric_name = check.get("metric")
                if not isinstance(metric_name, str) or not metric_name:
                    raise TypeError(f"scenario assertion {check['id']} metric must be a non-empty string")
                actual = metrics.get(metric_name)
            else:  # scenario_from_dict validates this before a worker starts
                raise ValueError(f"unsupported assertion source: {source}")
            operator = str(check.get("operator") or "equals")
            expected = check.get("value")
            outcomes.append(
                _outcome(
                    check,
                    category=str(check.get("category") or "state"),
                    passed=_compare(actual, operator, expected, workspace=workspace),
                    code="protocol.scenario",
                    actual=actual,
                    expected={"operator": operator, "value": expected},
                    evidence=evidence,
                )
            )
    return tuple(outcomes)


@dataclass
class _InteractionRule:
    id: str
    kind: str
    match: dict[str, Any]
    reply: str
    min_uses: int
    max_uses: int
    uses: int = 0


class ScriptedInteractionController:
    def __init__(self, rules: list[_InteractionRule], recorder: TrajectoryRecorder):
        self.rules = rules
        self.recorder = recorder
        self._unmatched: list[CheckOutcome] = []

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None, recorder: TrajectoryRecorder) -> ScriptedInteractionController:
        data = dict(value or {})
        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, list) or any(not isinstance(item, dict) for item in raw_rules):
            raise TypeError("scenario.interactions.rules must be an array of objects")
        rules: list[_InteractionRule] = []
        for position, item in enumerate(raw_rules, start=1):
            rule_id = item.get("id")
            kind = item.get("kind")
            match = item.get("match", {})
            reply = item.get("reply")
            minimum = item.get("min_uses", 1)
            maximum = item.get("max_uses", minimum)
            if not isinstance(rule_id, str) or not rule_id:
                raise ValueError(f"interaction rule {position} requires an id")
            if kind not in {"approval", "question"}:
                raise ValueError(f"interaction rule {rule_id} kind must be approval or question")
            if not isinstance(match, dict) or not isinstance(reply, str):
                raise TypeError(f"interaction rule {rule_id} requires object match and string reply")
            if not isinstance(minimum, int) or not isinstance(maximum, int) or minimum < 0 or maximum < minimum:
                raise ValueError(f"interaction rule {rule_id} has invalid use bounds")
            rules.append(_InteractionRule(rule_id, kind, dict(match), reply, minimum, maximum))
        return cls(rules, recorder)

    def _match(self, kind: str, *, tool: str = "", args: Any = None, question: str = "") -> _InteractionRule | None:
        for rule in self.rules:
            if rule.kind != kind or rule.uses >= rule.max_uses:
                continue
            expected_tool = rule.match.get("tool")
            if expected_tool is not None and expected_tool != tool:
                continue
            argument_match = rule.match.get("arguments")
            if argument_match is not None and not _argument_condition(args, argument_match):
                continue
            question_regex = rule.match.get("question_regex")
            if question_regex is not None and re.search(str(question_regex), question, re.IGNORECASE) is None:
                continue
            return rule
        return None

    def approval(self, model_call_id: str, tool_call_id: str, tool: str, args: Any) -> tuple[bool, str]:
        rule = self._match("approval", tool=tool, args=args)
        if rule is None:
            reply = "unmatched scripted approval"
            self.recorder.record_interaction(
                interaction_type="approval",
                model_call_id=model_call_id,
                tool_call_id=tool_call_id,
                rule_id=None,
                prompt=tool,
                reply=reply,
                matched=False,
            )
            self._unmatched.append(
                CheckOutcome(
                    "interaction.unmatched.approval", "protocol", "approval", False, "safety.approval.missing", tool, "matching rule", ("trajectory.jsonl",)
                )
            )
            return False, reply
        rule.uses += 1
        reply = rule.reply.strip()
        approved = reply.lower() in {"", "y", "yes", "approve", "approved"}
        self.recorder.record_interaction(
            interaction_type="approval",
            model_call_id=model_call_id,
            tool_call_id=tool_call_id,
            rule_id=rule.id,
            prompt=tool,
            reply=reply,
            matched=True,
        )
        return approved, "" if reply.lower() in {"n", "no"} or approved else reply

    def question(self, model_call_id: str, tool_call_id: str, question: str) -> str:
        rule = self._match("question", question=question)
        if rule is None:
            reply = "Evaluation interaction unavailable"
            self.recorder.record_interaction(
                interaction_type="question",
                model_call_id=model_call_id,
                tool_call_id=tool_call_id,
                rule_id=None,
                prompt=question,
                reply=reply,
                matched=False,
            )
            self._unmatched.append(
                CheckOutcome(
                    "interaction.unmatched.question",
                    "protocol",
                    "question",
                    False,
                    "trajectory.trigger.missing",
                    question,
                    "matching rule",
                    ("trajectory.jsonl",),
                )
            )
            return reply
        rule.uses += 1
        self.recorder.record_interaction(
            interaction_type="question",
            model_call_id=model_call_id,
            tool_call_id=tool_call_id,
            rule_id=rule.id,
            prompt=question,
            reply=rule.reply,
            matched=True,
        )
        return rule.reply

    def outcomes(self) -> tuple[CheckOutcome, ...]:
        outcomes = [
            CheckOutcome(
                rule.id,
                "protocol",
                rule.kind,
                rule.min_uses <= rule.uses <= rule.max_uses,
                "trajectory.trigger.missing",
                rule.uses,
                {"min": rule.min_uses, "max": rule.max_uses},
                ("trajectory.jsonl",),
            )
            for rule in self.rules
        ]
        return (*outcomes, *self._unmatched)
