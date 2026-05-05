from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .schema import TaskSpec


@dataclass
class AgentOutcome:
    returncode: int
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(ABC):
    """Interface implemented by every evaluated coding agent."""

    name: str

    def public_metadata(self) -> dict[str, Any]:
        return {"kind": self.name}

    def available_requirements(self, task: TaskSpec) -> dict[str, bool]:
        del task
        metadata = self.public_metadata()
        return {"provider_configured": bool(metadata.get("url") and metadata.get("model"))}

    def secret_values(self) -> tuple[str, ...]:
        return ()

    def estimate_cost(self, usage: dict[str, Any]) -> float | None:
        del usage
        return None

    def for_task(self, task: TaskSpec) -> AgentAdapter:
        del task
        return self

    def worker_payload(self) -> dict[str, Any]:
        return {}

    @abstractmethod
    def run_local(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        prompt: str,
        timeout_seconds: int,
        max_steps: int,
    ) -> AgentOutcome:
        raise NotImplementedError


class YucodeAdapter(AgentAdapter):
    name = "yucode"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        profile: str = "coding_default",
        allowed_tools: tuple[str, ...] | None = None,
        yolo: bool = True,
        attachments: tuple[str, ...] = (),
        scenario: dict[str, Any] | None = None,
    ):
        self.config = config
        self.profile = profile
        self.allowed_tools = allowed_tools
        self.yolo = yolo
        self.attachments = attachments
        self.scenario = scenario or {}

    def for_task(self, task: TaskSpec) -> AgentAdapter:
        scenario: dict[str, Any] = {}
        scenario_path = task.scenario_path
        if scenario_path is not None:
            try:
                value = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid task scenario {scenario_path}: {exc}") from exc
            if not isinstance(value, dict):
                raise TypeError(f"task scenario must be an object: {scenario_path}")
            scenario = value
        return YucodeAdapter(
            self.config,
            profile=task.profile,
            allowed_tools=task.allowed_tools,
            yolo=bool(task.metadata.get("yolo", True)),
            attachments=task.attachments,
            scenario=scenario,
        )

    def _provider(self) -> tuple[str, dict[str, Any]]:
        root = self.config.get("provider", {})
        if not isinstance(root, dict):
            return "default", {}
        active = str(root.get("active", "default"))
        named = root.get(active)
        return (active, named) if isinstance(named, dict) else (active, root)

    def _provider_tables(self) -> dict[str, dict[str, Any]]:
        root = self.config.get("provider", {})
        if not isinstance(root, dict):
            return {}
        active = str(root.get("active", "default"))
        if isinstance(root.get(active), dict):
            return {str(name): value for name, value in root.items() if isinstance(value, dict) and {"url", "model", "key", "api"} & value.keys()}
        return {active: root}

    def provider_hosts(self) -> tuple[str, ...]:
        """Return the provider hosts used by this evaluation scenario."""

        active, _provider = self._provider()
        tables = self._provider_tables()
        selected = {active}
        rounds = self.scenario.get("rounds", [])
        if isinstance(rounds, list):
            for round_spec in rounds:
                if not isinstance(round_spec, dict):
                    continue
                provider_name = round_spec.get("provider")
                if provider_name == "@alternate":
                    provider_name = next((name for name in sorted(tables) if name != active), None)
                elif provider_name == "@active" or provider_name is None:
                    provider_name = active
                if isinstance(provider_name, str):
                    selected.add(provider_name)
        return tuple(
            sorted(
                {hostname for name in selected if (provider := tables.get(name)) is not None if (hostname := urlsplit(str(provider.get("url") or "")).hostname)}
            )
        )

    def public_metadata(self) -> dict[str, Any]:
        name, provider = self._provider()
        raw_url = str(provider.get("url") or "")
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{hostname}:{port}" if port is not None else hostname
        safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        strict_tools_active = False
        prompt_cache_supported = False
        provider_tools_supported = bool(provider.get("builtin_tools"))
        try:
            from yucode.base import Config
            from yucode.provider_compat import builtin_tools_issue

            parsed_config = Config.from_dict(self.config)
            resolved = parsed_config.provider.resolve()
            strict_tools_active = resolved.strict_tools_active
            prompt_cache_supported = resolved.prompt_cache_key
            provider_tools_supported = (
                bool(parsed_config.provider.builtin_tools) and builtin_tools_issue(resolved, parsed_config.provider.builtin_tools) is None
            )
        except (TypeError, ValueError):
            pass
        return {
            "kind": self.name,
            "provider": name,
            "url": safe_url,
            "model": provider.get("model"),
            "api": provider.get("api", "auto"),
            "stream": provider.get("stream", True),
            "reasoning": provider.get("reasoning", "medium"),
            "temperature": provider.get("temperature"),
            "max_tokens": provider.get("max_tokens", 0),
            "timeout": provider.get("timeout", 120),
            "response_timeout": provider.get("response_timeout", 600),
            "image_input": provider.get("image_input", "auto"),
            "strict_tools": strict_tools_active,
            "prompt_cache_key": provider.get("prompt_cache_key", "auto"),
            "prompt_cache_supported": prompt_cache_supported,
            "builtin_tool_types": sorted(str(item.get("type")) for item in provider.get("builtin_tools", []) if isinstance(item, dict) and item.get("type")),
            "provider_tools_supported": provider_tools_supported,
            "configured_providers": {
                provider_name: {
                    "url": urlunsplit(
                        (
                            parsed_url.scheme,
                            f"{parsed_url.hostname}:{parsed_url.port}" if parsed_url.hostname and parsed_url.port is not None else parsed_url.hostname or "",
                            parsed_url.path,
                            "",
                            "",
                        )
                    ),
                    "model": provider_config.get("model"),
                    "api": provider_config.get("api", "auto"),
                }
                for provider_name, provider_config in sorted(self._provider_tables().items())
                if (parsed_url := urlsplit(str(provider_config.get("url") or ""))).hostname
            },
        }

    def available_requirements(self, task: TaskSpec) -> dict[str, bool]:
        metadata = self.public_metadata()
        return {
            "provider_configured": bool(metadata.get("url") and metadata.get("model")),
            "code_index": task.profile == "coding_indexed",
            "image_input": bool(task.attachments) and metadata.get("image_input") != "off",
            "provider_tools": bool(metadata.get("provider_tools_supported")),
            "provider_switch": len(self._provider_tables()) >= 2,
            "strict_tools": bool(metadata.get("strict_tools")),
            "cache_telemetry": bool(metadata.get("prompt_cache_supported")) and metadata.get("prompt_cache_key") != "off",
        }

    def secret_values(self) -> tuple[str, ...]:
        values: list[str] = []

        def visit(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for name, item in value.items():
                    visit(item, str(name))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item, key)
            elif isinstance(value, str) and value and any(token in key.lower() for token in ("key", "token", "secret", "password")):
                values.append(value)

        visit(self.config)
        return tuple(values)

    def estimate_cost(self, usage: dict[str, Any]) -> float | None:
        eval_config = self.config.get("eval", {})
        pricing = eval_config.get("pricing", {}) if isinstance(eval_config, dict) else {}
        if not isinstance(pricing, dict):
            return None
        try:
            prompt_rate = float(pricing["prompt_per_million"])
            completion_rate = float(pricing["completion_per_million"])
            cached_read_rate = float(pricing.get("cached_read_per_million", prompt_rate))
            cached_write_rate = float(pricing.get("cached_write_per_million", prompt_rate))
        except (KeyError, TypeError, ValueError):
            return None
        if min(prompt_rate, completion_rate, cached_read_rate, cached_write_rate) < 0:
            return None
        prompt = int(usage.get("prompt_tokens", 0))
        cached_read = int(usage.get("cached_read_tokens", 0))
        cached_write = int(usage.get("cached_write_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        uncached = max(0, prompt - cached_read - cached_write)
        return (uncached * prompt_rate + cached_read * cached_read_rate + cached_write * cached_write_rate + completion * completion_rate) / 1_000_000

    def worker_payload(self) -> dict[str, Any]:
        return {
            "driver": "yucode",
            "config": self.config,
            "profile": self.profile,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "yolo": self.yolo,
            "attachments": list(self.attachments),
            "scenario": self.scenario,
        }

    def run_local(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        prompt: str,
        timeout_seconds: int,
        max_steps: int,
    ) -> AgentOutcome:
        payload = {
            "workspace": str(workspace),
            "artifact_dir": str(artifact_dir),
            "prompt": prompt,
            "max_steps": max_steps,
            **self.worker_payload(),
        }
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "evals.worker"],
                cwd=Path(__file__).resolve().parents[1],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            (artifact_dir / "agent.stderr.log").write_text(str(exc.stderr or ""), encoding="utf-8")
            return AgentOutcome(
                returncode=124,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
                error=f"agent exceeded {timeout_seconds}s wall-time budget",
            )
        (artifact_dir / "agent.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (artifact_dir / "agent.stderr.log").write_text(completed.stderr, encoding="utf-8")
        metrics: dict[str, Any] = {}
        worker_path = artifact_dir / "worker.json"
        if worker_path.is_file():
            try:
                value = json.loads(worker_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    metrics = value
            except (OSError, json.JSONDecodeError):
                pass
        error = None
        if completed.returncode != 0:
            error = str(metrics.get("error") or f"agent exited {completed.returncode}")
        return AgentOutcome(
            returncode=completed.returncode,
            duration_seconds=time.monotonic() - started,
            error=error,
            metrics=metrics,
        )


class CommandAdapter(AgentAdapter):
    """Minimal adapter for comparing another agent via a command template.

    Supported placeholders are ``{workspace}``, ``{prompt_file}``, and
    ``{artifact_dir}``. The command receives no yucode provider credentials.
    """

    name = "command"

    def __init__(self, command: list[str]):
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("agent command must be a non-empty JSON array of strings")
        self.command = tuple(command)

    def public_metadata(self) -> dict[str, Any]:
        return {"kind": self.name, "argv_length": len(self.command)}

    def run_local(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        prompt: str,
        timeout_seconds: int,
        max_steps: int,
    ) -> AgentOutcome:
        del max_steps
        prompt_path = artifact_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        replacements = {
            "workspace": str(workspace),
            "prompt_file": str(prompt_path),
            "artifact_dir": str(artifact_dir),
        }
        try:
            command = [part.format_map(replacements) for part in self.command]
        except KeyError as exc:
            return AgentOutcome(
                returncode=2,
                duration_seconds=0.0,
                error=f"unknown command placeholder: {exc}",
            )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env={**os.environ, "YUCODE_EVAL_PROMPT": str(prompt_path)},
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            (artifact_dir / "agent.stdout.log").write_text(str(exc.stdout or ""), encoding="utf-8")
            (artifact_dir / "agent.stderr.log").write_text(str(exc.stderr or ""), encoding="utf-8")
            return AgentOutcome(
                returncode=124,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
                error=f"agent exceeded {timeout_seconds}s wall-time budget",
            )
        (artifact_dir / "agent.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (artifact_dir / "agent.stderr.log").write_text(completed.stderr, encoding="utf-8")
        return AgentOutcome(
            returncode=completed.returncode,
            duration_seconds=time.monotonic() - started,
            error=(None if completed.returncode == 0 else f"agent command exited {completed.returncode}"),
        )
