from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .adapters import AgentAdapter, AgentOutcome
from .schema import TaskSpec


@dataclass
class GradeOutcome:
    passed: bool
    returncode: int
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None
    details: dict[str, Any] | None = None


class Executor(Protocol):
    """Execution boundary shared by local debugging and formal Docker runs."""

    comparable: bool

    def bind_experiment(self, experiment_id: str) -> None: ...

    def task_image(self, task: TaskSpec) -> str | None: ...

    def image_digest(self, image: str | None) -> str | None: ...

    def run_agent(
        self,
        adapter: AgentAdapter,
        *,
        workspace: Path,
        artifact_dir: Path,
        prompt: str,
        timeout_seconds: int,
        max_steps: int,
        task: TaskSpec,
    ) -> AgentOutcome: ...

    def grade(
        self,
        task: TaskSpec,
        *,
        workspace: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> GradeOutcome: ...


class LocalExecutor:
    """Unsafe local execution intended only for fast benchmark debugging."""

    comparable = False

    def bind_experiment(self, experiment_id: str) -> None:
        del experiment_id

    def task_image(self, task: TaskSpec) -> str | None:
        return task.environment.image

    def image_digest(self, image: str | None) -> str | None:
        del image
        return None

    def run_agent(
        self,
        adapter: AgentAdapter,
        *,
        workspace: Path,
        artifact_dir: Path,
        prompt: str,
        timeout_seconds: int,
        max_steps: int,
        task: TaskSpec,
    ) -> AgentOutcome:
        del task
        return adapter.run_local(
            workspace=workspace,
            artifact_dir=artifact_dir,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            max_steps=max_steps,
        )

    def grade(
        self,
        task: TaskSpec,
        *,
        workspace: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> GradeOutcome:
        output_dir = artifact_dir / "grader_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        replacements = {
            "workspace": str(workspace),
            "grader": str(task.grader.path),
            "output": str(output_dir),
        }
        try:
            command = [part.format_map(replacements) for part in task.grader.command]
        except KeyError as exc:
            return GradeOutcome(
                passed=False,
                returncode=2,
                duration_seconds=0.0,
                error=f"unknown grader command placeholder: {exc}",
            )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env={
                    **os.environ,
                    "YUCODE_EVAL_WORKSPACE": str(workspace),
                    "YUCODE_EVAL_GRADER": str(task.grader.path),
                    "YUCODE_EVAL_OUTPUT": str(output_dir),
                },
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            (artifact_dir / "grader.log").write_text(f"{exc.stdout or ''}\n{exc.stderr or ''}", encoding="utf-8")
            return GradeOutcome(
                passed=False,
                returncode=124,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
                error=f"grader exceeded {timeout_seconds}s wall-time budget",
            )
        (artifact_dir / "grader.log").write_text(
            completed.stdout + ("\n--- stderr ---\n" if completed.stderr else "") + completed.stderr,
            encoding="utf-8",
        )
        details: dict[str, Any] | None = None
        result_path = output_dir / task.grader.result_file
        if result_path.is_file():
            try:
                value = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    details = value
            except (OSError, json.JSONDecodeError):
                details = {"diagnostic": f"invalid JSON in {task.grader.result_file}"}
        return GradeOutcome(
            passed=completed.returncode == 0,
            returncode=completed.returncode,
            duration_seconds=time.monotonic() - started,
            error=(None if completed.returncode == 0 else f"grader exited {completed.returncode}"),
            details=details,
        )
