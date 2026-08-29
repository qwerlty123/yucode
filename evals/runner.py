from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yucode import __version__ as yucode_version

from .adapters import AgentAdapter, AgentOutcome
from .checks import SafetyContext, evaluate_safety, snapshot_safety_state
from .executors import Executor, GradeOutcome
from .models import (
    CheckOutcome,
    EvidenceManifest,
    FailureReason,
    RunRecord,
    Score,
    ScoreCard,
    UsageMetrics,
    evaluate_contract,
    primary_failure,
    write_json,
    write_jsonl,
)
from .report import write_report
from .schema import SuiteSpec, TaskSpec, catalog_digest, evaluate_applicability
from .store import AttemptLease, RunStore
from .trace import TraceRecorder, build_evidence_manifest, package_versions, sha256_bytes, sha256_file, sha256_tree, write_evidence
from .trajectory import evaluate_scenario, load_scenario, load_trajectory


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def experiment_id(suite_name: str, agent_name: str) -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    safe_suite = "".join(char if char.isalnum() or char in "-_" else "-" for char in suite_name).strip("-")
    return f"{stamp}-{safe_suite or 'suite'}-{agent_name}"


def _run_git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def prepare_source(task: TaskSpec, destination: Path) -> str:
    if task.source.type == "local":
        assert task.source.path is not None
        source_root = task.source.path.resolve()
        for path in source_root.rglob("*"):
            if not path.is_symlink():
                continue
            try:
                path.resolve().relative_to(source_root)
            except ValueError as exc:
                raise RuntimeError(f"source symlink escapes task source: {path}") from exc
        shutil.copytree(
            task.source.path,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        source_revision = hashlib.sha256()
        for path in sorted(destination.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            source_revision.update(path.relative_to(destination).as_posix().encode())
            source_revision.update(b"\0")
            source_revision.update(("symlink:" + os.readlink(path)).encode() if path.is_symlink() else path.read_bytes())
            source_revision.update(b"\0")
        revision = "sha256:" + source_revision.hexdigest()
    else:
        assert task.source.url is not None and task.source.revision is not None
        subprocess.run(
            ["git", "clone", "--no-checkout", "--quiet", task.source.url, str(destination)],
            text=True,
            capture_output=True,
            check=True,
        )
        _run_git(["checkout", "--quiet", "--detach", task.source.revision], cwd=destination)
        revision = _run_git(["rev-parse", "HEAD"], cwd=destination).stdout.strip()
        shutil.rmtree(destination / ".git")

    _run_git(["init", "--quiet"], cwd=destination)
    _run_git(["config", "user.email", "eval@yucode.local"], cwd=destination)
    _run_git(["config", "user.name", "yucode eval"], cwd=destination)
    _run_git(["add", "--all"], cwd=destination)
    _run_git(["commit", "--quiet", "-m", "evaluation baseline"], cwd=destination)
    return revision


def capture_patch(workspace: Path, baseline: str | None = None) -> tuple[str, str]:
    if baseline is None:
        baseline = _run_git(["rev-list", "--max-parents=0", "HEAD"], cwd=workspace).stdout.splitlines()[0]
    _run_git(["add", "--all"], cwd=workspace)
    patch = _run_git(
        ["diff", "--cached", "--binary", "--full-index", "--no-color", baseline],
        cwd=workspace,
    ).stdout
    return baseline, patch


def apply_patch(workspace: Path, patch: str) -> None:
    if not patch:
        return
    completed = subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
        cwd=workspace,
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not apply agent patch: {completed.stderr.strip()}")


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_baselines(
    suite: SuiteSpec,
    executor: Executor,
    output_dir: Path,
    *,
    tasks: tuple[TaskSpec, ...] | None = None,
) -> dict[str, str]:
    """Validate base-fails/gold-passes contracts without invoking an Agent."""

    errors: dict[str, str] = {}
    selected = suite.tasks if tasks is None else tasks
    for task in selected:
        if not task.grader.base_must_fail and task.grader.gold_patch is None:
            continue
        try:
            if task.grader.base_must_fail:
                artifact_dir = output_dir / "baseline_checks" / task.id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="yucode-eval-base-") as temporary:
                    workspace = Path(temporary) / "workspace"
                    prepare_source(task, workspace)
                    outcome = executor.grade(
                        task,
                        workspace=workspace,
                        artifact_dir=artifact_dir,
                        timeout_seconds=suite.defaults.grader_timeout_seconds,
                    )
                if outcome.passed:
                    errors[task.id] = "invalid benchmark: hidden grader passes on the unchanged baseline"
                elif outcome.timed_out:
                    errors[task.id] = outcome.error or "baseline grader timed out"
            if task.id in errors or task.grader.gold_patch is None:
                continue
            artifact_dir = output_dir / "gold_checks" / task.id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="yucode-eval-gold-") as temporary:
                workspace = Path(temporary) / "workspace"
                prepare_source(task, workspace)
                apply_patch(workspace, task.grader.gold_patch.read_text(encoding="utf-8"))
                outcome = executor.grade(
                    task,
                    workspace=workspace,
                    artifact_dir=artifact_dir,
                    timeout_seconds=suite.defaults.grader_timeout_seconds,
                )
            if not outcome.passed:
                errors[task.id] = outcome.error or "invalid benchmark: gold patch does not pass the hidden grader"
        except Exception as exc:  # noqa: BLE001 - every task must report its own invalid contract
            errors[task.id] = f"benchmark validation failed: {exc}"
    return errors


class EvaluationRunner:
    def __init__(
        self,
        suite: SuiteSpec,
        adapter: AgentAdapter,
        executor: Executor,
        *,
        output_dir: Path,
        repetitions: int | None = None,
        selected_tasks: set[str] | None = None,
        jobs: int | None = None,
        resume: bool = False,
        experiment_metadata: dict[str, Any] | None = None,
    ):
        self.suite = suite
        self.adapter = adapter
        self.executor = executor
        self.output_dir = output_dir.resolve()
        self.repetitions = suite.defaults.repetitions if repetitions is None else repetitions
        self.jobs = suite.defaults.jobs if jobs is None else jobs
        self.resume_requested = resume
        self.extra_experiment_metadata = dict(experiment_metadata or {})
        if self.repetitions <= 0:
            raise ValueError("repetitions must be positive")
        if self.jobs <= 0:
            raise ValueError("jobs must be positive")
        self.tasks = tuple(task for task in suite.tasks if selected_tasks is None or task.id in selected_tasks)
        for task in self.tasks:
            if task.source.type != "local" or task.source.path is None:
                continue
            try:
                self.output_dir.relative_to(task.source.path.resolve())
            except ValueError:
                continue
            raise ValueError(f"output directory must not be inside task source: {self.output_dir}")
        if selected_tasks:
            missing = selected_tasks - {task.id for task in self.tasks}
            if missing:
                raise ValueError(f"unknown task ids: {', '.join(sorted(missing))}")
        self.experiment_id = self.output_dir.name
        self.executor.bind_experiment(self.experiment_id)

    def run(self) -> list[RunRecord]:
        self.output_dir.mkdir(parents=True, exist_ok=self.resume_requested)
        metadata = self._experiment_metadata()
        experiment_path = self.output_dir / "experiment.json"
        if not self.resume_requested or not experiment_path.exists():
            write_json(experiment_path, metadata)
        store = RunStore(self.output_dir / "runs.sqlite3")
        suite_digest = sha256_file(self.suite.manifest_path)
        store.create_experiment(
            self.experiment_id,
            suite_digest=suite_digest,
            output_dir=self.output_dir,
            metadata=metadata,
            resume=self.resume_requested,
        )
        if self.resume_requested:
            store.recover_expired(self.experiment_id, force=True)
        store.enqueue(
            self.experiment_id,
            (
                (
                    task.id,
                    repetition,
                    {
                        "schema_version": task.schema_version,
                        "profile": task.profile,
                        "targets": list(task.targets),
                        "subject": (self.suite.catalog.capabilities[task.targets[0]].subject if self.suite.catalog and task.targets else "eval_harness"),
                        "category": task.category,
                        "release_eligible": task.release_eligible,
                    },
                )
                for task in self.tasks
                for repetition in range(1, self.repetitions + 1)
            ),
        )
        baseline_errors = self._validate_baselines()
        tasks_by_id = {task.id: task for task in self.tasks}

        def worker(position: int) -> None:
            worker_id = f"local-{os.getpid()}-{position}-{uuid.uuid4().hex[:8]}"
            while True:
                lease = store.claim(
                    self.experiment_id,
                    worker_id,
                    lease_seconds=self.suite.defaults.agent_timeout_seconds + self.suite.defaults.grader_timeout_seconds + 300,
                )
                if lease is None:
                    return
                task = tasks_by_id[lease.task_id]
                record = self._run_one(task, lease, baseline_errors.get(task.id), store)
                artifacts = record.metadata.get("artifacts", [])
                store.complete(lease.id, record, artifacts=artifacts if isinstance(artifacts, list) else [])

        with ThreadPoolExecutor(max_workers=self.jobs, thread_name_prefix="yucode-eval") as pool:
            list(pool.map(worker, range(self.jobs)))
        records = store.records(self.experiment_id)
        self._finalize_experiment_metadata(records, store)
        write_jsonl(self.output_dir / "results.jsonl", [record.to_dict() for record in records])
        write_report(self.output_dir)
        return records

    def _experiment_metadata(self) -> dict[str, Any]:
        from yucode.tools import TOOL_REGISTRY

        def task_metadata(task: TaskSpec) -> dict[str, Any]:
            profile = self.suite.catalog.profiles[task.profile] if self.suite.catalog is not None else None
            schemas = [TOOL_REGISTRY[name].schema(False) for name in task.allowed_tools if name in TOOL_REGISTRY]
            source_digest = task.source.expected_digest or (
                sha256_tree(task.source.path) if task.source.path is not None else sha256_bytes(str(task.source.revision).encode())
            )
            return {
                "id": task.id,
                "manifest_digest": sha256_file(task.manifest_path),
                "prompt_digest": sha256_file(task.prompt_path),
                "source_digest": source_digest,
                "grader_digest": sha256_tree(task.grader.path),
                "gold_digest": sha256_file(task.grader.gold_patch) if task.grader.gold_patch else None,
                "scenario_digest": sha256_file(task.scenario_path) if task.scenario_path else None,
                "profile": task.profile,
                "profile_digest": sha256_bytes(json.dumps(asdict(profile), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
                if profile is not None
                else None,
                "targets": list(task.targets),
                "allowed_tools": list(task.allowed_tools),
                "tool_schema_digest": sha256_bytes(json.dumps(schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
                "expected_artifact": task.expected_artifact,
                "success_policy": list(task.success_policy),
                "network": task.environment.network,
                "image": task.environment.image,
                "expected_image_digest": task.environment.expected_digest,
                "platform": task.environment.platform,
                "limits": asdict(task.limits),
            }

        metadata = {
            "schema_version": self.suite.schema_version,
            "experiment_id": self.experiment_id,
            "suite": self.suite.name,
            "suite_manifest": str(self.suite.manifest_path),
            "suite_digest": sha256_file(self.suite.manifest_path),
            "catalog_digest": catalog_digest(self.suite.catalog) if self.suite.catalog else None,
            "agent": self.adapter.name,
            "agent_config": self.adapter.public_metadata(),
            "yucode_version": yucode_version,
            "agent_code_digest": sha256_tree(Path(__file__).resolve().parents[1] / "yucode"),
            "harness_digest": sha256_tree(Path(__file__).resolve().parent),
            "pyproject_digest": sha256_file(Path(__file__).resolve().parents[1] / "pyproject.toml"),
            "packages": package_versions(),
            "mode": "formal" if self.executor.comparable else "local-debug",
            "comparable": self.executor.comparable,
            "repetitions": self.repetitions,
            "jobs": self.jobs,
            "tasks": [task_metadata(task) for task in self.tasks],
            "started_at": utc_now(),
            "defaults": asdict(self.suite.defaults),
        }
        if self.extra_experiment_metadata:
            metadata["matrix"] = self.extra_experiment_metadata
        return metadata

    def _finalize_experiment_metadata(self, records: list[RunRecord], store: RunStore) -> None:
        path = self.output_dir / "experiment.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        selected: dict[tuple[str, int], RunRecord] = {}
        for record in records:
            key = (record.task_id, record.repetition)
            if key not in selected or record.attempt >= selected[key].attempt:
                selected[key] = record
        tasks = metadata.get("tasks", [])
        if isinstance(tasks, list):
            for item in tasks:
                if not isinstance(item, dict):
                    continue
                task_records = [record for (task_id, _repetition), record in selected.items() if task_id == item.get("id")]
                item["observed_source_revisions"] = sorted({record.source_revision for record in task_records if record.source_revision})
                item["observed_images"] = sorted({record.image for record in task_records if record.image})
                item["observed_image_digests"] = sorted({record.image_digest for record in task_records if record.image_digest})
        metadata["finished_at"] = utc_now()
        write_json(path, metadata)
        store.update_experiment_metadata(self.experiment_id, metadata)

    def _validate_baselines(self) -> dict[str, str]:
        return validate_baselines(self.suite, self.executor, self.output_dir, tasks=self.tasks)

    def _run_one(
        self,
        task: TaskSpec,
        lease: AttemptLease,
        baseline_error: str | None,
        store: RunStore,
    ) -> RunRecord:
        sealed = self.output_dir / "runs" / task.id / str(lease.repetition) / f"attempt-{lease.attempt}" / "run.json"
        if lease.stage == "evidence_sealed" and sealed.is_file():
            try:
                value = json.loads(sealed.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return RunRecord.from_dict(value)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        return self._run_v2(task, lease, baseline_error, store)

    def _run_v2(
        self,
        task: TaskSpec,
        lease: AttemptLease,
        baseline_error: str | None,
        store: RunStore,
    ) -> RunRecord:
        assert self.suite.catalog is not None
        repetition = lease.repetition
        started_at = utc_now()
        started = time.monotonic()
        run_dir = self.output_dir / "runs" / task.id / str(repetition) / f"attempt-{lease.attempt}"
        run_dir.mkdir(parents=True, exist_ok=True)
        task_adapter = self.adapter.for_task(task)
        secrets = task_adapter.secret_values()
        trace = TraceRecorder(
            run_dir / "trace.jsonl",
            experiment_id=self.experiment_id,
            task_id=task.id,
            repetition=repetition,
            attempt=lease.attempt,
            secrets=secrets,
        )
        subject = self.suite.catalog.capabilities[task.targets[0]].subject
        store.stage(lease.id, "preflight")
        available = task_adapter.available_requirements(task)
        profile = self.suite.catalog.profiles[task.profile]
        if profile.driver != task_adapter.name:
            available[f"driver:{profile.driver}"] = False
            task = replace(task, requirements={**task.requirements, f"driver:{profile.driver}": True})
        decision = evaluate_applicability(task, self.suite.catalog, available)
        trace.emit(
            "applicability.checked",
            subject="eval_harness",
            stage="preflight",
            payload={"status": decision.status, "checked": list(decision.checked), "unmet": list(decision.unmet)},
        )
        if not decision.applicable:
            return self._not_applicable_record(task, lease, run_dir, started_at, started, subject, decision.unmet, trace, task_adapter, secrets, store)
        if baseline_error:
            trace.emit("infrastructure.failure", subject="eval_harness", stage="preflight", payload={"error": baseline_error})
            return self._v2_infra_record(task, lease, run_dir, started_at, started, subject, baseline_error, trace, task_adapter, secrets, store)

        patch_path = run_dir / "patch.diff"
        patch_checkpoint = lease.stage in {"patch_captured", "grader_running", "evidence_sealed"}
        if patch_checkpoint and not patch_path.is_file():
            message = f"checkpoint {lease.stage} is missing its immutable patch artifact"
            trace.emit("infrastructure.failure", subject="eval_harness", stage=lease.stage, payload={"error": message})
            return self._v2_infra_record(
                task,
                lease,
                run_dir,
                started_at,
                started,
                subject,
                message,
                trace,
                task_adapter,
                secrets,
                store,
            )
        if patch_checkpoint:
            required_checkpoint_artifacts = [run_dir / "agent-checkpoint.json", run_dir / "worker.json"]
            if task.scenario_path is not None:
                required_checkpoint_artifacts.extend([run_dir / "checks.json", run_dir / "trajectory.jsonl"])
            missing_checkpoint_artifacts = [path.name for path in required_checkpoint_artifacts if not path.is_file()]
            if missing_checkpoint_artifacts:
                message = "resume checkpoint is missing agent or trajectory/check artifacts: " + ", ".join(sorted(missing_checkpoint_artifacts))
                trace.emit("infrastructure.failure", subject="eval_harness", stage=lease.stage, payload={"error": message})
                return self._v2_infra_record(
                    task,
                    lease,
                    run_dir,
                    started_at,
                    started,
                    subject,
                    message,
                    trace,
                    task_adapter,
                    secrets,
                    store,
                    patch_bytes=patch_path.stat().st_size,
                )

        source_revision: str | None = None
        image: str | None = task.environment.image
        image_digest: str | None = None
        agent_outcome: AgentOutcome | None = None
        grade_outcome: GradeOutcome | None = None
        patch = ""
        expected_artifact_exists = False
        check_results: tuple[CheckOutcome, ...] = ()
        trajectory_digest: str | None = None
        resume_from_patch = lease.stage in {"patch_captured", "grader_running", "evidence_sealed"} and patch_path.is_file()
        try:
            with tempfile.TemporaryDirectory(prefix="yucode-eval-run-") as temporary:
                temporary_path = Path(temporary)
                trace.roots.update({"temporary": temporary_path, "run": run_dir})
                scenario_spec = load_scenario(task.scenario_path)
                if resume_from_patch:
                    patch = patch_path.read_text(encoding="utf-8")
                    worker_metrics: dict[str, Any] = {}
                    worker_path = run_dir / "worker.json"
                    if worker_path.is_file():
                        value = json.loads(worker_path.read_text(encoding="utf-8"))
                        if isinstance(value, dict):
                            worker_metrics = value
                    checkpoint: dict[str, Any] = {}
                    checkpoint_path = run_dir / "agent-checkpoint.json"
                    if checkpoint_path.is_file():
                        value = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                        if isinstance(value, dict):
                            checkpoint = value
                    agent_outcome = AgentOutcome(
                        returncode=int(checkpoint.get("returncode", 0)),
                        duration_seconds=float(checkpoint.get("duration_seconds", 0.0)),
                        timed_out=bool(checkpoint.get("timed_out", False)),
                        error=str(checkpoint["error"]) if checkpoint.get("error") else None,
                        metrics=worker_metrics,
                    )
                    checks_path = run_dir / "checks.json"
                    if checks_path.is_file():
                        value = json.loads(checks_path.read_text(encoding="utf-8"))
                        raw_results = value.get("results", []) if isinstance(value, dict) else []
                        if not isinstance(raw_results, list):
                            raise TypeError("checks.json results must be an array")
                        check_results = tuple(CheckOutcome(**item) for item in raw_results if isinstance(item, dict))
                    trajectory_path = run_dir / "trajectory.jsonl"
                    trajectory_digest = sha256_file(trajectory_path) if trajectory_path.is_file() else None
                    trace.emit(
                        "attempt.resumed",
                        subject="eval_harness",
                        stage="patch_captured",
                        payload={"from_stage": lease.stage, "model_reinvoked": False},
                    )
                else:
                    agent_workspace = temporary_path / "agent-workspace"
                    source_revision = prepare_source(task, agent_workspace)
                    if task.source.expected_digest and task.source.expected_digest != source_revision:
                        raise RuntimeError(f"source digest mismatch: expected {task.source.expected_digest}, observed {source_revision}")
                    store.stage(lease.id, "source_prepared")
                    trace.emit(
                        "source.prepared",
                        subject="eval_harness",
                        stage="source_prepared",
                        payload={"revision": source_revision},
                    )
                    baseline = _run_git(["rev-parse", "HEAD"], cwd=agent_workspace).stdout.strip()
                    before_state = snapshot_safety_state(scenario_spec.safety, agent_workspace)
                    prompt = task.prompt_path.read_text(encoding="utf-8")
                    store.stage(lease.id, "agent_running")
                    trace.emit(
                        "agent.started",
                        subject=subject,
                        stage="agent_running",
                        payload={"driver": task_adapter.name, "profile": task.profile, "allowed_tools": list(task.allowed_tools)},
                    )
                    outcome: AgentOutcome = self.executor.run_agent(
                        task_adapter,
                        workspace=agent_workspace,
                        artifact_dir=run_dir,
                        prompt=prompt,
                        timeout_seconds=task.limits.agent_timeout_seconds or self.suite.defaults.agent_timeout_seconds,
                        max_steps=task.limits.max_agent_steps or self.suite.defaults.max_steps,
                        task=task,
                    )
                    agent_outcome = outcome
                    trace.emit(
                        "agent.finished",
                        subject=subject,
                        stage="agent_running",
                        payload={
                            "returncode": outcome.returncode,
                            "timed_out": outcome.timed_out,
                            "max_steps_exhausted": bool(outcome.metrics.get("max_steps_exhausted")),
                        },
                    )
                    write_json(
                        run_dir / "agent-checkpoint.json",
                        {
                            "returncode": outcome.returncode,
                            "duration_seconds": outcome.duration_seconds,
                            "timed_out": outcome.timed_out,
                            "error": outcome.error,
                        },
                    )
                    expected_artifact_exists = bool(task.expected_artifact and (agent_workspace / task.expected_artifact).exists())
                    _baseline, patch = capture_patch(agent_workspace, baseline)
                    atomic_write_text(patch_path, patch)
                    trajectory_path = run_dir / "trajectory.jsonl"
                    trajectory_events = load_trajectory(trajectory_path)
                    trajectory_digest = sha256_file(trajectory_path) if trajectory_path.is_file() else None
                    worker_metrics = outcome.metrics
                    usage_for_checks = worker_metrics.get("usage", {})
                    if not isinstance(usage_for_checks, dict):
                        usage_for_checks = {}
                    worker_metrics["estimated_cost_usd"] = task_adapter.estimate_cost(usage_for_checks)
                    scenario_results = evaluate_scenario(
                        scenario_spec,
                        trajectory_events,
                        workspace=agent_workspace,
                        answer=str(worker_metrics.get("answer") or ""),
                        metrics={**worker_metrics, "total_tokens": int(usage_for_checks.get("total_tokens", 0))},
                    )
                    raw_interactions = worker_metrics.get("interaction_checks", [])
                    if not isinstance(raw_interactions, list):
                        raise TypeError("worker interaction_checks must be an array")
                    interaction_results = tuple(CheckOutcome(**item) for item in raw_interactions if isinstance(item, dict))
                    after_state = snapshot_safety_state(scenario_spec.safety, agent_workspace)
                    session_path = run_dir / "session.jsonl"
                    agent_log_path = run_dir / "agent.log"
                    safety_results = evaluate_safety(
                        task.safety_checks,
                        scenario_spec.safety,
                        SafetyContext(
                            workspace=agent_workspace,
                            run_dir=run_dir,
                            events=trajectory_events,
                            answer=str(worker_metrics.get("answer") or ""),
                            patch=patch,
                            session_text=session_path.read_text(encoding="utf-8") if session_path.is_file() else "",
                            agent_log=agent_log_path.read_text(encoding="utf-8") if agent_log_path.is_file() else "",
                            before_state=before_state,
                            after_state=after_state,
                            metrics=worker_metrics,
                        ),
                    )
                    check_results = (*scenario_results, *interaction_results, *safety_results)
                    write_json(
                        run_dir / "checks.json",
                        {"schema_version": 1, "passed": all(item.passed for item in check_results), "results": [asdict(item) for item in check_results]},
                    )
                    store.stage(lease.id, "patch_captured")
                    trace.emit(
                        "patch.captured",
                        subject="eval_harness",
                        stage="patch_captured",
                        payload={"bytes": len(patch.encode()), "digest": sha256_bytes(patch.encode())},
                    )

                grader_workspace = temporary_path / "grader-workspace"
                resumed_source_revision = prepare_source(task, grader_workspace)
                source_revision = source_revision or resumed_source_revision
                apply_patch(grader_workspace, patch)
                if resume_from_patch:
                    expected_artifact_exists = bool(task.expected_artifact and (grader_workspace / task.expected_artifact).exists())
                store.stage(lease.id, "grader_running")
                outcome_grade: GradeOutcome = self.executor.grade(
                    task,
                    workspace=grader_workspace,
                    artifact_dir=run_dir,
                    timeout_seconds=task.limits.grader_timeout_seconds or self.suite.defaults.grader_timeout_seconds,
                )
                grade_outcome = outcome_grade
                trace.emit(
                    "grader.finished",
                    subject="eval_harness",
                    stage="grader_running",
                    payload={
                        "exit_code": outcome_grade.returncode,
                        "timed_out": outcome_grade.timed_out,
                        "diagnostics": outcome_grade.details or {},
                    },
                )
                image = self.executor.task_image(task)
                image_digest = self.executor.image_digest(image)
                if task.environment.expected_digest and image_digest != task.environment.expected_digest:
                    raise RuntimeError(f"image digest mismatch: expected {task.environment.expected_digest}, observed {image_digest}")
        except Exception as exc:  # noqa: BLE001 - an individual attempt becomes infrastructure failure
            trace.emit("infrastructure.failure", subject="eval_harness", stage="execution", payload={"error": str(exc)})
            return self._v2_infra_record(
                task,
                lease,
                run_dir,
                started_at,
                started,
                subject,
                str(exc),
                trace,
                task_adapter,
                secrets,
                store,
                source_revision=source_revision,
                image=image,
                image_digest=image_digest,
                patch_bytes=len(patch.encode()),
            )

        assert agent_outcome is not None and grade_outcome is not None
        worker_metrics = agent_outcome.metrics
        usage_data = worker_metrics.get("usage", {})
        if not isinstance(usage_data, dict):
            usage_data = {}
        usage = UsageMetrics(**{key: value for key, value in usage_data.items() if key in UsageMetrics.__dataclass_fields__})
        usage.estimated_cost_usd = task_adapter.estimate_cost(usage_data)
        max_steps_exhausted = bool(worker_metrics.get("max_steps_exhausted"))
        budget_results: list[CheckOutcome] = []

        def budget(check_id: str, code: str, actual: int, maximum: int | None) -> None:
            if maximum is None:
                return
            budget_results.append(CheckOutcome(check_id, "budget", "budget", actual <= maximum, code, actual, f"<={maximum}", ("worker.json",)))

        budget("limit.tool_calls", "budget.tool_calls", int(worker_metrics.get("tool_calls", 0)), task.step_budget)
        budget("limit.model_calls", "budget.model_calls", usage.model_calls, task.limits.max_model_calls)
        budget("limit.tool_errors", "budget.tool_errors", int(worker_metrics.get("tool_errors", 0)), task.limits.max_tool_errors)
        budget("limit.total_tokens", "budget.tokens", usage.total_tokens, task.limits.max_total_tokens)
        check_results = (*check_results, *budget_results)
        within_budget = all(item.passed for item in budget_results) and not max_steps_exhausted
        normal_stop = agent_outcome.returncode == 0 and not agent_outcome.timed_out and not max_steps_exhausted
        protocol_results = [item for item in check_results if item.domain not in {"safety", "budget"}]
        safety_results = [item for item in check_results if item.domain == "safety"]
        protocol_scenario_passed = all(item.passed for item in protocol_results)
        safety_passed = all(item.passed for item in safety_results)
        scorecard, contract_failures = evaluate_contract(
            within_budget=within_budget,
            verifier_passed=grade_outcome.passed,
            expected_artifact_exists=expected_artifact_exists,
            normal_success_stop=normal_stop,
            safety_passed=safety_passed,
            evidence_refs={
                "artifact": ("patch.diff",),
                "budget": ("worker.json",),
                "verifier": ("grade.json", "grader.log"),
                "stop": ("worker.json",),
                "safety": ("checks.json", "trajectory.jsonl"),
            },
        )
        scorecard = replace(
            scorecard,
            protocol="failed" if not protocol_scenario_passed else scorecard.protocol,
            safety="passed" if safety_results and safety_passed else "failed" if safety_results else "not_checked",
            conditions={
                **scorecard.conditions,
                **({"trajectory_checks": protocol_scenario_passed} if protocol_results else {}),
                **({"safety_checks": safety_passed} if safety_results else {}),
            },
            efficiency=(
                Score("tools", subject, "observed", int(worker_metrics.get("tool_calls", 0)), task.step_budget, ("worker.json",)),
                Score("model_calls", subject, "observed", usage.model_calls, task.limits.max_model_calls, ("worker.json",)),
                Score("tokens", subject, "observed", usage.total_tokens, task.limits.max_total_tokens, ("worker.json",)),
                Score("cost_usd", subject, "observed", usage.estimated_cost_usd, None, ("worker.json",)),
            ),
        )
        failed_checks = [item for item in check_results if not item.passed]
        failures = [
            failure
            for failure in contract_failures
            if not (failure.code == "budget.exceeded" and any(item.domain == "budget" for item in failed_checks))
            and not (failure.code == "safety.violation" and any(item.domain == "safety" for item in failed_checks))
        ]
        failures.extend(
            FailureReason.create(
                item.code,
                "agent",
                "agent_mechanism" if item.domain in {"safety", "trajectory", "protocol"} else subject,
                f"check {item.id} failed: actual={item.actual!r}, expected={item.expected!r}",
                item.evidence_refs,
            )
            for item in failed_checks
        )
        if agent_outcome.timed_out:
            failures.append(FailureReason.create("agent.timeout", "agent", subject, agent_outcome.error or "agent timed out", ("agent.stderr.log",)))
            execution_status = "timed_out"
            status = "timeout"
        elif agent_outcome.returncode != 0:
            failures.append(FailureReason.create("agent.error", "agent", subject, agent_outcome.error or "agent failed", ("worker.json",)))
            execution_status = "agent_error"
            status = "agent_error"
        elif grade_outcome.timed_out:
            failures = [FailureReason.create("infra.grader_timeout", "grader", "eval_harness", grade_outcome.error or "grader timed out", ("grader.log",))]
            scorecard = replace(scorecard, functional="unknown", protocol="not_checked")
            execution_status = "infra_error"
            status = "infra_error"
        else:
            execution_status = "completed"
            passed_conditions = all(scorecard.conditions.values()) and scorecard.protocol != "failed" and scorecard.safety != "failed"
            status = "passed" if passed_conditions else "failed"
        passed = execution_status == "completed" and status == "passed"
        trace.emit(
            "score.finalized",
            subject=subject,
            stage="evidence_sealed",
            payload={
                "passed": passed,
                "execution": execution_status,
                "functional": scorecard.functional,
                "protocol": scorecard.protocol,
                "safety": scorecard.safety,
                "failure_codes": [failure.code for failure in failures],
            },
        )
        write_json(
            run_dir / "grade.json",
            {"passed": grade_outcome.passed, "exit_code": grade_outcome.returncode, "diagnostics": grade_outcome.details or {}},
        )
        trace_digest, semantic_hash = trace.seal()
        manifest = self._seal_evidence(
            task,
            lease,
            run_dir,
            task_adapter,
            secrets,
            source_revision=source_revision,
            image=image,
            image_digest=image_digest,
            trace_digest=trace_digest,
            trajectory_digest=trajectory_digest,
        )
        store.stage(lease.id, "evidence_sealed")
        scorecard = replace(scorecard, reproducibility="passed" if manifest.complete else "failed")
        primary = primary_failure(failures)
        record = RunRecord(
            schema_version=3,
            experiment_id=self.experiment_id,
            task_id=task.id,
            repetition=repetition,
            agent=task_adapter.name,
            status=status,
            passed=passed,
            comparable=self.executor.comparable,
            started_at=started_at,
            finished_at=utc_now(),
            duration_seconds=time.monotonic() - started,
            agent_duration_seconds=agent_outcome.duration_seconds,
            grader_duration_seconds=grade_outcome.duration_seconds,
            usage=usage,
            tool_calls=int(worker_metrics.get("tool_calls", 0)),
            tool_errors=int(worker_metrics.get("tool_errors", 0)),
            compactions=int(worker_metrics.get("compactions", 0)),
            retries=int(worker_metrics.get("retries", 0)),
            patch_bytes=len(patch.encode()),
            image=image,
            image_digest=image_digest,
            source_revision=source_revision,
            error=primary.message if primary else None,
            grader={"exit_code": grade_outcome.returncode, "diagnostics": grade_outcome.details or {}},
            metadata={
                "tags": list(task.tags),
                "difficulty": task.difficulty,
                "category": task.category,
                "network": task.environment.network,
                "release_eligible": task.release_eligible,
                "artifacts": list(manifest.artifacts),
                "tool_status_counts": worker_metrics.get("tool_status_counts", {}),
            },
            attempt=lease.attempt,
            execution_status=execution_status,
            functional_outcome=scorecard.functional,
            protocol_result=scorecard.protocol,
            safety_result=scorecard.safety,
            subject=subject,
            targets=task.targets,
            profile=task.profile,
            failures=tuple(failures),
            primary_failure=primary,
            scorecard=scorecard,
            evidence_path=str(run_dir / "evidence.json"),
            trace_path=str(trace.path),
            trace_digest=trace_digest,
            semantic_trace_hash=semantic_hash,
            trajectory_path=str(run_dir / "trajectory.jsonl") if (run_dir / "trajectory.jsonl").is_file() else None,
            trajectory_digest=trajectory_digest,
            check_results=check_results,
        )
        write_json(run_dir / "run.json", record.to_dict())
        return record

    def _seal_evidence(
        self,
        task: TaskSpec,
        lease: AttemptLease,
        run_dir: Path,
        adapter: AgentAdapter,
        secrets: tuple[str, ...],
        *,
        source_revision: str | None,
        image: str | None,
        image_digest: str | None,
        trace_digest: str,
        trajectory_digest: str | None = None,
    ) -> EvidenceManifest:
        assert self.suite.catalog is not None
        profile = self.suite.catalog.profiles[task.profile]
        profile_payload = json.dumps(asdict(profile), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        from yucode.tools import TOOL_REGISTRY

        schemas = [TOOL_REGISTRY[name].schema(False) for name in task.allowed_tools if name in TOOL_REGISTRY]
        tool_payload = json.dumps(schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        manifest = build_evidence_manifest(
            repo_root=Path(__file__).resolve().parents[1],
            run_dir=run_dir,
            experiment_id=self.experiment_id,
            task_id=task.id,
            repetition=lease.repetition,
            attempt=lease.attempt,
            digests={
                "suite": sha256_file(self.suite.manifest_path),
                "task": sha256_file(task.manifest_path),
                "prompt": sha256_file(task.prompt_path),
                "source": source_revision
                or task.source.expected_digest
                or (sha256_tree(task.source.path) if task.source.path else sha256_bytes(str(task.source.revision).encode())),
                "grader": sha256_tree(task.grader.path),
                "gold": sha256_file(task.grader.gold_patch) if task.grader.gold_patch else None,
                "agent_code": sha256_tree(Path(__file__).resolve().parents[1] / "yucode"),
                "harness": sha256_tree(Path(__file__).resolve().parent),
                "pyproject": sha256_file(Path(__file__).resolve().parents[1] / "pyproject.toml"),
                "catalog": catalog_digest(self.suite.catalog),
                "profile": sha256_bytes(profile_payload),
                "tool_schema": sha256_bytes(tool_payload),
                "docker_image": image_digest,
                "trace": trace_digest,
                "trajectory": trajectory_digest,
            },
            agent={**adapter.public_metadata(), "yucode_version": yucode_version},
            environment={
                "mode": "formal" if self.executor.comparable else "local-debug",
                "image": image,
                "image_digest": image_digest,
                "network": task.environment.network,
                "max_agent_steps": task.limits.max_agent_steps,
                "agent_timeout_seconds": task.limits.agent_timeout_seconds,
                "grader_timeout_seconds": task.limits.grader_timeout_seconds,
                "automatic_approval": bool(task.metadata.get("yolo", True)),
            },
            secrets=secrets,
        )
        write_evidence(run_dir / "evidence.json", manifest)
        return manifest

    def _not_applicable_record(
        self,
        task: TaskSpec,
        lease: AttemptLease,
        run_dir: Path,
        started_at: str,
        started: float,
        subject: str,
        unmet: tuple[str, ...],
        trace: TraceRecorder,
        adapter: AgentAdapter,
        secrets: tuple[str, ...],
        store: RunStore,
    ) -> RunRecord:
        trace_digest, semantic_hash = trace.seal()
        manifest = self._seal_evidence(
            task,
            lease,
            run_dir,
            adapter,
            secrets,
            source_revision=None,
            image=task.environment.image,
            image_digest=task.environment.expected_digest,
            trace_digest=trace_digest,
        )
        store.stage(lease.id, "evidence_sealed")
        scorecard = ScoreCard(
            applicability="not_applicable",
            reproducibility="passed" if manifest.complete else "failed",
        )
        record = RunRecord(
            schema_version=3,
            experiment_id=self.experiment_id,
            task_id=task.id,
            repetition=lease.repetition,
            agent=adapter.name,
            status="failed",
            passed=False,
            comparable=self.executor.comparable,
            started_at=started_at,
            finished_at=utc_now(),
            duration_seconds=time.monotonic() - started,
            error=f"not applicable: {', '.join(unmet)}",
            metadata={
                "category": task.category,
                "unmet_requirements": list(unmet),
                "release_eligible": task.release_eligible,
                "artifacts": list(manifest.artifacts),
            },
            attempt=lease.attempt,
            execution_status="not_applicable",
            functional_outcome="unknown",
            applicability="not_applicable",
            subject=subject,
            targets=task.targets,
            profile=task.profile,
            scorecard=scorecard,
            evidence_path=str(run_dir / "evidence.json"),
            trace_path=str(trace.path),
            trace_digest=trace_digest,
            semantic_trace_hash=semantic_hash,
        )
        write_json(run_dir / "run.json", record.to_dict())
        return record

    def _v2_infra_record(
        self,
        task: TaskSpec,
        lease: AttemptLease,
        run_dir: Path,
        started_at: str,
        started: float,
        subject: str,
        error: str,
        trace: TraceRecorder,
        adapter: AgentAdapter,
        secrets: tuple[str, ...],
        store: RunStore,
        *,
        source_revision: str | None = None,
        image: str | None = None,
        image_digest: str | None = None,
        patch_bytes: int = 0,
    ) -> RunRecord:
        trace_digest, semantic_hash = trace.seal()
        manifest = self._seal_evidence(
            task,
            lease,
            run_dir,
            adapter,
            secrets,
            source_revision=source_revision,
            image=image,
            image_digest=image_digest,
            trace_digest=trace_digest,
        )
        store.stage(lease.id, "evidence_sealed")
        failure = FailureReason.create("infra.execution", "runner", "eval_harness", error, ("trace.jsonl",))
        scorecard = ScoreCard(reproducibility="passed" if manifest.complete else "failed")
        record = RunRecord(
            schema_version=3,
            experiment_id=self.experiment_id,
            task_id=task.id,
            repetition=lease.repetition,
            agent=adapter.name,
            status="infra_error",
            passed=False,
            comparable=self.executor.comparable,
            started_at=started_at,
            finished_at=utc_now(),
            duration_seconds=time.monotonic() - started,
            patch_bytes=patch_bytes,
            image=image,
            image_digest=image_digest,
            source_revision=source_revision,
            error=error,
            metadata={"category": task.category, "release_eligible": task.release_eligible, "artifacts": list(manifest.artifacts)},
            attempt=lease.attempt,
            execution_status="infra_error",
            subject=subject,
            targets=task.targets,
            profile=task.profile,
            failures=(failure,),
            primary_failure=failure,
            scorecard=scorecard,
            evidence_path=str(run_dir / "evidence.json"),
            trace_path=str(trace.path),
            trace_digest=trace_digest,
            semantic_trace_hash=semantic_hash,
        )
        write_json(run_dir / "run.json", record.to_dict())
        return record
