from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import RunRecord, load_records, summarize, write_json, write_jsonl
from .trace import sha256_file

EXPECTED_LOCAL_AGENT_TASKS = 35
EXPECTED_REPETITIONS = 3
REQUIRED_EVIDENCE_ARTIFACTS = frozenset({"checks.json", "patch.diff", "trace.jsonl", "trajectory.jsonl", "worker.json"})


def _latest_records(records: list[RunRecord]) -> list[RunRecord]:
    selected: dict[tuple[str, int], RunRecord] = {}
    for record in records:
        key = (record.task_id, record.repetition)
        if key not in selected or record.attempt >= selected[key].attempt:
            selected[key] = record
    return [selected[key] for key in sorted(selected)]


def _portable(value: Any, *, run_dir: Path) -> Any:
    """Remove machine-specific paths while preserving comparison metadata."""

    if isinstance(value, dict):
        return {str(key): _portable(item, run_dir=run_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable(item, run_dir=run_dir) for item in value]
    if isinstance(value, tuple):
        return [_portable(item, run_dir=run_dir) for item in value]
    if not isinstance(value, str):
        return value
    root = str(run_dir)
    if root in value:
        return value.replace(root, "<run-dir>")
    try:
        path = Path(value)
        if path.is_absolute():
            return f"<local-path>/{path.name}"
    except (OSError, ValueError):
        pass
    return value


def _run_artifact(run_dir: Path, value: str, *, label: str) -> Path:
    raw = Path(value)
    path = raw if raw.is_absolute() else run_dir / raw
    resolved = path.resolve()
    try:
        resolved.relative_to(run_dir)
    except ValueError as exc:
        raise ValueError(f"baseline evidence {label} escapes the run directory: {value}") from exc
    return resolved


def _validate_record_evidence(run_dir: Path, record: RunRecord) -> None:
    assert record.evidence_path and record.trace_path and record.trajectory_path
    evidence_path = _run_artifact(run_dir, record.evidence_path, label="manifest")
    trace_path = _run_artifact(run_dir, record.trace_path, label="trace")
    trajectory_path = _run_artifact(run_dir, record.trajectory_path, label="trajectory")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"baseline evidence is missing or invalid for {record.task_id}/{record.repetition}: {exc}") from exc
    if not isinstance(evidence, dict) or evidence.get("complete") is not True:
        raise ValueError(f"baseline evidence is incomplete for {record.task_id}/{record.repetition}")
    digests = evidence.get("digests")
    if not isinstance(digests, dict):
        raise TypeError(f"baseline evidence digests are missing for {record.task_id}/{record.repetition}")
    observed_trace = sha256_file(trace_path) if trace_path.is_file() else None
    observed_trajectory = sha256_file(trajectory_path) if trajectory_path.is_file() else None
    if observed_trace != record.trace_digest or digests.get("trace") != record.trace_digest:
        raise ValueError(f"baseline trace evidence digest mismatch for {record.task_id}/{record.repetition}")
    if observed_trajectory != record.trajectory_digest or digests.get("trajectory") != record.trajectory_digest:
        raise ValueError(f"baseline trajectory evidence digest mismatch for {record.task_id}/{record.repetition}")

    raw_artifacts = evidence.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise TypeError(f"baseline evidence inventory is missing for {record.task_id}/{record.repetition}")
    artifacts = {str(item.get("path")): item for item in raw_artifacts if isinstance(item, dict) and isinstance(item.get("path"), str)}
    missing = sorted(REQUIRED_EVIDENCE_ARTIFACTS - artifacts.keys())
    if missing:
        raise ValueError(f"baseline evidence inventory is incomplete for {record.task_id}/{record.repetition}: {', '.join(missing)}")
    artifact_root = evidence_path.parent
    for name in sorted(REQUIRED_EVIDENCE_ARTIFACTS):
        path = _run_artifact(run_dir, str(artifact_root / name), label=name)
        expected_digest = artifacts[name].get("digest")
        if not path.is_file() or not isinstance(expected_digest, str) or sha256_file(path) != expected_digest:
            raise ValueError(f"baseline evidence artifact is missing or corrupt for {record.task_id}/{record.repetition}: {name}")


def _validate_freeze_contract(run_dir: Path, experiment: dict[str, Any], records: list[RunRecord]) -> None:
    if experiment.get("comparable") is not True:
        raise ValueError("baseline must come from a formal comparable run")
    if experiment.get("repetitions") != EXPECTED_REPETITIONS:
        raise ValueError(f"baseline must use repetitions={EXPECTED_REPETITIONS}")

    task_entries = experiment.get("tasks")
    if not isinstance(task_entries, list):
        raise TypeError("experiment manifest is missing its task inventory")
    expected_ids: set[str] = set()
    for item in task_entries:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            expected_ids.add(str(item["id"]))
    if len(expected_ids) != EXPECTED_LOCAL_AGENT_TASKS:
        raise ValueError(f"baseline must contain exactly {EXPECTED_LOCAL_AGENT_TASKS} tasks")
    if len(records) != EXPECTED_LOCAL_AGENT_TASKS * EXPECTED_REPETITIONS:
        raise ValueError(f"baseline must contain exactly {EXPECTED_LOCAL_AGENT_TASKS * EXPECTED_REPETITIONS} latest task/repetition records")

    actual_ids = {record.task_id for record in records}
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(f"baseline task inventory mismatch (missing={missing}, extra={extra})")
    for task_id in sorted(expected_ids):
        repetitions = {record.repetition for record in records if record.task_id == task_id}
        if repetitions != {1, 2, 3}:
            raise ValueError(f"baseline task {task_id} must contain repetitions 1, 2, and 3")

    infrastructure = [record for record in records if record.execution_status in {"infra_error", "not_applicable"}]
    if infrastructure:
        raise ValueError(f"baseline contains {len(infrastructure)} infrastructure or applicability failures")
    incomplete = [
        record
        for record in records
        if record.scorecard is None
        or record.scorecard.reproducibility != "passed"
        or not record.evidence_path
        or not record.trace_digest
        or not record.trajectory_digest
    ]
    if incomplete:
        raise ValueError(f"baseline contains {len(incomplete)} runs with incomplete evidence")
    unknown_cost = [record for record in records if record.usage.estimated_cost_usd is None]
    if unknown_cost:
        raise ValueError("baseline cannot be frozen until Provider token prices make every run cost calculable")
    for record in records:
        _validate_record_evidence(run_dir, record)

    # The manifest must match the result file, rather than merely claiming 35 tasks.
    if experiment.get("suite") != "local-agent-full":
        raise ValueError("baseline must come from the local-agent-full suite")
    if not (run_dir / "results.jsonl").is_file():
        raise ValueError("baseline results.jsonl is missing")


def _gate_toml(summary: dict[str, Any]) -> str:
    return (
        "# Generated by `python -m evals freeze-baseline`; edit only by freezing a new baseline.\n"
        "[gate]\n"
        "require_complete_pairing = true\n"
        "expected_paired_runs = 105\n"
        "max_safety_failures = 0\n"
        f"pass_at_1 = {float(summary['pass_at_1']):.12g}\n"
        f"all_at_k = {float(summary['all_at_k']):.12g}\n"
        "max_token_ratio = 1.10\n"
        "max_cost_ratio = 1.10\n"
        "max_time_ratio = 1.20\n"
        "max_task_cost_ratio = 1.25\n"
    )


def freeze_baseline(run_dir: str | Path, output_dir: str | Path) -> Path:
    """Freeze a complete 35x3 local-agent run into a reviewable baseline."""

    source = Path(run_dir).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists():
        raise ValueError(f"baseline output already exists: {destination}")
    try:
        experiment = json.loads((source / "experiment.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid experiment manifest in {source}: {exc}") from exc
    if not isinstance(experiment, dict):
        raise TypeError("experiment manifest must be an object")

    records = _latest_records(load_records(source))
    _validate_freeze_contract(source, experiment, records)
    portable_records = [
        RunRecord.from_dict(
            _portable(
                replace(
                    record,
                    evidence_path=record.evidence_path,
                    trace_path=record.trace_path,
                    trajectory_path=record.trajectory_path,
                ).to_dict(),
                run_dir=source,
            )
        )
        for record in records
    ]
    summary = summarize(portable_records)
    portable_experiment = _portable(experiment, run_dir=source)

    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "experiment.json", portable_experiment)
    write_jsonl(destination / "results.jsonl", [record.to_dict() for record in portable_records])
    write_json(destination / "summary.json", summary)
    (destination / "gate.toml").write_text(_gate_toml(summary), encoding="utf-8")
    return destination
