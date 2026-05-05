from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .experiment import comparability_certificate, read_experiment_manifest
from .models import RunRecord, load_records, summarize, write_json


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_report(summary: dict[str, Any], records: list[RunRecord]) -> str:
    comparable = "formal / comparable" if summary["comparable"] else "debug / not comparable"
    cost = summary["estimated_cost_usd"]
    cost_text = f"${cost:.4f}" if cost is not None else "not configured"
    lines = [
        f"# Evaluation report: {summary.get('experiment_id') or 'empty'}",
        "",
        f"- Agent: `{summary.get('agent') or 'n/a'}`",
        f"- Mode: **{comparable}**",
        f"- Tasks / runs: **{summary['tasks']} / {summary['runs']}**",
        f"- pass@1: **{_percent(summary['pass_at_1'])}**",
        f"- pass@k: **{_percent(summary['pass_at_k'])}**",
        f"- all@k: **{_percent(summary['all_at_k'])}**",
        (
            f"- Run success: **{_percent(summary['run_success_rate'])}** "
            f"(95% Wilson CI {_percent(summary['run_success_wilson_95']['low'])}–"
            f"{_percent(summary['run_success_wilson_95']['high'])})"
        ),
        f"- Tokens: **{summary['total_tokens']:,}**",
        f"- Estimated cost: **{cost_text}**",
        f"- Total wall time: **{summary['duration_seconds']:.1f}s**",
        "",
        "## Task results",
        "",
        "| Task | Passed | pass@1 | pass@k | all@k | Statuses |",
        "|---|---:|---:|---:|---:|---|",
    ]
    lines[4:4] = [
        f"- Applicability: **{summary['applicable_runs']} applicable / {summary['not_applicable_runs']} N/A**",
        f"- Infrastructure failures: **{summary['infra_error_runs']}**",
        f"- Evidence incomplete: **{summary['incomplete_evidence_runs']}**",
    ]
    for task in summary["task_results"]:
        lines.append(
            f"| `{task['task_id']}` | {task['passed_runs']}/{task['runs']} | "
            f"{'yes' if task['pass_at_1'] else 'no'} | "
            f"{'yes' if task['pass_at_k'] else 'no'} | "
            f"{'yes' if task['all_at_k'] else 'no'} | "
            f"{', '.join(task['statuses'])} |"
        )

    if summary.get("subject_results"):
        lines.extend(
            [
                "",
                "## Results by subject",
                "",
                "| Subject | Passed | Scored runs | Success |",
                "|---|---:|---:|---:|",
            ]
        )
        for item in summary["subject_results"]:
            rate = "N/A" if item["success_rate"] is None else _percent(item["success_rate"])
            lines.append(f"| `{item['subject']}` | {item['passed']} | {item['runs']} | {rate} |")
    if summary.get("capability_results"):
        lines.extend(
            [
                "",
                "## Results by capability",
                "",
                "| Capability | Passed / scored | N/A | Infra | Success |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for item in summary["capability_results"]:
            rate = "N/A" if item["success_rate"] is None else _percent(item["success_rate"])
            lines.append(f"| `{item['capability']}` | {item['passed']}/{item['runs']} | {item['not_applicable']} | {item['infra']} | {rate} |")

    failures: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        if not record.passed and record.applicability == "applicable":
            failures[record.status].append(record)
    if failures:
        lines.extend(["", "## Failures", ""])
        for status, failed_records in sorted(failures.items()):
            lines.append(f"### {status} ({len(failed_records)})")
            lines.append("")
            for record in failed_records:
                detail = f": {record.error}" if record.error else ""
                code = f" [{record.primary_failure.code}]" if record.primary_failure else ""
                lines.append(f"- `{record.task_id}` repetition {record.repetition}, attempt {record.attempt}{code}{detail}")
            lines.append("")
    not_applicable = [record for record in records if record.applicability == "not_applicable"]
    if not_applicable:
        lines.extend(["", "## Not applicable", ""])
        for record in not_applicable:
            unmet = record.metadata.get("unmet_requirements", [])
            lines.append(f"- `{record.task_id}`: {', '.join(unmet) or 'preflight requirement unmet'}")
    return "\n".join(lines).rstrip() + "\n"


def write_report(run_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(run_dir).resolve()
    records = load_records(directory)
    summary = summarize(records)
    summary_path = directory / "summary.json"
    report_path = directory / "report.md"
    write_json(summary_path, summary)
    report_path.write_text(render_report(summary, records), encoding="utf-8")
    return summary_path, report_path


def compare_results(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    allowed_differences: tuple[str, ...] = (),
) -> dict[str, Any]:
    baseline_records = load_records(baseline_path)
    candidate_records = load_records(candidate_path)
    baseline = summarize(baseline_records)
    candidate = summarize(candidate_records)
    certificate = comparability_certificate(
        read_experiment_manifest(baseline_path),
        read_experiment_manifest(candidate_path),
        allowed_differences=allowed_differences,
    )

    baseline_runs = {(record.task_id, record.repetition): record for record in baseline_records}
    candidate_runs = {(record.task_id, record.repetition): record for record in candidate_records}
    common = sorted(baseline_runs.keys() & candidate_runs.keys())
    regressions = []
    improvements = []
    for key in common:
        old = baseline_runs[key]
        new = candidate_runs[key]
        item = {"task_id": key[0], "repetition": key[1]}
        if certificate.comparable and old.passed and not new.passed:
            regressions.append(item)
        elif certificate.comparable and not old.passed and new.passed:
            improvements.append(item)

    def metric(name: str) -> dict[str, Any]:
        old = baseline[name]
        new = candidate[name]
        comparable_values = isinstance(old, (int, float)) and isinstance(new, (int, float))
        return {
            "baseline": old,
            "candidate": new,
            "delta": new - old if certificate.comparable and comparable_values else None,
            "ratio": new / old if certificate.comparable and comparable_values and old else None,
        }

    return {
        "schema_version": 2,
        "baseline_experiment": baseline["experiment_id"],
        "candidate_experiment": candidate["experiment_id"],
        "paired_runs": len(common),
        "baseline_only_runs": len(baseline_runs.keys() - candidate_runs.keys()),
        "candidate_only_runs": len(candidate_runs.keys() - baseline_runs.keys()),
        "comparability_certificate": certificate.to_dict(),
        "metrics": {
            name: metric(name)
            for name in (
                "pass_at_1",
                "pass_at_k",
                "all_at_k",
                "run_success_rate",
                "total_tokens",
                "estimated_cost_usd",
                "duration_seconds",
            )
        },
        "regressions": regressions,
        "improvements": improvements,
    }
