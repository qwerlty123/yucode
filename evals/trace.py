from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import EvidenceManifest, write_json

_SECRET_KEY = re.compile(r"(?:key|token|secret|password|authorization|cookie)", re.IGNORECASE)
_VOLATILE_KEYS = {
    "observed_at",
    "monotonic_seconds",
    "duration_seconds",
    "session_uid",
    "provider_request_id",
    "experiment_id",
    "repetition",
    "attempt",
    "semantic_digest",
}


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return "sha256:" + digest.hexdigest()
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.name.endswith(".pyc") or not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _normalize_text(value: str, roots: dict[str, Path]) -> str:
    text = value.replace("\r\n", "\n")
    for label, root in sorted(roots.items(), key=lambda item: len(str(item[1])), reverse=True):
        text = text.replace(str(root), f"<{label}>")
    return text


def redact(value: Any, *, secrets: Iterable[str] = (), roots: dict[str, Path] | None = None) -> Any:
    roots = roots or {}
    secret_values = tuple(item for item in secrets if item)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[str(key)] = "<redacted>" if _SECRET_KEY.search(str(key)) else redact(item, secrets=secret_values, roots=roots)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets=secret_values, roots=roots) for item in value]
    if isinstance(value, str):
        text = _normalize_text(value, roots)
        for secret in secret_values:
            text = text.replace(secret, "<redacted>")
        return text
    return value


def canonical_event(event: dict[str, Any]) -> dict[str, Any]:
    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in sorted(value.items()) if key not in _VOLATILE_KEYS}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return normalize(event)


class TraceRecorder:
    def __init__(
        self,
        path: Path,
        *,
        experiment_id: str,
        task_id: str,
        repetition: int,
        attempt: int,
        secrets: Iterable[str] = (),
        roots: dict[str, Path] | None = None,
    ):
        self.path = path
        self.experiment_id = experiment_id
        self.task_id = task_id
        self.repetition = repetition
        self.attempt = attempt
        self.secrets = tuple(secrets)
        self.roots = roots or {}
        self._events: list[dict[str, Any]] = []
        self._started = time.monotonic()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._events.append(event)

    def emit(self, event_type: str, *, subject: str, stage: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "schema_version": 1,
            "event_index": len(self._events) + 1,
            "event_type": event_type,
            "subject": subject,
            "stage": stage,
            "experiment_id": self.experiment_id,
            "task_id": self.task_id,
            "repetition": self.repetition,
            "attempt": self.attempt,
            "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "monotonic_seconds": round(time.monotonic() - self._started, 6),
            "payload": redact(payload or {}, secrets=self.secrets, roots=self.roots),
        }
        canonical = canonical_event(event)
        event["semantic_digest"] = sha256_bytes(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        self._events.append(event)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return event

    def seal(self) -> tuple[str, str]:
        byte_digest = sha256_file(self.path)
        canonical = [canonical_event(event) for event in self._events]
        semantic = sha256_bytes(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        return byte_digest, semantic


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo_root, text=True, capture_output=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def package_versions() -> dict[str, str]:
    return {distribution.metadata["Name"] or distribution.name: distribution.version for distribution in importlib.metadata.distributions()}


def dirty_digest(repo_root: Path, status: str) -> str:
    payload = hashlib.sha256()
    payload.update(status.encode())
    payload.update(b"\0")
    payload.update(_git(repo_root, "diff", "--binary", "HEAD").encode())
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        path = (repo_root / line[3:]).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError:
            continue
        if path.is_file():
            payload.update(line[3:].encode())
            payload.update(b"\0")
            payload.update(path.read_bytes())
            payload.update(b"\0")
        elif path.is_dir():
            payload.update(sha256_tree(path).encode())
    return "sha256:" + payload.hexdigest()


def artifact_inventory(root: Path, *, exclude: set[Path] | None = None) -> tuple[dict[str, Any], ...]:
    excluded = {path.resolve() for path in (exclude or set())}
    items: list[dict[str, Any]] = []
    if not root.exists():
        return ()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.resolve() in excluded or path.name.endswith(".tmp"):
            continue
        items.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "digest": sha256_file(path),
            }
        )
    return tuple(items)


def secret_scan(root: Path, secrets: Iterable[str]) -> dict[str, Any]:
    values = tuple(value for value in secrets if value)
    checked = 0
    matches = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        checked += 1
        try:
            data = path.read_bytes()
        except OSError:
            continue
        matches += sum(data.count(value.encode()) for value in values)
    return {
        "policy_version": 1,
        "checked_artifacts": checked,
        "configured_secret_matches": matches,
        "configured_secret_exact_value_absent": matches == 0,
    }


def build_evidence_manifest(
    *,
    repo_root: Path,
    run_dir: Path,
    experiment_id: str,
    task_id: str,
    repetition: int,
    attempt: int,
    digests: dict[str, str | None],
    agent: dict[str, Any],
    environment: dict[str, Any],
    secrets: Iterable[str] = (),
) -> EvidenceManifest:
    status = _git(repo_root, "status", "--porcelain=v1")
    scan = secret_scan(run_dir, secrets)
    manifest_path = run_dir / "evidence.json"
    artifacts = artifact_inventory(run_dir, exclude={manifest_path})
    required = ("suite", "task", "prompt", "source", "grader", "agent_code", "profile", "tool_schema", "trace")
    complete = all(digests.get(key) for key in required) and scan["configured_secret_exact_value_absent"]
    return EvidenceManifest(
        schema_version=1,
        experiment_id=experiment_id,
        task_id=task_id,
        repetition=repetition,
        attempt=attempt,
        git={
            "commit": _git(repo_root, "rev-parse", "HEAD"),
            "branch": _git(repo_root, "branch", "--show-current"),
            "dirty": bool(status),
            "dirty_digest": dirty_digest(repo_root, status),
        },
        digests=dict(digests),
        agent=dict(agent),
        environment=dict(environment),
        platform={
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "timezone": time.tzname[0],
            "locale": os.environ.get("LC_ALL") or os.environ.get("LANG") or "",
            "packages": package_versions(),
        },
        artifacts=artifacts,
        redaction=scan,
        complete=complete,
    )


def write_evidence(path: Path, manifest: EvidenceManifest) -> None:
    write_json(path, asdict(manifest))
