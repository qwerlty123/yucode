from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from yucode.base import Config, ConfigFile

from .adapters import AgentAdapter, CommandAdapter, YucodeAdapter
from .docker import DockerError, DockerExecutor
from .executors import LocalExecutor
from .experiment import evaluate_release_gate, load_matrix, resolve_variant_config
from .models import write_json
from .report import compare_results, write_report
from .runner import EvaluationRunner, experiment_id
from .schema import EvalConfigError, load_suite
from .store import RunStore
from .swebench import SwebenchUnavailable, run_swebench


def _config(path: str | None) -> tuple[dict[str, Any], str]:
    resolved = ConfigFile.resolve_path(path)
    data = ConfigFile.load(resolved)
    config = Config.from_dict(data)
    provider = config.provider
    missing = [
        key
        for key, value in (
            ("provider.url", provider.url),
            ("provider.key", provider.key),
            ("provider.model", provider.model),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"incomplete yucode config: {', '.join(missing)}")
    return data, resolved


def _adapter(args: argparse.Namespace) -> tuple[AgentAdapter, str | None]:
    if args.agent == "command":
        if not args.agent_command:
            raise ValueError("--agent command requires --agent-command as a JSON array")
        try:
            command = json.loads(args.agent_command)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --agent-command JSON: {exc}") from exc
        if not isinstance(command, list):
            raise ValueError("--agent-command must be a JSON array")
        return CommandAdapter(command), None
    data, resolved = _config(args.config)
    return YucodeAdapter(data), resolved


def _output(value: str | None, *, suite: str, agent: str) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    root = Path.cwd() / ".yucode" / "evals"
    return root / experiment_id(suite, agent)


def _print_summary(directory: Path) -> None:
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    mode = "comparable" if summary["comparable"] else "debug-only"
    print(f"results: {directory}")
    print(f"mode={mode} pass@1={summary['pass_at_1']:.1%} pass@k={summary['pass_at_k']:.1%} all@k={summary['all_at_k']:.1%}")
    print(f"report: {directory / 'report.md'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Developer-only agent evaluation harness for yucode.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate suite and task manifests")
    validate.add_argument("suite")

    def add_run_arguments(command: argparse.ArgumentParser, *, output_required: bool = False) -> None:
        command.add_argument("suite")
        command.add_argument("--agent", choices=("yucode", "command"), default="yucode")
        command.add_argument("--agent-command", help="JSON argv template for the command adapter")
        command.add_argument("--config", help="yucode config TOML (defaults to ~/.yucode/config.toml)")
        command.add_argument("--local", action="store_true", help="unsafe, non-comparable local debug mode")
        command.add_argument("--task", dest="tasks", action="append", help="task id; repeat to select several")
        command.add_argument("--repetitions", type=int)
        command.add_argument("--jobs", type=int)
        command.add_argument("--output", required=output_required)
        command.add_argument("--exit-zero-on-capability-failure", action="store_true")

    run = subparsers.add_parser("run", help="run a private task suite")
    add_run_arguments(run)
    resume = subparsers.add_parser("resume", help="resume an interrupted SQLite-backed run")
    add_run_arguments(resume, output_required=True)

    retry = subparsers.add_parser("retry", help="create an immutable retry attempt, then resume")
    add_run_arguments(retry, output_required=True)
    retry.add_argument("--repetition", type=int, required=True)

    matrix = subparsers.add_parser("matrix", help="run a controlled experiment matrix")
    add_run_arguments(matrix)
    matrix.add_argument("--matrix", dest="matrix_manifest", required=True, help="experiment matrix TOML")

    report = subparsers.add_parser("report", help="rebuild summary.json and report.md")
    report.add_argument("run_dir")

    compare = subparsers.add_parser("compare", help="compare paired baseline/candidate runs")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--output")
    compare.add_argument("--allow-difference", action="append", default=[])

    gate = subparsers.add_parser("gate", help="evaluate structural and configured release gates")
    gate.add_argument("run_dir")
    gate.add_argument("--comparison")
    gate.add_argument("--policy", help="TOML or JSON table containing explicit numeric thresholds")
    gate.add_argument("--output")

    swebench = subparsers.add_parser("swebench", help="run inference and the official SWE-bench harness")
    swebench.add_argument("--config", help="yucode config TOML")
    swebench.add_argument(
        "--dataset",
        default="princeton-nlp/SWE-bench_Verified",
        help="Hugging Face dataset or local dataset path",
    )
    swebench.add_argument("--split", default="test")
    selection = swebench.add_mutually_exclusive_group(required=True)
    selection.add_argument("--instance-ids", nargs="+")
    selection.add_argument("--limit", type=int)
    swebench.add_argument("--repetitions", type=int, default=3)
    swebench.add_argument("--agent-timeout", type=int, default=1800)
    swebench.add_argument("--grader-timeout", type=int, default=1800)
    swebench.add_argument("--max-steps", type=int, default=200)
    swebench.add_argument("--max-workers", type=int, default=1)
    swebench.add_argument("--jobs", type=int, default=1, help="concurrent inference workers")
    swebench.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            suite = load_suite(args.suite)
            print(f"valid: {suite.name} ({len(suite.tasks)} tasks)")
            return 0
        if args.command == "report":
            summary_path, report_path = write_report(args.run_dir)
            print(f"summary: {summary_path}")
            print(f"report: {report_path}")
            return 0
        if args.command == "compare":
            result = compare_results(args.baseline, args.candidate, allowed_differences=tuple(args.allow_difference))
            candidate_store = Path(args.candidate).expanduser().resolve() / "runs.sqlite3"
            if candidate_store.is_file():
                RunStore(candidate_store).save_comparison(
                    str(result["baseline_experiment"]),
                    str(result["candidate_experiment"]),
                    result["comparability_certificate"],
                )
            if args.output:
                output = Path(args.output).expanduser().resolve()
                write_json(output, result)
                print(f"comparison: {output}")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["comparability_certificate"]["comparable"] else 5
        if args.command == "gate":
            summary_path = Path(args.run_dir).resolve() / "summary.json"
            if not summary_path.is_file():
                write_report(args.run_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            comparison = json.loads(Path(args.comparison).read_text(encoding="utf-8")) if args.comparison else None
            policy: dict[str, Any] = {}
            if args.policy:
                policy_path = Path(args.policy).expanduser().resolve()
                if policy_path.suffix.lower() == ".json":
                    value = json.loads(policy_path.read_text(encoding="utf-8"))
                else:
                    with policy_path.open("rb") as file:
                        value = tomllib.load(file)
                if not isinstance(value, dict):
                    raise ValueError("gate policy must be an object/table")
                policy = value.get("gate", value)
                if not isinstance(policy, dict):
                    raise ValueError("gate policy [gate] must be a table")
            result = evaluate_release_gate(summary, comparison=comparison, policy=policy)
            payload = result.to_dict()
            if args.output:
                write_json(Path(args.output).expanduser().resolve(), payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if result.passed else 4
        if args.command == "matrix":
            suite = load_suite(args.suite)
            matrix_spec = load_matrix(args.matrix_manifest)
            adapter, _config_path = _adapter(args)
            if not isinstance(adapter, YucodeAdapter):
                raise ValueError("matrix execution currently requires --agent yucode")
            selected = {task.id for task in suite.tasks if task.profile == matrix_spec.base_profile}
            if args.tasks:
                requested = set(args.tasks)
                unknown = requested - selected
                if unknown:
                    raise ValueError(f"matrix tasks must use base_profile {matrix_spec.base_profile}: {', '.join(sorted(unknown))}")
                selected = requested
            if not selected:
                raise ValueError(f"suite contains no tasks for matrix base_profile {matrix_spec.base_profile}")
            repetitions = args.repetitions if args.repetitions is not None else matrix_spec.repetitions
            jobs = args.jobs if args.jobs is not None else suite.defaults.jobs
            if repetitions <= 0 or jobs <= 0:
                raise ValueError("matrix repetitions and jobs must be positive")
            output = _output(args.output, suite=suite.name + "-matrix", agent=adapter.name)
            output.mkdir(parents=True, exist_ok=False)
            matrix_metadata = {
                "base_profile": matrix_spec.base_profile,
                "controlled_fields": list(matrix_spec.controlled_fields),
                "expected_differences": list(matrix_spec.expected_differences),
                "subject": matrix_spec.subject,
                "single_factor": matrix_spec.single_factor,
            }
            variant_dirs: dict[str, Path] = {}
            summaries: dict[str, Any] = {}
            for name, changes in matrix_spec.variants.items():
                if not name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in name):
                    raise ValueError(f"matrix variant name is not path-safe: {name}")
                variant_adapter = YucodeAdapter(resolve_variant_config(adapter.config, changes))
                executor = LocalExecutor() if args.local else DockerExecutor(variant_adapter)
                directory = output / name
                EvaluationRunner(
                    suite,
                    variant_adapter,
                    executor,
                    output_dir=directory,
                    repetitions=repetitions,
                    selected_tasks=selected,
                    jobs=jobs,
                    experiment_metadata=matrix_metadata,
                ).run()
                variant_dirs[name] = directory
                summaries[name] = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
            baseline_name = next(iter(variant_dirs))
            comparisons: dict[str, Any] = {}
            for name, directory in list(variant_dirs.items())[1:]:
                comparisons[name] = compare_results(
                    variant_dirs[baseline_name],
                    directory,
                    allowed_differences=matrix_spec.expected_differences,
                )
            payload = {
                "schema_version": 1,
                "matrix": asdict(matrix_spec),
                "baseline_variant": baseline_name,
                "variants": {name: {"run_dir": str(variant_dirs[name]), "summary": summaries[name]} for name in variant_dirs},
                "comparisons": comparisons,
            }
            write_json(output / "matrix.json", payload)
            print(f"matrix results: {output}")
            if any(summary.get("infra_error_runs", 0) for summary in summaries.values()):
                return 3
            if any(not item["comparability_certificate"]["comparable"] for item in comparisons.values()):
                return 5
            capability_failed = any(summary.get("passed_runs", 0) < summary.get("scored_runs", 0) for summary in summaries.values())
            return 0 if not capability_failed or args.exit_zero_on_capability_failure else 1
        if args.command in {"run", "resume", "retry"}:
            suite = load_suite(args.suite)
            adapter, _config_path = _adapter(args)
            if args.repetitions is not None and args.repetitions <= 0:
                raise ValueError("--repetitions must be positive")
            repetitions = args.repetitions if args.repetitions is not None else suite.defaults.repetitions
            output = _output(args.output, suite=suite.name, agent=adapter.name)
            if args.jobs is not None and args.jobs <= 0:
                raise ValueError("--jobs must be positive")
            if args.command == "retry":
                if args.repetition <= 0:
                    raise ValueError("--repetition must be positive")
                if not args.tasks or len(args.tasks) != 1:
                    raise ValueError("retry requires exactly one --task")
                if not (output / "runs.sqlite3").is_file():
                    raise ValueError(f"run store does not exist: {output / 'runs.sqlite3'}")
                RunStore(output / "runs.sqlite3").retry(output.name, args.tasks[0], args.repetition)
            executor = LocalExecutor() if args.local else DockerExecutor(adapter)
            runner = EvaluationRunner(
                suite,
                adapter,
                executor,
                output_dir=output,
                repetitions=repetitions,
                selected_tasks=set(args.tasks) if args.tasks else None,
                jobs=args.jobs,
                resume=args.command in {"resume", "retry"},
            )
            records = runner.run()
            _print_summary(output)
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            if summary.get("infra_error_runs", 0):
                return 3
            capability_failed = summary.get("passed_runs", 0) < summary.get("scored_runs", 0)
            return 0 if not capability_failed or args.exit_zero_on_capability_failure else 1
        if args.command == "swebench":
            data, config_path = _config(args.config)
            adapter = YucodeAdapter(data)
            for label in (
                "repetitions",
                "agent_timeout",
                "grader_timeout",
                "max_steps",
                "max_workers",
                "jobs",
            ):
                if getattr(args, label) <= 0:
                    raise ValueError(f"--{label.replace('_', '-')} must be positive")
            if args.limit is not None and args.limit <= 0:
                raise ValueError("--limit must be positive")
            output = _output(args.output, suite="swebench-verified", agent=adapter.name)
            records = run_swebench(
                adapter=adapter,
                config_path=config_path,
                output_dir=output,
                dataset_name=args.dataset,
                split=args.split,
                instance_ids=args.instance_ids,
                limit=args.limit,
                repetitions=args.repetitions,
                agent_timeout_seconds=args.agent_timeout,
                grader_timeout_seconds=args.grader_timeout,
                max_steps=args.max_steps,
                max_workers=args.max_workers,
                jobs=args.jobs,
            )
            _print_summary(output)
            if any(record.status == "infra_error" for record in records):
                return 3
            return 0 if records and all(record.passed for record in records) else 1
    except (EvalConfigError, DockerError, SwebenchUnavailable, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
