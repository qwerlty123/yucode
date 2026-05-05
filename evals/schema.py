from __future__ import annotations

import glob
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


class EvalConfigError(ValueError):
    """Raised when an evaluation manifest is invalid or unsafe."""


NetworkMode = Literal["offline", "provider-only", "full"]
CapabilitySubject = Literal["agent_capability", "agent_mechanism", "product_interface", "eval_harness"]
CapabilitySupport = Literal["shipped", "conditional"]
DriverKind = Literal["yucode", "command"]

REQUIRED_SUCCESS_CONDITIONS = (
    "within_budget",
    "verifier_passed",
    "expected_artifact_exists",
    "normal_success_stop",
)


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    subject: CapabilitySubject
    support: CapabilitySupport
    profiles: tuple[str, ...]
    claim: str
    requirements: tuple[str, ...]
    evidence: tuple[str, ...]
    exclusions: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionProfile:
    id: str
    driver: DriverKind
    tools: tuple[str, ...]
    requirements: tuple[str, ...]
    release_eligible: bool = True


@dataclass(frozen=True)
class CapabilityCatalog:
    schema_version: int
    path: Path
    capabilities: dict[str, CapabilitySpec]
    profiles: dict[str, ExecutionProfile]


@dataclass(frozen=True)
class ApplicabilityDecision:
    applicable: bool
    unmet: tuple[str, ...] = ()
    checked: tuple[str, ...] = ()

    @property
    def status(self) -> Literal["applicable", "not_applicable"]:
        return "applicable" if self.applicable else "not_applicable"


@dataclass(frozen=True)
class TaskLimits:
    max_agent_steps: int | None = None
    agent_timeout_seconds: int | None = None
    grader_timeout_seconds: int | None = None
    memory: str | None = None
    cpus: float | None = None
    pids: int | None = None


@dataclass(frozen=True)
class SuiteDefaults:
    repetitions: int = 3
    agent_timeout_seconds: int = 1800
    grader_timeout_seconds: int = 900
    max_steps: int = 200
    jobs: int = 1
    network: NetworkMode = "provider-only"


@dataclass(frozen=True)
class SourceSpec:
    type: Literal["local", "git"]
    path: Path | None = None
    url: str | None = None
    revision: str | None = None
    expected_digest: str | None = None


@dataclass(frozen=True)
class EnvironmentSpec:
    image: str | None = None
    dockerfile: Path | None = None
    context: Path | None = None
    platform: str | None = None
    workdir: str = "/workspace"
    network: NetworkMode | None = None
    expected_digest: str | None = None


@dataclass(frozen=True)
class GraderSpec:
    path: Path
    command: tuple[str, ...]
    result_file: str = "grade.json"
    gold_patch: Path | None = None
    base_must_fail: bool = False


@dataclass(frozen=True)
class TaskSpec:
    id: str
    manifest_path: Path
    prompt_path: Path
    source: SourceSpec
    environment: EnvironmentSpec
    grader: GraderSpec
    tags: tuple[str, ...] = ()
    difficulty: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 2
    targets: tuple[str, ...] = ()
    profile: str = "coding_default"
    category: str = "coding"
    allowed_tools: tuple[str, ...] = ()
    step_budget: int | None = None
    expected_artifact: str | None = None
    requirements: dict[str, bool] = field(default_factory=dict)
    success_policy: tuple[str, ...] = ()
    protocol_checks: tuple[str, ...] = ()
    safety_checks: tuple[str, ...] = ()
    limits: TaskLimits = field(default_factory=TaskLimits)
    scenario_path: Path | None = None
    attachments: tuple[str, ...] = ()
    release_eligible: bool = False


@dataclass(frozen=True)
class SuiteSpec:
    schema_version: int
    name: str
    manifest_path: Path
    defaults: SuiteDefaults
    tasks: tuple[TaskSpec, ...]
    catalog: CapabilityCatalog | None = None


def _string_list(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value) or any(not isinstance(item, str) or not item.strip() for item in value):
        qualifier = "non-empty " if required else ""
        raise EvalConfigError(f"{label} must be a {qualifier}array of strings")
    return tuple(str(item) for item in value)


def _bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise EvalConfigError(f"{label} must be a boolean")
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:
        raise EvalConfigError(f"manifest does not exist: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise EvalConfigError(f"invalid TOML in {path}: {exc}") from exc


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvalConfigError(f"{label} must be a TOML table")
    return value


def _positive_int(value: Any, label: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EvalConfigError(f"{label} must be a positive integer")
    return value


def _optional_positive_float(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise EvalConfigError(f"{label} must be a positive number")
    return float(value)


def _optional_nonempty_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvalConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _network(value: Any, label: str, default: NetworkMode) -> NetworkMode:
    mode = default if value is None else value
    if mode not in {"offline", "provider-only", "full"}:
        raise EvalConfigError(f"{label} must be one of: offline, provider-only, full")
    return mode


def _digest(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise EvalConfigError(f"{label} must be a sha256:<64 hex characters> digest")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise EvalConfigError(f"{label} must be a sha256:<64 hex characters> digest") from exc
    return value.lower()


def _safe_relative(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise EvalConfigError(f"{label} must be a non-empty path")
    raw = Path(value)
    if raw.is_absolute():
        raise EvalConfigError(f"{label} must be relative to {base}")
    resolved = (base / raw).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise EvalConfigError(f"{label} escapes its task directory: {value}") from exc
    return resolved


def _command(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise EvalConfigError(f"{label} must be a non-empty array of strings")
    return tuple(value)


def _catalog_path() -> Path:
    return Path(__file__).with_name("catalog.toml").resolve()


def load_catalog(path: str | Path | None = None) -> CapabilityCatalog:
    source = Path(path).expanduser().resolve() if path is not None else _catalog_path()
    data = _read_toml(source)
    version = data.get("schema_version", 1)
    if version != 1:
        raise EvalConfigError(f"unsupported capability catalog schema_version: {version}")

    raw_profiles = _require_mapping(data.get("profiles"), "catalog.profiles")
    profiles: dict[str, ExecutionProfile] = {}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(profile_id, str) or not profile_id:
            raise EvalConfigError("catalog profile id must be a non-empty string")
        profile_data = _require_mapping(raw, f"profiles.{profile_id}")
        driver = profile_data.get("driver")
        if driver not in {"yucode", "command"}:
            raise EvalConfigError(f"profiles.{profile_id}.driver is invalid: {driver}")
        tools = _string_list(profile_data.get("tools", []), f"profiles.{profile_id}.tools")
        requirements = _string_list(profile_data.get("requirements", []), f"profiles.{profile_id}.requirements")
        profiles[profile_id] = ExecutionProfile(
            id=profile_id,
            driver=driver,
            tools=tools,
            requirements=requirements,
            release_eligible=_bool(profile_data.get("release_eligible"), f"profiles.{profile_id}.release_eligible", True),
        )

    raw_capabilities = data.get("capabilities")
    if not isinstance(raw_capabilities, list) or not raw_capabilities:
        raise EvalConfigError("catalog.capabilities must be a non-empty array of tables")
    capabilities: dict[str, CapabilitySpec] = {}
    for position, raw in enumerate(raw_capabilities):
        item = _require_mapping(raw, f"catalog.capabilities[{position}]")
        capability_id = item.get("id")
        subject = item.get("subject")
        support = item.get("support")
        claim = item.get("claim")
        if not isinstance(capability_id, str) or not capability_id:
            raise EvalConfigError(f"catalog.capabilities[{position}].id must be a non-empty string")
        if capability_id in capabilities:
            raise EvalConfigError(f"duplicate capability id: {capability_id}")
        if subject not in {"agent_capability", "agent_mechanism", "product_interface", "eval_harness"}:
            raise EvalConfigError(f"capability {capability_id} has invalid subject: {subject}")
        if support not in {"shipped", "conditional"}:
            raise EvalConfigError(f"capability {capability_id} has invalid support: {support}")
        if not isinstance(claim, str) or not claim.strip():
            raise EvalConfigError(f"capability {capability_id} must have a claim")
        capability_profiles = _string_list(item.get("profiles", []), f"capability {capability_id}.profiles")
        missing_profiles = sorted(set(capability_profiles) - profiles.keys())
        if missing_profiles:
            raise EvalConfigError(f"capability {capability_id} references unknown profiles: {', '.join(missing_profiles)}")
        requirements = _string_list(item.get("requirements", []), f"capability {capability_id}.requirements")
        if support == "conditional" and not requirements:
            raise EvalConfigError(f"conditional capability {capability_id} must declare requirements")
        evidence = _string_list(item.get("evidence", []), f"capability {capability_id}.evidence", required=True)
        capabilities[capability_id] = CapabilitySpec(
            id=capability_id,
            subject=subject,
            support=support,
            profiles=capability_profiles,
            claim=claim.strip(),
            requirements=requirements,
            evidence=evidence,
            exclusions=_string_list(item.get("exclusions", []), f"capability {capability_id}.exclusions"),
        )
    return CapabilityCatalog(version, source, capabilities, profiles)


def catalog_digest(catalog: CapabilityCatalog) -> str:
    return "sha256:" + hashlib.sha256(catalog.path.read_bytes()).hexdigest()


def evaluate_applicability(
    task: TaskSpec,
    catalog: CapabilityCatalog,
    available: dict[str, bool] | None = None,
) -> ApplicabilityDecision:
    context = dict(available or {})
    profile = catalog.profiles[task.profile]
    required = list(profile.requirements)
    for target in task.targets:
        required.extend(catalog.capabilities[target].requirements)
    required.extend(name for name, expected in task.requirements.items() if expected)
    checked = tuple(dict.fromkeys(required))
    unmet = tuple(name for name in checked if not context.get(name, False))
    return ApplicabilityDecision(not unmet, unmet, checked)


def _load_defaults(data: dict[str, Any]) -> SuiteDefaults:
    return SuiteDefaults(
        repetitions=_positive_int(data.get("repetitions"), "defaults.repetitions", 3),
        agent_timeout_seconds=_positive_int(
            data.get("agent_timeout_seconds"),
            "defaults.agent_timeout_seconds",
            1800,
        ),
        grader_timeout_seconds=_positive_int(
            data.get("grader_timeout_seconds"),
            "defaults.grader_timeout_seconds",
            900,
        ),
        max_steps=_positive_int(data.get("max_steps"), "defaults.max_steps", 200),
        jobs=_positive_int(data.get("jobs"), "defaults.jobs", 1),
        network=_network(data.get("network"), "defaults.network", "provider-only"),
    )


def _load_task(path: Path, defaults: SuiteDefaults, catalog: CapabilityCatalog) -> TaskSpec:
    data = _read_toml(path)
    task_schema = data.get("schema_version")
    if task_schema != 2:
        raise EvalConfigError(f"unsupported task schema_version in {path}")

    task_dir = path.parent.resolve()
    task_id = data.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise EvalConfigError(f"task id must be a non-empty string in {path}")

    prompt_path = _safe_relative(task_dir, data.get("prompt", "prompt.md"), f"{task_id}.prompt")
    if not prompt_path.is_file():
        raise EvalConfigError(f"prompt does not exist for {task_id}: {prompt_path}")

    source_data = _require_mapping(data.get("source"), f"{task_id}.source")
    source_type = source_data.get("type", "local")
    if source_type == "local":
        source_path = _safe_relative(task_dir, source_data.get("path", "source"), f"{task_id}.source.path")
        if not source_path.is_dir():
            raise EvalConfigError(f"source directory does not exist for {task_id}: {source_path}")
        source = SourceSpec(
            type="local",
            path=source_path,
            expected_digest=_digest(source_data.get("expected_digest"), f"{task_id}.source.expected_digest"),
        )
    elif source_type == "git":
        url = source_data.get("url")
        revision = source_data.get("revision")
        if not isinstance(url, str) or not url:
            raise EvalConfigError(f"{task_id}.source.url is required for git sources")
        if not isinstance(revision, str) or not revision:
            raise EvalConfigError(f"{task_id}.source.revision is required for git sources")
        source = SourceSpec(
            type="git",
            url=url,
            revision=revision,
            expected_digest=_digest(source_data.get("expected_digest"), f"{task_id}.source.expected_digest"),
        )
    else:
        raise EvalConfigError(f"unsupported source type for {task_id}: {source_type}")

    environment_data = _require_mapping(data.get("environment", {}), f"{task_id}.environment")
    image = environment_data.get("image")
    if image is not None and (not isinstance(image, str) or not image):
        raise EvalConfigError(f"{task_id}.environment.image must be a string")
    dockerfile_value = environment_data.get("dockerfile")
    context_value = environment_data.get("context")
    dockerfile = _safe_relative(task_dir, dockerfile_value, f"{task_id}.environment.dockerfile") if dockerfile_value is not None else None
    context = (
        _safe_relative(task_dir, context_value, f"{task_id}.environment.context")
        if context_value is not None
        else (dockerfile.parent if dockerfile is not None else None)
    )
    if image is None and dockerfile is None:
        raise EvalConfigError(f"{task_id}.environment requires either image or dockerfile")
    if image is not None and dockerfile is not None:
        raise EvalConfigError(f"{task_id}.environment cannot set both image and dockerfile")
    if dockerfile is not None and not dockerfile.is_file():
        raise EvalConfigError(f"Dockerfile does not exist for {task_id}: {dockerfile}")
    if context is not None and not context.is_dir():
        raise EvalConfigError(f"Docker build context does not exist for {task_id}: {context}")
    if dockerfile is not None and context is not None:
        try:
            dockerfile.relative_to(context)
        except ValueError as exc:
            raise EvalConfigError(f"{task_id}.environment.dockerfile must be inside its build context") from exc
    platform = environment_data.get("platform")
    if platform is not None and (not isinstance(platform, str) or not platform):
        raise EvalConfigError(f"{task_id}.environment.platform must be a string")
    workdir = environment_data.get("workdir", "/workspace")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise EvalConfigError(f"{task_id}.environment.workdir must be absolute")
    network = _network(
        environment_data.get("network"),
        f"{task_id}.environment.network",
        defaults.network,
    )
    environment = EnvironmentSpec(
        image=image,
        dockerfile=dockerfile,
        context=context,
        platform=platform,
        workdir=workdir,
        network=network,
        expected_digest=_digest(environment_data.get("expected_digest"), f"{task_id}.environment.expected_digest"),
    )

    grader_data = _require_mapping(data.get("grader"), f"{task_id}.grader")
    grader_path = _safe_relative(task_dir, grader_data.get("path", "grader"), f"{task_id}.grader.path")
    if not grader_path.is_dir():
        raise EvalConfigError(f"grader directory does not exist for {task_id}: {grader_path}")
    grader_command = _command(grader_data.get("command"), f"{task_id}.grader.command")
    result_file = grader_data.get("result_file", "grade.json")
    if not isinstance(result_file, str) or not result_file or Path(result_file).is_absolute() or ".." in Path(result_file).parts:
        raise EvalConfigError(f"{task_id}.grader.result_file must be a safe relative path")
    gold_value = grader_data.get("gold_patch")
    gold_patch = _safe_relative(task_dir, gold_value, f"{task_id}.grader.gold_patch") if gold_value is not None else None
    if gold_patch is not None and not gold_patch.is_file():
        raise EvalConfigError(f"gold patch does not exist for {task_id}: {gold_patch}")

    protected_paths = [grader_path]
    if gold_patch is not None:
        protected_paths.append(gold_patch)
    if context is not None:
        for protected in protected_paths:
            try:
                protected.resolve().relative_to(context.resolve())
            except ValueError:
                continue
            raise EvalConfigError(f"{task_id}: Docker build context contains hidden grading material: {protected}")

    tags_value = data.get("tags", [])
    if not isinstance(tags_value, list) or any(not isinstance(tag, str) or not tag for tag in tags_value):
        raise EvalConfigError(f"{task_id}.tags must be an array of strings")
    difficulty = data.get("difficulty")
    if difficulty is not None and not isinstance(difficulty, str):
        raise EvalConfigError(f"{task_id}.difficulty must be a string")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise EvalConfigError(f"{task_id}.metadata must be a TOML table")
    try:
        json.dumps(metadata)
    except (TypeError, ValueError) as exc:
        raise EvalConfigError(f"{task_id}.metadata must contain JSON values") from exc
    base_must_fail = grader_data.get("base_must_fail", False)
    if not isinstance(base_must_fail, bool):
        raise EvalConfigError(f"{task_id}.grader.base_must_fail must be a boolean")

    targets = _string_list(data.get("targets"), f"{task_id}.targets", required=True)
    unknown_targets = sorted(set(targets) - catalog.capabilities.keys())
    if unknown_targets:
        raise EvalConfigError(f"{task_id}.targets contains unknown capabilities: {', '.join(unknown_targets)}")
    profile_value = data.get("profile")
    if not isinstance(profile_value, str) or profile_value not in catalog.profiles:
        raise EvalConfigError(f"{task_id}.profile must name a catalog profile")
    profile = profile_value
    for target in targets:
        declared = catalog.capabilities[target].profiles
        if declared and profile not in declared:
            raise EvalConfigError(f"{task_id}: capability {target} is not available in profile {profile}")
    category_value = data.get("category")
    if not isinstance(category_value, str) or not category_value.strip():
        raise EvalConfigError(f"{task_id}.category must be a non-empty string")
    category = category_value.strip()
    if "allowed_tools" not in data:
        raise EvalConfigError(f"{task_id}.allowed_tools is required")
    allowed_tools = _string_list(data.get("allowed_tools"), f"{task_id}.allowed_tools")
    profile_tools = set(catalog.profiles[profile].tools)
    outside_profile = sorted(set(allowed_tools) - profile_tools)
    if outside_profile:
        raise EvalConfigError(f"{task_id}.allowed_tools exceed profile {profile}: {', '.join(outside_profile)}")
    from yucode.tools import TOOL_REGISTRY

    unknown_tools = sorted(set(allowed_tools) - TOOL_REGISTRY.keys())
    if unknown_tools:
        raise EvalConfigError(f"{task_id}.allowed_tools contains unknown tools: {', '.join(unknown_tools)}")
    if "step_budget" not in data:
        raise EvalConfigError(f"{task_id}.step_budget is required")
    step_budget = _positive_int(data.get("step_budget"), f"{task_id}.step_budget", 1)
    expected_value = data.get("expected_artifact")
    _safe_relative(task_dir, expected_value, f"{task_id}.expected_artifact")
    expected_artifact = str(expected_value)
    raw_requirements = _require_mapping(data.get("requirements", {}), f"{task_id}.requirements")
    for name, value in raw_requirements.items():
        if not isinstance(name, str) or not name or not isinstance(value, bool):
            raise EvalConfigError(f"{task_id}.requirements must map names to booleans")
    requirements = dict(raw_requirements)
    success = _require_mapping(data.get("success"), f"{task_id}.success")
    success_policy = _string_list(success.get("require"), f"{task_id}.success.require", required=True)
    missing_conditions = [condition for condition in REQUIRED_SUCCESS_CONDITIONS if condition not in success_policy]
    if missing_conditions:
        raise EvalConfigError(f"{task_id}.success.require is missing: {', '.join(missing_conditions)}")
    protocol_checks = _string_list(data.get("protocol_checks", []), f"{task_id}.protocol_checks")
    safety_checks = _string_list(data.get("safety_checks", []), f"{task_id}.safety_checks")
    limits_data = _require_mapping(data.get("limits", {}), f"{task_id}.limits")
    limits = TaskLimits(
        max_agent_steps=_positive_int(limits_data.get("max_agent_steps"), f"{task_id}.limits.max_agent_steps", defaults.max_steps),
        agent_timeout_seconds=_positive_int(
            limits_data.get("agent_timeout_seconds"), f"{task_id}.limits.agent_timeout_seconds", defaults.agent_timeout_seconds
        ),
        grader_timeout_seconds=_positive_int(
            limits_data.get("grader_timeout_seconds"), f"{task_id}.limits.grader_timeout_seconds", defaults.grader_timeout_seconds
        ),
        memory=_optional_nonempty_string(limits_data.get("memory"), f"{task_id}.limits.memory"),
        cpus=_optional_positive_float(limits_data.get("cpus"), f"{task_id}.limits.cpus"),
        pids=(_positive_int(limits_data.get("pids"), f"{task_id}.limits.pids", 256) if limits_data.get("pids") is not None else None),
    )
    scenario_path: Path | None = None
    attachments: tuple[str, ...] = ()
    scenario_value = data.get("scenario")
    if scenario_value is not None:
        scenario_path = _safe_relative(task_dir, scenario_value, f"{task_id}.scenario")
        if not scenario_path.is_file():
            raise EvalConfigError(f"scenario does not exist for {task_id}: {scenario_path}")
    attachment_values = _string_list(data.get("attachments", []), f"{task_id}.attachments")
    for attachment in attachment_values:
        raw_attachment = Path(attachment)
        if raw_attachment.is_absolute() or ".." in raw_attachment.parts:
            raise EvalConfigError(f"{task_id}.attachments must stay inside the prepared source: {attachment}")
        if source.path is not None and not (source.path / raw_attachment).is_file():
            raise EvalConfigError(f"{task_id}.attachment does not exist in source: {attachment}")
    attachments = attachment_values
    release_eligible = catalog.profiles[profile].release_eligible

    return TaskSpec(
        id=task_id,
        manifest_path=path.resolve(),
        prompt_path=prompt_path,
        source=source,
        environment=environment,
        grader=GraderSpec(
            path=grader_path,
            command=grader_command,
            result_file=result_file,
            gold_patch=gold_patch,
            base_must_fail=base_must_fail,
        ),
        tags=tuple(tags_value),
        difficulty=difficulty,
        metadata=dict(metadata),
        schema_version=task_schema,
        targets=targets,
        profile=profile,
        category=category,
        allowed_tools=allowed_tools,
        step_budget=step_budget,
        expected_artifact=expected_artifact,
        requirements=requirements,
        success_policy=success_policy,
        protocol_checks=protocol_checks,
        safety_checks=safety_checks,
        limits=limits,
        scenario_path=scenario_path,
        attachments=attachments,
        release_eligible=release_eligible,
    )


def load_suite(manifest: str | Path) -> SuiteSpec:
    path = Path(manifest).expanduser().resolve()
    data = _read_toml(path)
    schema_version = data.get("schema_version")
    if schema_version != 2:
        raise EvalConfigError(f"unsupported suite schema_version: {schema_version}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise EvalConfigError("suite name must be a non-empty string")
    patterns = data.get("tasks")
    if not isinstance(patterns, list) or not patterns or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
        raise EvalConfigError("suite tasks must be a non-empty array of glob patterns")

    defaults = _load_defaults(_require_mapping(data.get("defaults", {}), "suite.defaults"))
    suite_dir = path.parent.resolve()
    catalog_value = data.get("catalog")
    if catalog_value is None:
        catalog = load_catalog()
    else:
        catalog = load_catalog(_safe_relative(suite_dir, catalog_value, "suite.catalog"))
    task_paths: set[Path] = set()
    for pattern in patterns:
        raw_pattern = Path(pattern)
        if raw_pattern.is_absolute() or ".." in raw_pattern.parts:
            raise EvalConfigError(f"task glob must stay inside the suite directory: {pattern}")
        matches = [Path(item).resolve() for item in glob.glob(str(suite_dir / pattern))]
        if not matches:
            raise EvalConfigError(f"task glob matched no files: {pattern}")
        for match in matches:
            try:
                match.relative_to(suite_dir)
            except ValueError as exc:
                raise EvalConfigError(f"task manifest escapes suite directory: {match}") from exc
            if not match.is_file():
                raise EvalConfigError(f"task manifest is not a file: {match}")
            task_paths.add(match)

    tasks = tuple(_load_task(task_path, defaults, catalog) for task_path in sorted(task_paths))
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise EvalConfigError(f"duplicate task ids: {', '.join(duplicates)}")
    return SuiteSpec(
        schema_version=schema_version,
        name=name,
        manifest_path=path,
        defaults=defaults,
        tasks=tasks,
        catalog=catalog,
    )
