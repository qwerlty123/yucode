from __future__ import annotations

import json
import tomllib
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExperimentMatrix:
    schema_version: int
    base_profile: str
    repetitions: int
    variants: dict[str, dict[str, Any]]
    controlled_fields: tuple[str, ...]
    expected_differences: tuple[str, ...]
    subject: str
    single_factor: bool = True


@dataclass(frozen=True)
class ComparabilityCertificate:
    status: str
    comparable: bool
    allowed_differences: tuple[str, ...]
    mismatches: tuple[dict[str, Any], ...]
    checked_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReleaseGateResult:
    passed: bool
    checks: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_matrix(path: str | Path) -> ExperimentMatrix:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid experiment matrix {source}: {exc}") from exc
    version = data.get("schema_version", 1)
    if version != 1:
        raise ValueError(f"unsupported experiment matrix schema_version: {version}")
    profile = data.get("base_profile")
    repetitions = data.get("repetitions", 1)
    variants = data.get("variants", {})
    controlled = data.get("controlled_fields", [])
    expected = data.get("expected_differences", [])
    subject = data.get("subject")
    single_factor = data.get("single_factor", True)
    if not isinstance(profile, str) or not profile:
        raise ValueError("matrix.base_profile must be a non-empty string")
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions <= 0:
        raise ValueError("matrix.repetitions must be a positive integer")
    if not isinstance(variants, dict) or len(variants) < 2 or any(not isinstance(item, dict) for item in variants.values()):
        raise ValueError("matrix.variants must contain at least two tables")
    if not isinstance(controlled, list) or any(not isinstance(item, str) or not item for item in controlled):
        raise ValueError("matrix.controlled_fields must be an array of strings")
    if not isinstance(expected, list) or any(not isinstance(item, str) or not item for item in expected):
        raise ValueError("matrix.expected_differences must be an array of strings")
    if not isinstance(subject, str) or subject not in {"agent_capability", "agent_mechanism", "product_interface", "eval_harness"}:
        raise ValueError("matrix.subject is invalid")
    if not isinstance(single_factor, bool):
        raise TypeError("matrix.single_factor must be a boolean")
    if single_factor:
        logical_factors = {field for variant in variants.values() for field in variant}
        if len(logical_factors) != 1:
            raise ValueError("single-factor matrix variants must change exactly one logical factor")
    actual = {field for variant in variants.values() for field in variant}
    undeclared = sorted(actual - set(expected))
    if undeclared:
        raise ValueError(f"matrix variants change undeclared fields: {', '.join(undeclared)}")
    return ExperimentMatrix(
        schema_version=version,
        base_profile=profile,
        repetitions=repetitions,
        variants={str(name): dict(value) for name, value in variants.items()},
        controlled_fields=tuple(controlled),
        expected_differences=tuple(expected),
        subject=subject,
        single_factor=single_factor,
    )


def resolve_variant_config(config: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply the small, explicit set of live-agent matrix factors.

    Matrix field names match the public experiment manifest so the same names
    can be used by the comparability certificate.
    """

    resolved = deepcopy(config)
    provider_root = resolved.get("provider")
    if not isinstance(provider_root, dict):
        provider_root = {}
        resolved["provider"] = provider_root
    active = str(provider_root.get("active", "default"))

    def active_table() -> dict[str, Any]:
        value = provider_root.get(active)
        if isinstance(value, dict):
            return value
        return provider_root

    allowed = {
        "model",
        "api",
        "reasoning",
        "temperature",
        "max_tokens",
        "strict_tools",
        "prompt_cache_key",
        "image_input",
    }
    for field, value in changes.items():
        if field == "agent_config.provider":
            if not isinstance(value, str) or value not in provider_root or not isinstance(provider_root[value], dict):
                raise ValueError(f"matrix provider does not exist in config: {value}")
            active = value
            provider_root["active"] = value
            continue
        prefix = "agent_config."
        if not field.startswith(prefix) or field.removeprefix(prefix) not in allowed:
            raise ValueError(f"unsupported matrix factor: {field}")
        active_table()[field.removeprefix(prefix)] = value
    return resolved


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in sorted(value.items()):
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(item, path))
        return result
    if isinstance(value, list):
        return {prefix: value}
    return {prefix: value}


def comparability_certificate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    allowed_differences: tuple[str, ...] = (),
) -> ComparabilityCertificate:
    ignored = {"experiment_id", "started_at", "finished_at", "output_dir"}
    left = _flatten(baseline)
    right = _flatten(candidate)
    checked: list[str] = []
    mismatches: list[dict[str, Any]] = []
    if baseline.get("comparable") is not True or candidate.get("comparable") is not True:
        mismatches.append(
            {
                "field": "comparable",
                "baseline": baseline.get("comparable"),
                "candidate": candidate.get("comparable"),
                "reason": "LocalDebug runs are descriptive only",
            }
        )

    def allowed(path: str) -> bool:
        return any(path == item or path.startswith(item + ".") for item in allowed_differences)

    for path in sorted(left.keys() | right.keys()):
        if path.split(".", 1)[0] in ignored:
            continue
        checked.append(path)
        if left.get(path) != right.get(path) and not allowed(path):
            mismatches.append({"field": path, "baseline": left.get(path), "candidate": right.get(path)})
    comparable = not mismatches
    status = "comparable_with_declared_variants" if comparable and allowed_differences else "comparable" if comparable else "not_comparable"
    return ComparabilityCertificate(status, comparable, allowed_differences, tuple(mismatches), tuple(checked))


def read_experiment_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    if source.is_dir():
        source = source / "experiment.json"
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid experiment manifest {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"experiment manifest must be an object: {source}")
    return value


def evaluate_release_gate(
    summary: dict[str, Any],
    *,
    comparison: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> ReleaseGateResult:
    config = policy or {}
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, actual: Any, expected: Any) -> None:
        checks.append({"name": name, "passed": passed, "actual": actual, "expected": expected})

    infra = int(summary.get("infra_error_runs", 0))
    check("infra_error_zero", infra == 0, infra, 0)
    comparable_run = bool(summary.get("comparable", False))
    check("formal_comparable_run", comparable_run, comparable_run, True)
    incomplete = int(summary.get("incomplete_evidence_runs", 0))
    check("evidence_complete", incomplete == 0, incomplete, 0)
    if comparison is not None:
        certificate = comparison.get("comparability_certificate", {})
        comparable = bool(certificate.get("comparable"))
        check("comparison_comparable", comparable, certificate.get("status"), "comparable")
        regressions = len(comparison.get("regressions", [])) if comparable else 0
        check("no_paired_regression", comparable and regressions == 0, regressions, 0)
    for metric in ("pass_at_1", "all_at_k"):
        if metric in config:
            threshold = float(config[metric])
            actual = float(summary.get(metric, 0.0))
            check(metric, actual >= threshold, actual, f">={threshold}")
    capability_policy = config.get("capabilities", {})
    if not isinstance(capability_policy, dict):
        raise TypeError("gate.capabilities must be a table")
    capability_results = {item["capability"]: item for item in summary.get("capability_results", [])}
    for capability, thresholds in sorted(capability_policy.items()):
        if not isinstance(thresholds, dict):
            raise TypeError(f"gate.capabilities.{capability} must be a table")
        result = capability_results.get(capability)
        check(f"capability.{capability}.present", result is not None, bool(result), True)
        if result is None:
            continue
        for metric in ("pass_at_1", "all_at_k"):
            if metric not in thresholds:
                continue
            threshold = float(thresholds[metric])
            actual = result.get(metric)
            passed = isinstance(actual, (int, float)) and float(actual) >= threshold
            check(f"capability.{capability}.{metric}", passed, actual, f">={threshold}")
    relative = {
        "max_token_ratio": "total_tokens",
        "max_cost_ratio": "estimated_cost_usd",
        "max_time_ratio": "duration_seconds",
    }
    for policy_name, metric in relative.items():
        if policy_name not in config:
            continue
        threshold = float(config[policy_name])
        ratio = comparison.get("metrics", {}).get(metric, {}).get("ratio") if comparison is not None else None
        check(policy_name, isinstance(ratio, (int, float)) and ratio <= threshold, ratio, f"<={threshold}")
    return ReleaseGateResult(all(item["passed"] for item in checks), tuple(checks))
