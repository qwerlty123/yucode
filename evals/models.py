from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal

RunStatus = Literal[
    "passed",
    "failed",
    "timeout",
    "agent_error",
    "infra_error",
]
ExecutionStatus = Literal["completed", "timed_out", "agent_error", "infra_error", "not_applicable"]
FunctionalOutcome = Literal["passed", "failed", "unknown"]
CheckResult = Literal["passed", "failed", "not_checked"]
ApplicabilityResult = Literal["applicable", "not_applicable"]

FAILURE_PRIORITY: dict[str, int] = {
    "infra": 900,
    "agent": 800,
    "safety": 700,
    "artifact.missing": 600,
    "budget.exceeded": 550,
    "verifier.failed": 500,
    "stop.abnormal": 450,
    "protocol": 400,
    "efficiency": 300,
}


@dataclass(frozen=True)
class FailureReason:
    code: str
    stage: str
    subject: str
    message: str
    evidence_refs: tuple[str, ...] = ()
    priority: int = 0

    @classmethod
    def create(
        cls,
        code: str,
        stage: str,
        subject: str,
        message: str,
        evidence_refs: tuple[str, ...] = (),
    ) -> FailureReason:
        family = next((prefix for prefix in FAILURE_PRIORITY if code == prefix or code.startswith(prefix + ".")), "protocol")
        return cls(code, stage, subject, message, evidence_refs, FAILURE_PRIORITY[family])


def primary_failure(failures: list[FailureReason] | tuple[FailureReason, ...]) -> FailureReason | None:
    if not failures:
        return None
    return min(failures, key=lambda item: (-item.priority, item.code, item.stage, item.subject))


@dataclass(frozen=True)
class Score:
    dimension: str
    subject: str
    result: str
    value: float | int | None = None
    threshold: float | int | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoreCard:
    functional: FunctionalOutcome = "unknown"
    protocol: CheckResult = "not_checked"
    safety: CheckResult = "not_checked"
    applicability: ApplicabilityResult = "applicable"
    efficiency: tuple[Score, ...] = ()
    reproducibility: CheckResult = "not_checked"
    conditions: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceManifest:
    schema_version: int
    experiment_id: str
    task_id: str
    repetition: int
    attempt: int
    git: dict[str, Any]
    digests: dict[str, str | None]
    agent: dict[str, Any]
    environment: dict[str, Any]
    platform: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...]
    redaction: dict[str, Any]
    complete: bool


@dataclass
class UsageMetrics:
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    estimated_cost_usd: float | None = None


@dataclass
class RunRecord:
    schema_version: int
    experiment_id: str
    task_id: str
    repetition: int
    agent: str
    status: RunStatus
    passed: bool
    comparable: bool
    started_at: str
    finished_at: str
    duration_seconds: float
    agent_duration_seconds: float | None = None
    grader_duration_seconds: float | None = None
    usage: UsageMetrics = field(default_factory=UsageMetrics)
    tool_calls: int = 0
    tool_errors: int = 0
    compactions: int = 0
    retries: int = 0
    patch_bytes: int = 0
    image: str | None = None
    image_digest: str | None = None
    source_revision: str | None = None
    error: str | None = None
    grader: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    stage: str = "finalized"
    execution_status: ExecutionStatus = "completed"
    functional_outcome: FunctionalOutcome = "unknown"
    protocol_result: CheckResult = "not_checked"
    safety_result: CheckResult = "not_checked"
    applicability: ApplicabilityResult = "applicable"
    subject: str = "agent_capability"
    targets: tuple[str, ...] = ()
    profile: str = "coding_default"
    failures: tuple[FailureReason, ...] = ()
    primary_failure: FailureReason | None = None
    scorecard: ScoreCard | None = None
    evidence_path: str | None = None
    trace_path: str | None = None
    trace_digest: str | None = None
    semantic_trace_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        payload = dict(data)
        payload["usage"] = UsageMetrics(**payload.get("usage", {}))
        payload["targets"] = tuple(payload.get("targets", ()))
        payload["failures"] = tuple(FailureReason(**item) if isinstance(item, dict) else item for item in payload.get("failures", ()))
        if isinstance(payload.get("primary_failure"), dict):
            payload["primary_failure"] = FailureReason(**payload["primary_failure"])
        if isinstance(payload.get("scorecard"), dict):
            scorecard = dict(payload["scorecard"])
            scorecard["efficiency"] = tuple(Score(**item) if isinstance(item, dict) else item for item in scorecard.get("efficiency", ()))
            payload["scorecard"] = ScoreCard(**scorecard)
        return cls(**payload)

    def score_eligible(self) -> bool:
        return self.applicability == "applicable" and self.execution_status not in {"infra_error", "not_applicable"}


def evaluate_contract(
    *,
    within_budget: bool,
    verifier_passed: bool,
    expected_artifact_exists: bool,
    normal_success_stop: bool,
    safety_passed: bool = True,
    evidence_refs: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ScoreCard, tuple[FailureReason, ...]]:
    refs = evidence_refs or {}
    conditions = {
        "within_budget": within_budget,
        "verifier_passed": verifier_passed,
        "expected_artifact_exists": expected_artifact_exists,
        "normal_success_stop": normal_success_stop,
    }
    failures: list[FailureReason] = []
    if not expected_artifact_exists:
        failures.append(FailureReason.create("artifact.missing", "artifact", "agent_capability", "expected artifact is missing", refs.get("artifact", ())))
    if not within_budget:
        failures.append(FailureReason.create("budget.exceeded", "agent", "agent_capability", "tool or step budget was exceeded", refs.get("budget", ())))
    if not verifier_passed:
        failures.append(
            FailureReason.create("verifier.failed", "grader", "agent_capability", "hidden verifier returned a non-zero exit code", refs.get("verifier", ()))
        )
    if not normal_success_stop:
        failures.append(
            FailureReason.create("stop.abnormal", "agent", "agent_mechanism", "agent did not stop with a normal final answer", refs.get("stop", ()))
        )
    if not safety_passed:
        failures.append(FailureReason.create("safety.violation", "agent", "agent_mechanism", "a declared safety check failed", refs.get("safety", ())))
    protocol_passed = expected_artifact_exists and normal_success_stop
    return (
        ScoreCard(
            functional="passed" if verifier_passed else "failed",
            protocol="passed" if protocol_passed else "failed",
            safety="passed" if safety_passed else "failed",
            applicability="applicable",
            conditions=conditions,
        ),
        tuple(failures),
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_records(path: str | Path) -> list[RunRecord]:
    source = Path(path)
    if source.is_dir():
        source = source / "results.jsonl"
    records: list[RunRecord] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError(f"results not found: {source}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError("record is not an object")
            records.append(RunRecord.from_dict(value))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid result at {source}:{line_number}: {exc}") from exc
    return records


def wilson_interval(successes: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def summarize(records: list[RunRecord]) -> dict[str, Any]:
    selected: dict[tuple[str, int], RunRecord] = {}
    for record in records:
        key = (record.task_id, record.repetition)
        if key not in selected or record.attempt >= selected[key].attempt:
            selected[key] = record
    selected_records = list(selected.values())
    eligible_records = [record for record in selected_records if record.score_eligible()]
    by_task: dict[str, list[RunRecord]] = {}
    for record in eligible_records:
        by_task.setdefault(record.task_id, []).append(record)
    tasks = []
    for task_id, task_records in sorted(by_task.items()):
        ordered = sorted(task_records, key=lambda item: item.repetition)
        passes = [record.passed for record in ordered]
        tasks.append(
            {
                "task_id": task_id,
                "runs": len(ordered),
                "passed_runs": sum(passes),
                "pass_at_1": bool(passes and passes[0]),
                "pass_at_k": any(passes),
                "all_at_k": bool(passes and all(passes)),
                "statuses": [record.status for record in ordered],
            }
        )

    run_successes = sum(record.passed for record in eligible_records)
    low, high = wilson_interval(run_successes, len(eligible_records))
    task_count = len(tasks)
    total_tokens = sum(record.usage.total_tokens for record in selected_records)
    costs = [record.usage.estimated_cost_usd for record in selected_records]
    known_costs = [cost for cost in costs if cost is not None]
    subject_results: list[dict[str, Any]] = []
    capability_results: list[dict[str, Any]] = []
    for subject in ("agent_capability", "agent_mechanism", "product_interface", "eval_harness"):
        subject_records = [record for record in eligible_records if record.subject == subject]
        subject_results.append(
            {
                "subject": subject,
                "runs": len(subject_records),
                "passed": sum(record.passed for record in subject_records),
                "success_rate": sum(record.passed for record in subject_records) / len(subject_records) if subject_records else None,
            }
        )
    targets = sorted({target for record in selected_records for target in record.targets})
    for target in targets:
        target_records = [record for record in eligible_records if target in record.targets]
        target_by_task: dict[str, list[RunRecord]] = {}
        for record in target_records:
            target_by_task.setdefault(record.task_id, []).append(record)
        pass_at_1_values: list[bool] = []
        all_at_k_values: list[bool] = []
        for task_records in target_by_task.values():
            ordered = sorted(task_records, key=lambda item: (item.repetition, item.attempt))
            pass_at_1_values.append(bool(ordered and ordered[0].passed))
            all_at_k_values.append(bool(ordered and all(record.passed for record in ordered)))
        capability_results.append(
            {
                "capability": target,
                "runs": len(target_records),
                "passed": sum(record.passed for record in target_records),
                "not_applicable": sum(target in record.targets and record.applicability == "not_applicable" for record in selected_records),
                "infra": sum(target in record.targets and record.execution_status == "infra_error" for record in selected_records),
                "success_rate": sum(record.passed for record in target_records) / len(target_records) if target_records else None,
                "pass_at_1": sum(pass_at_1_values) / len(pass_at_1_values) if pass_at_1_values else None,
                "all_at_k": sum(all_at_k_values) / len(all_at_k_values) if all_at_k_values else None,
            }
        )
    incomplete_evidence = sum(
        record.applicability == "applicable" and (record.scorecard is None or record.scorecard.reproducibility != "passed") for record in selected_records
    )
    agents = sorted({record.agent for record in selected_records})
    return {
        "schema_version": 2,
        "experiment_id": records[0].experiment_id if records else None,
        "agent": agents[0] if len(agents) == 1 else "mixed:" + ",".join(agents) if agents else None,
        "runs": len(selected_records),
        "attempts": len(records),
        "applicable_runs": sum(record.applicability == "applicable" for record in selected_records),
        "not_applicable_runs": sum(record.execution_status == "not_applicable" for record in selected_records),
        "infra_error_runs": sum(record.execution_status == "infra_error" for record in selected_records),
        "incomplete_evidence_runs": incomplete_evidence,
        "scored_runs": len(eligible_records),
        "tasks": task_count,
        "passed_runs": run_successes,
        "run_success_rate": run_successes / len(eligible_records) if eligible_records else 0.0,
        "run_success_wilson_95": {"low": low, "high": high},
        "pass_at_1": (sum(task["pass_at_1"] for task in tasks) / task_count if task_count else 0.0),
        "pass_at_k": (sum(task["pass_at_k"] for task in tasks) / task_count if task_count else 0.0),
        "all_at_k": (sum(task["all_at_k"] for task in tasks) / task_count if task_count else 0.0),
        "total_tokens": total_tokens,
        "estimated_cost_usd": sum(known_costs) if len(known_costs) == len(costs) else None,
        "duration_seconds": sum(record.duration_seconds for record in selected_records),
        "comparable": bool(selected_records) and all(record.comparable for record in selected_records),
        "task_results": tasks,
        "subject_results": subject_results,
        "capability_results": capability_results,
    }
