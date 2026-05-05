from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evals.adapters import AgentAdapter, AgentOutcome, CommandAdapter, YucodeAdapter
from evals.executors import LocalExecutor
from evals.experiment import comparability_certificate, evaluate_release_gate, load_matrix, resolve_variant_config
from evals.models import FailureReason, RunRecord, ScoreCard, primary_failure, summarize
from evals.runner import EvaluationRunner
from evals.schema import EvalConfigError, load_catalog, load_suite
from evals.store import RunStore
from evals.swebench import _image_for
from evals.trace import sha256_file


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class StubYucodeAdapter(AgentAdapter):
    name = "yucode"

    def public_metadata(self) -> dict[str, object]:
        return {"kind": self.name, "url": "https://example.invalid/v1", "model": "stub"}

    def run_local(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        prompt: str,
        timeout_seconds: int,
        max_steps: int,
    ) -> AgentOutcome:
        del artifact_dir, prompt, timeout_seconds, max_steps
        (workspace / "answer.txt").write_text("fixed\n", encoding="utf-8")
        return AgentOutcome(returncode=0, duration_seconds=0.0, metrics={"tool_calls": 1, "usage": {}})


def make_v2_suite(tmp_path: Path, *, profile: str = "coding_default", target: str = "agent.coding.patch") -> Path:
    root = tmp_path / "suite"
    write(
        root / "suite.toml",
        """
schema_version = 2
name = "v2-smoke"
tasks = ["task/task.toml"]

[defaults]
repetitions = 2
jobs = 2
agent_timeout_seconds = 30
grader_timeout_seconds = 30
max_steps = 5
network = "offline"
""".strip()
        + "\n",
    )
    write(root / "task" / "prompt.md", "Create answer.txt.\n")
    write(root / "task" / "source" / "README.md", "fixture\n")
    write(
        root / "task" / "grader" / "grade.py",
        "from pathlib import Path\nraise SystemExit(0 if Path('answer.txt').read_text() == 'fixed\\n' else 1)\n",
    )
    allowed = '["Edit"]' if profile == "coding_default" else "[]"
    write(
        root / "task" / "task.toml",
        f"""
schema_version = 2
id = "create-answer"
prompt = "prompt.md"
targets = ["{target}"]
profile = "{profile}"
category = "coding"
allowed_tools = {allowed}
step_budget = 1
expected_artifact = "answer.txt"
[source]
type = "local"
path = "source"

[environment]
image = "python:3.12"
network = "offline"

[grader]
path = "grader"
command = ["{sys.executable}", "{{grader}}/grade.py"]

[success]
require = ["within_budget", "verifier_passed", "expected_artifact_exists", "normal_success_stop"]
""".strip()
        + "\n",
    )
    return root / "suite.toml"


def test_catalog_and_unknown_capability_validation(tmp_path: Path) -> None:
    catalog = load_catalog()
    assert catalog.profiles["coding_default"].driver == "yucode"
    suite_path = make_v2_suite(tmp_path)
    task_path = suite_path.parent / "task" / "task.toml"
    task_path.write_text(task_path.read_text().replace("agent.coding.patch", "agent.unknown"), encoding="utf-8")
    with pytest.raises(EvalConfigError, match="unknown capabilities"):
        load_suite(suite_path)


def test_manifest_rejects_hidden_grader_in_docker_context(tmp_path: Path) -> None:
    suite_path = make_v2_suite(tmp_path)
    task_path = suite_path.parent / "task" / "task.toml"
    task_path.write_text(
        task_path.read_text().replace(
            'image = "python:3.12"',
            'dockerfile = "grader/Dockerfile"\ncontext = "grader"',
        ),
        encoding="utf-8",
    )
    write(suite_path.parent / "task" / "grader" / "Dockerfile", "FROM python:3.12\n")

    with pytest.raises(EvalConfigError, match="hidden grading material"):
        load_suite(suite_path)


def test_v2_full_pipeline_and_stable_trace(tmp_path: Path) -> None:
    suite = load_suite(make_v2_suite(tmp_path))
    output = tmp_path / "result"
    records = EvaluationRunner(
        suite,
        StubYucodeAdapter(),
        LocalExecutor(),
        output_dir=output,
    ).run()
    assert len(records) == 2
    assert all(record.passed for record in records)
    assert all(record.tool_calls == 1 for record in records)
    assert all(record.scorecard and record.scorecard.reproducibility == "passed" for record in records)
    assert len({record.semantic_trace_hash for record in records}) == 1
    assert (output / "runs.sqlite3").is_file()
    assert (output / "summary.json").is_file()


def test_fixed_baseline_survives_agent_amending_git_history(tmp_path: Path) -> None:
    class AmendingAdapter(StubYucodeAdapter):
        def run_local(
            self,
            *,
            workspace: Path,
            artifact_dir: Path,
            prompt: str,
            timeout_seconds: int,
            max_steps: int,
        ) -> AgentOutcome:
            outcome = super().run_local(
                workspace=workspace,
                artifact_dir=artifact_dir,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                max_steps=max_steps,
            )
            subprocess.run(["git", "add", "answer.txt"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "--amend", "--no-edit", "--quiet"], cwd=workspace, check=True)
            return outcome

    records = EvaluationRunner(
        load_suite(make_v2_suite(tmp_path)),
        AmendingAdapter(),
        LocalExecutor(),
        output_dir=tmp_path / "amended-result",
        repetitions=1,
    ).run()

    assert records[0].passed
    assert records[0].patch_bytes > 0


def test_invalid_gold_patch_is_rejected_before_agent_runs(tmp_path: Path) -> None:
    suite_path = make_v2_suite(tmp_path)
    task_dir = suite_path.parent / "task"
    manifest = task_dir / "task.toml"
    manifest.write_text(
        manifest.read_text().replace(
            '[grader]\npath = "grader"',
            '[grader]\npath = "grader"\ngold_patch = "gold.patch"',
        ),
        encoding="utf-8",
    )
    write(task_dir / "gold.patch", "not a patch\n")
    records = EvaluationRunner(
        load_suite(suite_path),
        StubYucodeAdapter(),
        LocalExecutor(),
        output_dir=tmp_path / "gold-result",
        repetitions=1,
    ).run()

    assert records[0].execution_status == "infra_error"
    assert "patch" in (records[0].error or "").lower()


def test_driver_mismatch_is_na_without_model_call(tmp_path: Path) -> None:
    suite = load_suite(make_v2_suite(tmp_path, profile="coding_default"))
    records = EvaluationRunner(
        suite,
        CommandAdapter([sys.executable, "-c", "raise SystemExit('must not run')"]),
        LocalExecutor(),
        output_dir=tmp_path / "na",
        repetitions=1,
    ).run()
    assert records[0].applicability == "not_applicable"
    assert records[0].execution_status == "not_applicable"
    assert records[0].usage.model_calls == 0
    assert not (tmp_path / "na" / "runs" / "create-answer" / "1" / "attempt-1" / "worker.json").exists()


def test_command_profile_runs_external_agent(tmp_path: Path) -> None:
    suite = load_suite(make_v2_suite(tmp_path, profile="command_coding"))
    adapter = CommandAdapter(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('answer.txt').write_text('fixed\\n', encoding='utf-8')",
        ]
    )
    records = EvaluationRunner(
        suite,
        adapter,
        LocalExecutor(),
        output_dir=tmp_path / "command-result",
        repetitions=1,
    ).run()

    assert records[0].passed
    assert records[0].comparable is False


def test_sqlite_claim_is_atomic_and_retry_is_immutable(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite3")
    store.create_experiment("e", suite_digest="sha256:x", output_dir=tmp_path, metadata={})
    store.enqueue("e", [("task", 1, {})])
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda index: store.claim("e", f"w{index}"), range(8)))
    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    attempt = store.retry("e", "task", 1)
    assert attempt == 2


def test_force_resume_marks_inflight_agent_infra_and_creates_recovery(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite3")
    store.create_experiment("e", suite_digest="sha256:x", output_dir=tmp_path, metadata={})
    store.enqueue(
        "e",
        [("task", 1, {"schema_version": 2, "profile": "coding_default", "targets": ["agent.coding.patch"]})],
    )
    lease = store.claim("e", "worker")
    assert lease is not None
    store.stage(lease.id, "agent_running")
    assert store.recover_expired("e", force=True) == 1
    records = store.records("e")
    assert records[0].execution_status == "infra_error"
    recovery = store.claim("e", "recovery")
    assert recovery is not None
    assert recovery.attempt == 2


def test_failure_priority_is_orthogonal() -> None:
    failures = [
        FailureReason.create("verifier.failed", "grader", "agent_capability", "failed"),
        FailureReason.create("artifact.missing", "artifact", "agent_capability", "missing"),
        FailureReason.create("infra.docker", "runner", "eval_harness", "docker"),
    ]
    assert primary_failure(failures).code == "infra.docker"  # type: ignore[union-attr]


def test_yucode_metadata_redacts_credentials_and_lists_provider_hosts() -> None:
    adapter = YucodeAdapter(
        {
            "provider": {
                "active": "private",
                "private": {
                    "url": "https://user:password@example.com:8443/v1?key=secret",
                    "key": "sk-secret",
                    "model": "test-model",
                },
                "alternate": {
                    "url": "https://alternate.example/v1",
                    "key": "sk-other",
                    "model": "other-model",
                },
            },
            "eval": {
                "pricing": {
                    "prompt_per_million": 1,
                    "completion_per_million": 4,
                    "cached_read_per_million": 0.1,
                    "cached_write_per_million": 1.25,
                }
            },
        },
        scenario={"rounds": [{"provider": "@active"}, {"provider": "@alternate"}]},
    )

    metadata = adapter.public_metadata()
    assert metadata["url"] == "https://example.com:8443/v1"
    assert adapter.provider_hosts() == ("alternate.example", "example.com")
    assert "secret" not in json.dumps(metadata)
    assert adapter.estimate_cost(
        {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 100_000,
            "cached_read_tokens": 500_000,
            "cached_write_tokens": 100_000,
        }
    ) == pytest.approx(0.975)


def test_swebench_image_uses_official_remote_namespace() -> None:
    class TestSpec:
        instance_image_key = "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"

    def make_test_spec(instance: dict[str, object], *, namespace: str) -> TestSpec:
        assert instance["instance_id"] == "astropy__astropy-12907"
        assert namespace == "swebench"
        return TestSpec()

    assert _image_for({"instance_id": "astropy__astropy-12907"}, make_test_spec) == TestSpec.instance_image_key


def test_v2_summary_excludes_na_and_infra_from_capability_denominator() -> None:
    def result(task_id: str, *, passed: bool, execution: str, applicability: str = "applicable") -> RunRecord:
        return RunRecord(
            schema_version=2,
            experiment_id="e",
            task_id=task_id,
            repetition=1,
            agent="yucode",
            status="passed" if passed else "infra_error" if execution == "infra_error" else "failed",
            passed=passed,
            comparable=True,
            started_at="now",
            finished_at="now",
            duration_seconds=0.1,
            execution_status=execution,  # type: ignore[arg-type]
            applicability=applicability,  # type: ignore[arg-type]
            subject="agent_capability",
            targets=("agent.coding.patch",),
            profile="coding_default",
            scorecard=ScoreCard(
                functional="passed" if passed else "unknown",
                applicability=applicability,  # type: ignore[arg-type]
                reproducibility="passed",
            ),
            metadata={"release_eligible": True},
        )

    summary = summarize(
        [
            result("passed", passed=True, execution="completed"),
            result("na", passed=False, execution="not_applicable", applicability="not_applicable"),
            result("infra", passed=False, execution="infra_error"),
        ]
    )
    assert summary["scored_runs"] == 1
    assert summary["run_success_rate"] == 1.0
    assert summary["not_applicable_runs"] == 1
    assert summary["infra_error_runs"] == 1


def test_comparability_and_release_gate_require_declared_differences() -> None:
    baseline = {"comparable": True, "tasks": ["a"], "agent_config": {"model": "old"}, "network": "offline"}
    candidate = {"comparable": True, "tasks": ["a"], "agent_config": {"model": "new"}, "network": "offline"}
    rejected = comparability_certificate(baseline, candidate)
    assert rejected.comparable is False
    accepted = comparability_certificate(baseline, candidate, allowed_differences=("agent_config.model",))
    assert accepted.comparable is True
    gate = evaluate_release_gate(
        {
            "comparable": True,
            "infra_error_runs": 0,
            "incomplete_evidence_runs": 0,
            "pass_at_1": 0.9,
            "all_at_k": 0.8,
        },
        policy={"pass_at_1": 0.8, "all_at_k": 0.75},
    )
    assert gate.passed is True


def test_source_controlled_profile_suites_validate() -> None:
    root = Path(__file__).resolve().parents[1]
    provider = load_suite(root / "evals/examples/profiles/provider-suite.toml")
    assert len(provider.tasks) == 8
    assert {task.profile for task in provider.tasks} == {
        "cache_live",
        "coding_default",
        "coding_indexed",
        "provider_switch",
        "provider_tools",
        "strict_tools",
        "vision_attachment",
        "vision_tool",
    }


def test_conditional_live_profiles_are_na_before_model_call(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    suite = load_suite(root / "evals/examples/profiles/provider-suite.toml")
    adapter = YucodeAdapter(
        {
            "provider": {
                "active": "only",
                "only": {
                    "url": "https://example.invalid/v1",
                    "key": "not-used",
                    "model": "text-only",
                    "image_input": "off",
                    "strict_tools": False,
                },
            }
        }
    )
    selected = {
        "provider-vision-tool",
        "provider-vision-attachment",
        "provider-builtin-tools",
        "provider-same-session-switch",
        "provider-strict-tools",
    }
    records = EvaluationRunner(
        suite,
        adapter,
        LocalExecutor(),
        output_dir=tmp_path / "conditional-na",
        selected_tasks=selected,
    ).run()
    assert len(records) == len(selected)
    assert all(record.applicability == "not_applicable" for record in records)
    assert all(record.usage.model_calls == 0 for record in records)
    assert not list((tmp_path / "conditional-na" / "runs").glob("**/worker.json"))


def test_all_live_profiles_are_na_when_provider_is_unconfigured(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    suite = load_suite(root / "evals/examples/profiles/provider-suite.toml")
    records = EvaluationRunner(
        suite,
        YucodeAdapter({}),
        LocalExecutor(),
        output_dir=tmp_path / "provider-unconfigured",
    ).run()
    assert len(records) == 8
    assert all(record.applicability == "not_applicable" for record in records)
    assert all(record.usage.model_calls == 0 for record in records)


def test_matrix_resolution_and_example_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    matrix = load_matrix(root / "evals/examples/profiles/temperature-matrix.toml")
    assert matrix.base_profile == "coding_default"
    assert tuple(matrix.variants) == ("deterministic", "exploratory")
    config = {
        "provider": {
            "active": "main",
            "main": {"url": "https://example.test/v1", "model": "m", "temperature": 1.0},
        }
    }
    resolved = resolve_variant_config(config, matrix.variants["deterministic"])
    assert resolved["provider"]["main"]["temperature"] == 0.0
    assert config["provider"]["main"]["temperature"] == 1.0


@pytest.mark.parametrize(
    ("stage", "same_attempt"),
    [
        ("queued", True),
        ("preflight", True),
        ("source_prepared", True),
        ("agent_running", False),
        ("patch_captured", True),
        ("grader_running", True),
        ("evidence_sealed", True),
    ],
)
def test_recovery_policy_for_every_checkpoint(tmp_path: Path, stage: str, same_attempt: bool) -> None:
    store = RunStore(tmp_path / f"{stage}.sqlite3")
    store.create_experiment("e", suite_digest="sha256:x", output_dir=tmp_path / stage, metadata={})
    store.enqueue("e", [("task", 1, {"schema_version": 2, "profile": "coding_default", "targets": ["agent.coding.patch"]})])
    lease = store.claim("e", "worker", lease_seconds=0)
    assert lease is not None
    store.stage(lease.id, stage)
    assert store.recover_expired("e") == 1
    recovered = store.claim("e", "recovery")
    assert recovered is not None
    assert (recovered.attempt == lease.attempt) is same_attempt
    if not same_attempt:
        records = store.records("e")
        assert records[0].execution_status == "infra_error"
        assert records[0].primary_failure and records[0].primary_failure.code == "infra.runner_lost"


def test_missing_checkpoint_patch_is_infra_without_model_replay(tmp_path: Path) -> None:
    suite = load_suite(make_v2_suite(tmp_path))
    output = tmp_path / "checkpoint-result"
    store = RunStore(output / "runs.sqlite3")
    store.create_experiment(
        output.name,
        suite_digest=sha256_file(suite.manifest_path),
        output_dir=output,
        metadata={},
    )
    task = suite.tasks[0]
    store.enqueue(
        output.name,
        [
            (
                task.id,
                1,
                {
                    "schema_version": 2,
                    "profile": task.profile,
                    "targets": list(task.targets),
                    "subject": "agent_capability",
                    "category": task.category,
                    "release_eligible": True,
                },
            )
        ],
    )
    lease = store.claim(output.name, "crashed")
    assert lease is not None
    store.stage(lease.id, "patch_captured")
    records = EvaluationRunner(
        suite,
        StubYucodeAdapter(),
        LocalExecutor(),
        output_dir=output,
        repetitions=1,
        resume=True,
    ).run()
    assert records[-1].execution_status == "infra_error"
    assert "missing its immutable patch artifact" in (records[-1].error or "")
    assert not list(output.glob("**/worker.json"))
