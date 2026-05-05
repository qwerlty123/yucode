from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any

from .adapters import AgentAdapter, AgentOutcome, CommandAdapter, YucodeAdapter
from .executors import GradeOutcome
from .schema import TaskSpec


class DockerError(RuntimeError):
    pass


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _docker(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["docker", *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerError("Docker CLI is not installed") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DockerError(f"docker {' '.join(args[:2])} failed: {detail}")
    return completed


class NetworkLease:
    def __init__(self, mode: str, allowed_hosts: tuple[str, ...], labels: list[str] | None = None):
        self.mode = mode
        self.allowed_hosts = allowed_hosts
        self.labels = labels or []
        self.network_name: str | None = None
        self.proxy_name: str | None = None

    def __enter__(self) -> list[str]:
        if self.mode == "full":
            return []
        if self.mode == "offline":
            return ["--network", "none"]
        if not self.allowed_hosts:
            raise DockerError("provider-only network requires a provider URL with a hostname")
        self.network_name = _slug("yucode-eval-net")
        self.proxy_name = _slug("yucode-eval-proxy")
        _docker(["network", "create", "--internal", *self.labels, self.network_name])
        proxy_script = Path(__file__).with_name("proxy.py").resolve()
        try:
            _docker(
                [
                    "run",
                    "--detach",
                    "--name",
                    self.proxy_name,
                    *self.labels,
                    "--network",
                    "bridge",
                    "--volume",
                    f"{proxy_script}:/proxy.py:ro",
                    "python:3.11-alpine",
                    "python",
                    "/proxy.py",
                    *(argument for host in self.allowed_hosts for argument in ("--allow", host)),
                ]
            )
            _docker(
                [
                    "network",
                    "connect",
                    "--alias",
                    "proxy",
                    self.network_name,
                    self.proxy_name,
                ]
            )
            for _attempt in range(50):
                logs = _docker(["logs", self.proxy_name], check=False)
                if "READY" in logs.stdout:
                    break
                time.sleep(0.1)
            else:
                raise DockerError("provider allowlist proxy did not become ready")
        except Exception:
            self.__exit__(None, None, None)
            raise
        return [
            "--network",
            self.network_name,
            "--env",
            "HTTP_PROXY=http://proxy:8080",
            "--env",
            "HTTPS_PROXY=http://proxy:8080",
            "--env",
            "http_proxy=http://proxy:8080",
            "--env",
            "https_proxy=http://proxy:8080",
            "--env",
            "NO_PROXY=localhost,127.0.0.1",
        ]

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self.proxy_name:
            _docker(["rm", "--force", self.proxy_name], check=False)
        if self.network_name:
            _docker(["network", "rm", self.network_name], check=False)


class DockerExecutor:
    """Formal evaluator: agent and hidden grader run in separate containers."""

    comparable = True

    def __init__(self, adapter: AgentAdapter):
        self.adapter = adapter
        self.repo_root = Path(__file__).resolve().parents[1]
        self._task_images: dict[str, str] = {}
        self._agent_images: dict[str, str] = {}
        self._digests: dict[str, str | None] = {}
        self._image_lock = threading.RLock()
        self.experiment_id: str | None = None
        self._check_daemon()

    def bind_experiment(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        stale = _docker(
            ["ps", "--all", "--quiet", "--filter", f"label=yucode.eval.experiment={experiment_id}"],
            check=False,
        )
        for container_id in stale.stdout.splitlines():
            if container_id.strip():
                _docker(["rm", "--force", container_id.strip()], check=False)
        networks = _docker(
            ["network", "ls", "--quiet", "--filter", f"label=yucode.eval.experiment={experiment_id}"],
            check=False,
        )
        for network_id in networks.stdout.splitlines():
            if network_id.strip():
                _docker(["network", "rm", network_id.strip()], check=False)

    def _labels(self, task: TaskSpec, artifact_dir: Path) -> list[str]:
        labels: list[str] = []
        if self.experiment_id:
            labels.extend(["--label", f"yucode.eval.experiment={self.experiment_id}"])
        labels.extend(["--label", f"yucode.eval.task={task.id}"])
        labels.extend(["--label", f"yucode.eval.attempt={artifact_dir.name}"])
        return labels

    @staticmethod
    def _resource_args(task: TaskSpec) -> list[str]:
        args: list[str] = []
        if task.limits.memory:
            args.extend(["--memory", task.limits.memory])
        if task.limits.cpus is not None:
            args.extend(["--cpus", str(task.limits.cpus)])
        if task.limits.pids is not None:
            args.extend(["--pids-limit", str(task.limits.pids)])
        return args

    def _check_daemon(self) -> None:
        completed = _docker(["info", "--format", "{{.ServerVersion}}"], check=False)
        if completed.returncode != 0:
            raise DockerError("Docker daemon is not available; start Docker Desktop or use --local for a non-comparable debug run")

    def task_image(self, task: TaskSpec) -> str:
        cached = self._task_images.get(task.id)
        if cached:
            return cached
        environment = task.environment
        if environment.image:
            image = environment.image
            if _docker(["image", "inspect", image], check=False).returncode != 0:
                pull_args = ["pull"]
                if environment.platform:
                    pull_args.extend(["--platform", environment.platform])
                pull_args.append(image)
                _docker(pull_args)
        else:
            assert environment.dockerfile is not None and environment.context is not None
            fingerprint = hashlib.sha256()
            fingerprint.update(environment.dockerfile.read_bytes())
            for path in sorted(item for item in environment.context.rglob("*") if item.is_file()):
                fingerprint.update(path.relative_to(environment.context).as_posix().encode())
                fingerprint.update(path.read_bytes())
            image = f"yucode-eval-task:{fingerprint.hexdigest()[:20]}"
            inspect = _docker(["image", "inspect", image], check=False)
            if inspect.returncode != 0:
                args = ["build", "--tag", image, "--file", str(environment.dockerfile)]
                if environment.platform:
                    args.extend(["--platform", environment.platform])
                args.append(str(environment.context))
                _docker(args)
        self._task_images[task.id] = image
        return image

    def _agent_image(self, task: TaskSpec, adapter: AgentAdapter | None = None) -> str:
        base_image = self.task_image(task)
        effective_adapter = adapter or self.adapter
        if not isinstance(effective_adapter, YucodeAdapter):
            return base_image
        cached = self._agent_images.get(base_image)
        if cached:
            return cached
        fingerprint = hashlib.sha256(base_image.encode())
        fingerprint.update((self.image_digest(base_image) or "unknown").encode())
        for relative in ("pyproject.toml", "README.md"):
            fingerprint.update((self.repo_root / relative).read_bytes())
        for path in sorted((self.repo_root / "yucode").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                fingerprint.update(path.relative_to(self.repo_root).as_posix().encode())
                fingerprint.update(path.read_bytes())
        fingerprint.update((Path(__file__).with_name("worker.py")).read_bytes())
        image = f"yucode-eval-agent:{fingerprint.hexdigest()[:20]}"
        if _docker(["image", "inspect", image], check=False).returncode == 0:
            self._agent_images[base_image] = image
            return image

        with tempfile.TemporaryDirectory(prefix="yucode-eval-overlay-") as temporary:
            context = Path(temporary)
            for relative in ("pyproject.toml", "README.md"):
                shutil.copy2(self.repo_root / relative, context / relative)
            shutil.copytree(
                self.repo_root / "yucode",
                context / "yucode",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            evals_dir = context / "evals"
            evals_dir.mkdir()
            (evals_dir / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy2(Path(__file__).with_name("worker.py"), evals_dir / "worker.py")
            shutil.copy2(
                Path(__file__).parent / "docker" / "yucode.Dockerfile",
                context / "Dockerfile",
            )
            args = [
                "build",
                "--tag",
                image,
                "--build-arg",
                f"BASE_IMAGE={base_image}",
                "--file",
                str(context / "Dockerfile"),
                str(context),
            ]
            if task.environment.platform:
                args[1:1] = ["--platform", task.environment.platform]
            _docker(args)
        self._agent_images[base_image] = image
        return image

    def image_digest(self, image: str | None) -> str | None:
        if not image:
            return None
        if image in self._digests:
            return self._digests[image]
        completed = _docker(
            ["image", "inspect", "--format", "{{json .RepoDigests}}|{{.Id}}", image],
            check=False,
        )
        digest = completed.stdout.strip() or None if completed.returncode == 0 else None
        self._digests[image] = digest
        return digest

    def _container_run(
        self,
        args: list[str],
        *,
        name: str,
        input_text: str | None,
        timeout_seconds: int,
    ) -> tuple[int, str, str, bool, float]:
        started = time.monotonic()
        interactive = ["--interactive"] if input_text is not None else []
        process = subprocess.Popen(
            ["docker", "run", "--rm", *interactive, "--name", name, *args],
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
            return process.returncode, stdout, stderr, False, time.monotonic() - started
        except subprocess.TimeoutExpired:
            _docker(["rm", "--force", name], check=False)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            return 124, stdout, stderr, True, time.monotonic() - started

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
        with self._image_lock:
            image = self._agent_image(task, adapter)
        network_mode = task.environment.network or "provider-only"
        allowed_hosts: tuple[str, ...] = ()
        input_text: str | None = None
        command: list[str]
        if isinstance(adapter, YucodeAdapter):
            allowed_hosts = adapter.provider_hosts()
            payload = {
                "workspace": task.environment.workdir,
                "artifact_dir": "/artifacts",
                "prompt": prompt,
                "max_steps": max_steps,
                **adapter.worker_payload(),
            }
            input_text = json.dumps(payload, ensure_ascii=False)
            command = [image, "python", "-m", "evals.worker"]
        elif isinstance(adapter, CommandAdapter):
            prompt_path = artifact_dir / "prompt.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            replacements = {
                "workspace": task.environment.workdir,
                "prompt_file": "/artifacts/prompt.md",
                "artifact_dir": "/artifacts",
            }
            command = [image, *[part.format_map(replacements) for part in adapter.command]]
        else:
            raise DockerError(f"adapter does not support Docker execution: {adapter.name}")

        name = _slug("yucode-eval-agent")
        labels = self._labels(task, artifact_dir)
        with NetworkLease(network_mode, allowed_hosts, labels) as network_args:
            args = [
                *network_args,
                *labels,
                *self._resource_args(task),
                *(["--platform", task.environment.platform] if task.environment.platform else []),
                "--volume",
                f"{workspace}:{task.environment.workdir}:rw",
                "--volume",
                f"{artifact_dir}:/artifacts:rw",
                "--workdir",
                task.environment.workdir,
                *command,
            ]
            returncode, stdout, stderr, timed_out, duration = self._container_run(
                args,
                name=name,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
            )
        (artifact_dir / "agent.stdout.log").write_text(stdout, encoding="utf-8")
        (artifact_dir / "agent.stderr.log").write_text(stderr, encoding="utf-8")
        metrics: dict[str, Any] = {}
        worker_path = artifact_dir / "worker.json"
        if worker_path.is_file():
            try:
                value = json.loads(worker_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    metrics = value
            except (OSError, json.JSONDecodeError):
                pass
        metrics["execution"] = {
            "image": image,
            "image_digest": self.image_digest(image),
            "network": network_mode,
        }
        error = None
        if timed_out:
            error = f"agent exceeded {timeout_seconds}s wall-time budget"
        elif returncode != 0:
            error = str(metrics.get("error") or f"agent container exited {returncode}")
        return AgentOutcome(
            returncode=returncode,
            duration_seconds=duration,
            timed_out=timed_out,
            error=error,
            metrics=metrics,
        )

    def grade(
        self,
        task: TaskSpec,
        *,
        workspace: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> GradeOutcome:
        image = self.task_image(task)
        output_dir = artifact_dir / "grader_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        replacements = {
            "workspace": task.environment.workdir,
            "grader": "/grader",
            "output": "/grader-output",
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
        name = _slug("yucode-eval-grader")
        args = [
            "--network",
            "none",
            *self._labels(task, artifact_dir),
            *self._resource_args(task),
            *(["--platform", task.environment.platform] if task.environment.platform else []),
            "--volume",
            f"{workspace}:{task.environment.workdir}:rw",
            "--volume",
            f"{task.grader.path}:/grader:ro",
            "--volume",
            f"{output_dir}:/grader-output:rw",
            "--env",
            f"YUCODE_EVAL_WORKSPACE={task.environment.workdir}",
            "--env",
            "YUCODE_EVAL_GRADER=/grader",
            "--env",
            "YUCODE_EVAL_OUTPUT=/grader-output",
            "--workdir",
            task.environment.workdir,
            image,
            *command,
        ]
        returncode, stdout, stderr, timed_out, duration = self._container_run(args, name=name, input_text=None, timeout_seconds=timeout_seconds)
        (artifact_dir / "grader.log").write_text(
            stdout + ("\n--- stderr ---\n" if stderr else "") + stderr,
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
            passed=returncode == 0 and not timed_out,
            returncode=returncode,
            duration_seconds=duration,
            timed_out=timed_out,
            error=(
                f"grader exceeded {timeout_seconds}s wall-time budget" if timed_out else None if returncode == 0 else f"grader container exited {returncode}"
            ),
            details=details,
        )
