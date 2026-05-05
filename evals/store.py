from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import FailureReason, RunRecord


@dataclass(frozen=True)
class AttemptLease:
    id: int
    experiment_id: str
    task_id: str
    repetition: int
    attempt: int
    stage: str
    contract: dict[str, Any]


class RunStore:
    """Small local SQLite queue and result index; artifacts remain ordinary files."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    suite_digest TEXT NOT NULL,
                    output_dir TEXT NOT NULL UNIQUE,
                    metadata_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'running',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_instances (
                    id INTEGER PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    repetition INTEGER NOT NULL,
                    contract_json TEXT NOT NULL,
                    UNIQUE(experiment_id, task_id, repetition)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY,
                    task_instance_id INTEGER NOT NULL REFERENCES task_instances(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    stage TEXT NOT NULL DEFAULT 'queued',
                    worker_id TEXT,
                    lease_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    recovery_of INTEGER REFERENCES attempts(id),
                    UNIQUE(task_instance_id, attempt_no)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY,
                    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    UNIQUE(attempt_id, path)
                );
                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY,
                    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    message TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS comparisons (
                    id INTEGER PRIMARY KEY,
                    baseline_experiment TEXT NOT NULL,
                    candidate_experiment TEXT NOT NULL,
                    certificate_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    def create_experiment(
        self,
        experiment_id: str,
        *,
        suite_digest: str,
        output_dir: Path,
        metadata: dict[str, Any],
        resume: bool = False,
    ) -> None:
        now = time.time()
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            row = connection.execute("SELECT suite_digest, output_dir FROM experiments WHERE id=?", (experiment_id,)).fetchone()
            if row is not None:
                if not resume:
                    raise ValueError(f"experiment already exists: {experiment_id}")
                if row["suite_digest"] != suite_digest or Path(row["output_dir"]).resolve() != output_dir.resolve():
                    raise ValueError("resume experiment does not match suite digest or output directory")
                return
            connection.execute(
                "INSERT INTO experiments(id,suite_digest,output_dir,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (experiment_id, suite_digest, str(output_dir.resolve()), encoded, now, now),
            )

    def enqueue(self, experiment_id: str, rows: Iterable[tuple[str, int, dict[str, Any]]]) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for task_id, repetition, contract in rows:
                    connection.execute(
                        "INSERT OR IGNORE INTO task_instances(experiment_id,task_id,repetition,contract_json) VALUES(?,?,?,?)",
                        (experiment_id, task_id, repetition, json.dumps(contract, ensure_ascii=False, sort_keys=True)),
                    )
                    task_instance = connection.execute(
                        "SELECT id FROM task_instances WHERE experiment_id=? AND task_id=? AND repetition=?",
                        (experiment_id, task_id, repetition),
                    ).fetchone()
                    assert task_instance is not None
                    connection.execute(
                        "INSERT OR IGNORE INTO attempts(task_instance_id,attempt_no,created_at,updated_at) VALUES(?,?,?,?)",
                        (task_instance["id"], 1, now, now),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def claim(self, experiment_id: str, worker_id: str, *, lease_seconds: int = 60) -> AttemptLease | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT a.id, a.attempt_no, a.stage, t.task_id, t.repetition, t.contract_json
                    FROM attempts a JOIN task_instances t ON t.id=a.task_instance_id
                    WHERE t.experiment_id=? AND a.state='pending'
                    ORDER BY t.task_id, t.repetition, a.attempt_no LIMIT 1
                    """,
                    (experiment_id,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                changed = connection.execute(
                    "UPDATE attempts SET state='running',worker_id=?,lease_until=?,updated_at=? WHERE id=? AND state='pending'",
                    (worker_id, now + lease_seconds, now, row["id"]),
                ).rowcount
                connection.commit()
                if changed != 1:
                    return None
                return AttemptLease(
                    id=row["id"],
                    experiment_id=experiment_id,
                    task_id=row["task_id"],
                    repetition=row["repetition"],
                    attempt=row["attempt_no"],
                    stage=row["stage"],
                    contract=json.loads(row["contract_json"]),
                )
            except Exception:
                connection.rollback()
                raise

    def heartbeat(self, attempt_id: int, worker_id: str, *, lease_seconds: int = 60) -> bool:
        now = time.time()
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE attempts SET lease_until=?,updated_at=? WHERE id=? AND state='running' AND worker_id=?",
                (now + lease_seconds, now, attempt_id, worker_id),
            ).rowcount
        return changed == 1

    def stage(self, attempt_id: int, stage: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE attempts SET stage=?,updated_at=? WHERE id=? AND state='running'",
                (stage, time.time(), attempt_id),
            ).rowcount
        if changed != 1:
            raise ValueError(f"attempt is not running: {attempt_id}")

    def complete(
        self,
        attempt_id: int,
        record: RunRecord,
        *,
        artifacts: Iterable[dict[str, Any]] = (),
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    "UPDATE attempts SET state='completed',stage='finalized',lease_until=NULL,result_json=?,error=?,updated_at=? WHERE id=? AND state='running'",
                    (json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True), record.error, now, attempt_id),
                ).rowcount
                if changed != 1:
                    raise ValueError(f"attempt is not running: {attempt_id}")
                for artifact in artifacts:
                    connection.execute(
                        "INSERT OR REPLACE INTO artifacts(attempt_id,kind,path,digest,size) VALUES(?,?,?,?,?)",
                        (attempt_id, artifact.get("kind", "file"), artifact["path"], artifact["digest"], int(artifact["size"])),
                    )
                for failure in record.failures:
                    connection.execute(
                        "INSERT INTO failures(attempt_id,code,stage,subject,message,priority,evidence_json) VALUES(?,?,?,?,?,?,?)",
                        (
                            attempt_id,
                            failure.code,
                            failure.stage,
                            failure.subject,
                            failure.message,
                            failure.priority,
                            json.dumps(list(failure.evidence_refs), ensure_ascii=False),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def recover_expired(self, experiment_id: str, *, force: bool = False) -> int:
        now = time.time()
        recovered = 0
        resumable = {"queued", "preflight", "source_prepared", "patch_captured", "grader_running", "evidence_sealed"}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                query = """
                    SELECT a.*, t.experiment_id, t.task_id, t.repetition, t.contract_json FROM attempts a
                    JOIN task_instances t ON t.id=a.task_instance_id
                    WHERE t.experiment_id=? AND a.state='running'
                """
                parameters: tuple[Any, ...] = (experiment_id,)
                if not force:
                    query += " AND a.lease_until < ?"
                    parameters += (now,)
                rows = connection.execute(query, parameters).fetchall()
                for row in rows:
                    if row["stage"] in resumable:
                        connection.execute(
                            "UPDATE attempts SET state='pending',worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                            (now, row["id"]),
                        )
                    else:
                        contract = json.loads(row["contract_json"])
                        failure = FailureReason.create(
                            "infra.runner_lost",
                            "runner",
                            "eval_harness",
                            "runner lost while agent was in flight",
                        )
                        now_text = datetime.now(UTC).isoformat(timespec="seconds")
                        record = RunRecord(
                            schema_version=int(contract.get("schema_version", 1)),
                            experiment_id=experiment_id,
                            task_id=row["task_id"],
                            repetition=int(row["repetition"]),
                            agent="unknown",
                            status="infra_error",
                            passed=False,
                            comparable=False,
                            started_at=now_text,
                            finished_at=now_text,
                            duration_seconds=0.0,
                            error=failure.message,
                            metadata={
                                "category": contract.get("category"),
                                "release_eligible": bool(contract.get("release_eligible", False)),
                            },
                            attempt=int(row["attempt_no"]),
                            execution_status="infra_error",
                            subject=str(contract.get("subject", "eval_harness")),
                            targets=tuple(contract.get("targets", ())),
                            profile=str(contract.get("profile", "coding_default")),
                            failures=(failure,),
                            primary_failure=failure,
                        )
                        connection.execute(
                            "UPDATE attempts SET state='completed',stage='finalized',lease_until=NULL,error=?,result_json=?,updated_at=? WHERE id=?",
                            (failure.message, json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True), now, row["id"]),
                        )
                        next_attempt = connection.execute(
                            "SELECT COALESCE(MAX(attempt_no),0)+1 AS value FROM attempts WHERE task_instance_id=?",
                            (row["task_instance_id"],),
                        ).fetchone()["value"]
                        connection.execute(
                            "INSERT INTO attempts(task_instance_id,attempt_no,created_at,updated_at,recovery_of) VALUES(?,?,?,?,?)",
                            (row["task_instance_id"], next_attempt, now, now, row["id"]),
                        )
                    recovered += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return recovered

    def retry(self, experiment_id: str, task_id: str, repetition: int) -> int:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    "SELECT id FROM task_instances WHERE experiment_id=? AND task_id=? AND repetition=?",
                    (experiment_id, task_id, repetition),
                ).fetchone()
                if task is None:
                    raise ValueError(f"unknown task repetition: {task_id}/{repetition}")
                attempt = connection.execute(
                    "SELECT COALESCE(MAX(attempt_no),0)+1 AS value FROM attempts WHERE task_instance_id=?",
                    (task["id"],),
                ).fetchone()["value"]
                connection.execute(
                    "INSERT INTO attempts(task_instance_id,attempt_no,created_at,updated_at) VALUES(?,?,?,?)",
                    (task["id"], attempt, now, now),
                )
                connection.commit()
                return int(attempt)
            except Exception:
                connection.rollback()
                raise

    def records(self, experiment_id: str) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.result_json FROM attempts a JOIN task_instances t ON t.id=a.task_instance_id
                WHERE t.experiment_id=? AND a.result_json IS NOT NULL ORDER BY t.task_id,t.repetition,a.attempt_no
                """,
                (experiment_id,),
            ).fetchall()
        return [RunRecord.from_dict(json.loads(row["result_json"])) for row in rows]

    def counts(self, experiment_id: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.state,COUNT(*) AS count FROM attempts a JOIN task_instances t ON t.id=a.task_instance_id
                WHERE t.experiment_id=? GROUP BY a.state
                """,
                (experiment_id,),
            ).fetchall()
        return {row["state"]: row["count"] for row in rows}

    def save_comparison(self, baseline: str, candidate: str, certificate: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO comparisons(baseline_experiment,candidate_experiment,certificate_json,created_at) VALUES(?,?,?,?)",
                (baseline, candidate, json.dumps(certificate, ensure_ascii=False, sort_keys=True), time.time()),
            )

    def update_experiment_metadata(self, experiment_id: str, metadata: dict[str, Any]) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE experiments SET metadata_json=?,updated_at=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), time.time(), experiment_id),
            ).rowcount
        if changed != 1:
            raise ValueError(f"unknown experiment: {experiment_id}")
