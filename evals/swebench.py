from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yucode import __version__ as yucode_version

from .adapters import AgentAdapter, AgentOutcome
from .docker import DockerExecutor
from .models import RunRecord, UsageMetrics, write_json, write_jsonl
from .report import write_report
from .runner import capture_patch, prepare_source, utc_now
from .schema import EnvironmentSpec, GraderSpec, SourceSpec, TaskSpec, load_catalog
from .store import RunStore


class SwebenchUnavailable(RuntimeError):
    pass


@dataclass
class PendingPrediction:
    attempt_id: int
    attempt_no: int
    task: TaskSpec
    repetition: int
    run_dir: Path
    started_at: str
    duration_seconds: float
    source_revision: str | None
    patch: str
    agent: AgentOutcome


def _load_api() -> tuple[Any, Any]:
    try:
        from swebench.harness.utils import load_swebench_dataset  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SwebenchUnavailable("SWE-bench support is not installed; run `uv sync --extra dev --extra eval`") from exc
    try:
        from swebench.harness.utils import make_test_spec  # pyright: ignore[reportMissingImports]
    except ImportError:
        try:
            from swebench.harness.test_spec.test_spec import make_test_spec  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise SwebenchUnavailable("the installed SWE-bench package has an unsupported harness API") from exc
    return load_swebench_dataset, make_test_spec


def _image_for(instance: dict[str, Any], make_test_spec: Any) -> str:
    image = instance.get("image")
    if isinstance(image, str) and image:
        return image
    spec = make_test_spec(instance, namespace="swebench")
    for attribute in ("image", "instance_image_key"):
        value = getattr(spec, attribute, None)
        if isinstance(value, str) and value:
            return value
    raise SwebenchUnavailable(f"SWE-bench did not provide an image for {instance.get('instance_id')}")


def _dataset_fingerprint(instances: list[dict[str, Any]]) -> str:
    stable = [
        {
            key: instance.get(key)
            for key in (
                "instance_id",
                "repo",
                "base_commit",
                "problem_statement",
                "version",
                "image",
            )
        }
        for instance in instances
    ]
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _official_result(directory: Path) -> tuple[set[str], dict[str, Any]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in directory.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and ("resolved_ids" in value or "resolved_instances" in value):
            candidates.append((path, value))
    if not candidates:
        raise RuntimeError("official SWE-bench harness produced no summary report")
    path, report = max(candidates, key=lambda item: item[0].stat().st_mtime)
    resolved_value = report.get("resolved_ids", [])
    resolved = {str(item) for item in resolved_value if isinstance(item, (str, int))}
    report = {**report, "_report_path": str(path)}
    return resolved, report


def run_swebench(
    *,
    adapter: AgentAdapter,
    config_path: str,
    output_dir: Path,
    dataset_name: str,
    split: str,
    instance_ids: list[str] | None,
    limit: int | None,
    repetitions: int,
    agent_timeout_seconds: int,
    grader_timeout_seconds: int,
    max_steps: int,
    max_workers: int,
    jobs: int = 1,
) -> list[RunRecord]:
    del config_path  # config is already captured by the adapter; never persist its secrets
    load_dataset, make_test_spec = _load_api()
    raw_instances = load_dataset(dataset_name, split, instance_ids)
    instances = [dict(item) for item in raw_instances]
    if instance_ids:
        wanted = set(instance_ids)
        instances = [item for item in instances if item.get("instance_id") in wanted]
        missing = wanted - {str(item.get("instance_id")) for item in instances}
        if missing:
            raise ValueError(f"unknown SWE-bench instance ids: {', '.join(sorted(missing))}")
    if limit is not None:
        instances = instances[:limit]
    if not instances:
        raise ValueError("no SWE-bench instances selected")

    executor = DockerExecutor(adapter)
    output_dir.mkdir(parents=True, exist_ok=False)
    experiment_id = output_dir.name
    package_version = importlib.metadata.version("swebench")
    write_json(
        output_dir / "experiment.json",
        {
            "schema_version": 1,
            "experiment_id": experiment_id,
            "suite": "swebench",
            "dataset": dataset_name,
            "split": split,
            "dataset_fingerprint": _dataset_fingerprint(instances),
            "swebench_version": package_version,
            "agent": adapter.name,
            "agent_config": adapter.public_metadata(),
            "yucode_version": yucode_version,
            "mode": "formal-official-harness",
            "comparable": True,
            "repetitions": repetitions,
            "tasks": [item["instance_id"] for item in instances],
            "started_at": utc_now(),
        },
    )
    executor.bind_experiment(experiment_id)
    tasks: dict[str, TaskSpec] = {}
    prompts: dict[str, str] = {}
    coding_tools = load_catalog().profiles["coding_default"].tools
    for instance in instances:
        task_id = str(instance["instance_id"])
        repo = str(instance["repo"])
        base_commit = str(instance["base_commit"])
        image = _image_for(instance, make_test_spec)
        prompt = str(instance["problem_statement"])
        task = TaskSpec(
            id=task_id,
            manifest_path=output_dir / "swebench.generated",
            prompt_path=output_dir / "swebench.prompt",
            source=SourceSpec(
                type="git",
                url=f"https://github.com/{repo}.git",
                revision=base_commit,
            ),
            environment=EnvironmentSpec(
                image=image,
                platform="linux/amd64",
                workdir="/testbed",
                network="provider-only",
            ),
            grader=GraderSpec(path=output_dir, command=("true",)),
            tags=("swebench", repo),
            difficulty=None,
            metadata={"dataset": dataset_name, "version": instance.get("version")},
            targets=("benchmark.swebench",),
            profile="swebench_official",
            category="coding",
            allowed_tools=coding_tools,
        )
        tasks[task_id] = task
        prompts[task_id] = prompt
        # Pull/inspect before spending a model call.
        executor.task_image(task)

    store = RunStore(output_dir / "runs.sqlite3")
    store.create_experiment(
        experiment_id,
        suite_digest=_dataset_fingerprint(instances),
        output_dir=output_dir,
        metadata={"suite": "swebench", "official_max_workers": max_workers, "inference_jobs": jobs},
    )
    store.enqueue(
        experiment_id,
        ((task_id, repetition, {"schema_version": 2, "profile": "swebench_official"}) for task_id in tasks for repetition in range(1, repetitions + 1)),
    )
    pending_by_repetition: dict[int, list[PendingPrediction]] = {repetition: [] for repetition in range(1, repetitions + 1)}
    pending_lock = threading.Lock()

    def inference_worker(position: int) -> None:
        worker_id = f"swebench-{position}-{uuid.uuid4().hex[:8]}"
        while True:
            lease = store.claim(
                experiment_id,
                worker_id,
                lease_seconds=agent_timeout_seconds + grader_timeout_seconds * max(1, len(tasks)) + 600,
            )
            if lease is None:
                return
            task = tasks[lease.task_id]
            run_dir = output_dir / "runs" / task.id / str(lease.repetition)
            run_dir.mkdir(parents=True, exist_ok=True)
            started_at = utc_now()
            started = time.monotonic()
            source_revision: str | None = None
            patch = ""
            store.stage(lease.id, "source_prepared")
            with tempfile.TemporaryDirectory(prefix="yucode-swebench-") as temporary:
                workspace = Path(temporary) / "workspace"
                try:
                    source_revision = prepare_source(task, workspace)
                    baseline = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=workspace,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip()
                    store.stage(lease.id, "agent_running")
                    agent_outcome = executor.run_agent(
                        adapter,
                        workspace=workspace,
                        artifact_dir=run_dir,
                        prompt=prompts[task.id],
                        timeout_seconds=agent_timeout_seconds,
                        max_steps=max_steps,
                        task=task,
                    )
                    _baseline, patch = capture_patch(workspace, baseline)
                except Exception as exc:  # noqa: BLE001 - preserve a record for failed inference
                    agent_outcome = AgentOutcome(returncode=1, duration_seconds=time.monotonic() - started, error=str(exc))
            (run_dir / "patch.diff").write_text(patch, encoding="utf-8")
            store.stage(lease.id, "patch_captured")
            item = PendingPrediction(
                attempt_id=lease.id,
                attempt_no=lease.attempt,
                task=task,
                repetition=lease.repetition,
                run_dir=run_dir,
                started_at=started_at,
                duration_seconds=time.monotonic() - started,
                source_revision=source_revision,
                patch=patch,
                agent=agent_outcome,
            )
            with pending_lock:
                pending_by_repetition[lease.repetition].append(item)

    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="yucode-swebench") as pool:
        list(pool.map(inference_worker, range(jobs)))

    records: list[RunRecord] = []
    for repetition, pending in pending_by_repetition.items():
        pending.sort(key=lambda item: item.task.id)
        official_dir = output_dir / "swebench_official" / f"repetition-{repetition}"
        official_dir.mkdir(parents=True, exist_ok=True)
        predictions_path = official_dir / "predictions.jsonl"
        with predictions_path.open("w", encoding="utf-8") as file:
            for item in pending:
                file.write(
                    json.dumps(
                        {
                            "instance_id": item.task.id,
                            "model_name_or_path": f"yucode-{experiment_id}-r{repetition}",
                            "model_patch": item.patch,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        run_id = f"{experiment_id}-r{repetition}"
        for item in pending:
            store.stage(item.attempt_id, "grader_running")
        command = [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            dataset_name,
            "--split",
            split,
            "--predictions_path",
            str(predictions_path),
            "--max_workers",
            str(max_workers),
            "--timeout",
            str(grader_timeout_seconds),
            "--run_id",
            run_id,
            "--report_dir",
            str(official_dir),
            "--instance_ids",
            *[item.task.id for item in pending],
        ]
        harness_started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=official_dir,
            text=True,
            capture_output=True,
            check=False,
        )
        (official_dir / "harness.log").write_text(
            completed.stdout + ("\n--- stderr ---\n" if completed.stderr else "") + completed.stderr,
            encoding="utf-8",
        )
        harness_duration = time.monotonic() - harness_started
        official_error: str | None = None
        resolved: set[str] = set()
        official_report: dict[str, Any] = {}
        if completed.returncode != 0:
            official_error = f"official SWE-bench harness exited {completed.returncode}"
        else:
            try:
                resolved, official_report = _official_result(official_dir)
            except RuntimeError as exc:
                official_error = str(exc)

        per_item_grade_duration = harness_duration / max(1, len(pending))
        for item in pending:
            metrics = item.agent.metrics
            usage_raw = metrics.get("usage", {})
            if not isinstance(usage_raw, dict):
                usage_raw = {}
            usage = UsageMetrics(**{key: value for key, value in usage_raw.items() if key in UsageMetrics.__dataclass_fields__})
            usage.estimated_cost_usd = adapter.estimate_cost(usage_raw)
            if item.agent.timed_out:
                status = "timeout"
                passed = False
                error = item.agent.error
            elif item.agent.returncode != 0:
                status = "agent_error"
                passed = False
                error = item.agent.error
            elif official_error:
                status = "infra_error"
                passed = False
                error = official_error
            else:
                passed = item.task.id in resolved
                status = "passed" if passed else "failed"
                error = None
            record = RunRecord(
                schema_version=2,
                experiment_id=experiment_id,
                task_id=item.task.id,
                repetition=item.repetition,
                agent=adapter.name,
                status=status,
                passed=passed,
                comparable=True,
                started_at=item.started_at,
                finished_at=utc_now(),
                duration_seconds=item.duration_seconds + per_item_grade_duration,
                agent_duration_seconds=item.agent.duration_seconds,
                grader_duration_seconds=per_item_grade_duration,
                usage=usage,
                tool_calls=int(metrics.get("tool_calls", 0)),
                tool_errors=int(metrics.get("tool_errors", 0)),
                compactions=int(metrics.get("compactions", 0)),
                retries=int(metrics.get("retries", 0)),
                patch_bytes=len(item.patch.encode()),
                image=executor.task_image(item.task),
                image_digest=executor.image_digest(executor.task_image(item.task)),
                source_revision=item.source_revision,
                error=error,
                grader={
                    "official": True,
                    "swebench_version": package_version,
                    "report": official_report,
                },
                metadata={
                    **item.task.metadata,
                    "network": item.task.environment.network,
                    "execution": metrics.get("execution", {}),
                    "official_max_workers": max_workers,
                    "inference_jobs": jobs,
                },
                attempt=item.attempt_no,
                targets=item.task.targets,
                profile=item.task.profile,
            )
            write_json(item.run_dir / "run.json", record.to_dict())
            store.complete(item.attempt_id, record)
            records.append(record)
    records = store.records(experiment_id)
    write_jsonl(output_dir / "results.jsonl", [record.to_dict() for record in records])
    write_report(output_dir)
    return records
