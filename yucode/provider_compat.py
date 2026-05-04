"""编译供应商/模型目录数据并解析协议兼容性。

目录描述记录在案的主机/模型差异。本模块负责通用匹配与 effort 回退;
Chat、Responses 与 Anthropic 路径仍各自负责自己的线格式。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from yucode.model_catalog import (
    ANTHROPIC_MODELS,
    MODEL_CAPABILITIES,
    PROVIDER_CATALOG,
    REASONING_LEVELS,
    BuiltinToolRuleData,
    ModelEffortRuleData,
    ModelRuleData,
    ProviderData,
)


@dataclass(frozen=True)
class ModelMatch:
    """所有目录规则共用的模型选择器:家族前缀或记录在案的模式。"""

    prefixes: tuple[str, ...] = ()
    pattern: str = ""

    def matches(self, model: str) -> bool:
        return any(model.startswith(prefix) for prefix in self.prefixes) or bool(self.pattern and re.match(self.pattern, model))


@dataclass(frozen=True)
class ModelRule(ModelMatch):
    """由模型家族前缀或记录在案的版本模式选出的值。"""

    value: str = ""


@dataclass(frozen=True)
class ModelEffortRule(ModelMatch):
    """按模型家族选择的受支持的规范化 effort。"""

    levels: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompatibilityProfile:
    """仅记录主机与通用协议行为的差异之处。"""

    api_rules: tuple[ModelRule, ...] = ()
    chat_reasoning: str | None = None
    chat_reasoning_rules: tuple[ModelRule, ...] = ()
    chat_reasoning_history: str = "all"
    chat_reasoning_history_rules: tuple[ModelRule, ...] = ()
    reasoning_effort_levels: tuple[str, ...] = ()
    reasoning_effort_level_rules: tuple[ModelEffortRule, ...] = ()
    reasoning_effort_off_rules: tuple[ModelRule, ...] = ()
    responses_reasoning_effort_off_rules: tuple[ModelRule, ...] = ()
    responses_reasoning_models: tuple[str, ...] | None = None
    prompt_cache_key: bool = True
    strict_tools: bool = False
    strict_beta: bool = False
    suppress_temperature: bool = False
    suppress_temperature_models: tuple[str, ...] = ()
    # 供应商侧工具策略:哪些解析出的线协议可以承载哪些供应商原生 JSON 子集。
    # ``None`` 表示未知主机保持通用透传;空映射表示没有任何线协议接受工具。
    builtin_tools_by_wire: dict[str, tuple[BuiltinToolRuleData, ...]] | None = None

    @staticmethod
    def rule_value(rules: tuple[ModelRule, ...], model: str) -> str | None:
        return next((rule.value for rule in rules if rule.matches(model)), None)

    def reasoning_effort_value(self, model: str, effort: str) -> str:
        levels = next((rule.levels for rule in self.reasoning_effort_level_rules if rule.matches(model)), self.reasoning_effort_levels)
        return nearest_reasoning_effort(effort, levels)


@dataclass(frozen=True)
class ResolvedProvider:
    """显式配置与兼容性折叠后生效的传输策略。"""

    api: str
    base_url: str
    host: str
    chat_reasoning: str
    chat_reasoning_history: str
    reasoning_effort: str | None
    responses_reasoning: bool
    suppress_temperature: bool
    prompt_cache_key: bool
    strict_tools_active: bool
    builtin_tools_by_wire: dict[str, tuple[BuiltinToolRuleData, ...]] | None = None


@dataclass(frozen=True)
class BuiltinToolsIssue:
    """一条已知供应商不兼容项,可直接用于请求与命令反馈。"""

    reason: Literal["wire", "entry"]
    configured: tuple[str, ...]
    supported_wires: tuple[str, ...] = ()
    supported_entries: tuple[str, ...] = ()


def _builtin_tool_label(entry: Mapping[str, object]) -> str:
    tool_type = str(entry.get("type") or "?")
    function = entry.get("function")
    if tool_type == "builtin_function" and isinstance(function, Mapping):
        name = str(function.get("name") or "")
        if name:
            return f"{tool_type}/{name}"
    requirements = []
    for key, value in entry.items():
        if key == "type":
            continue
        requirements.append(f"{key} object" if isinstance(value, Mapping) else f"{key}={value}")
    if requirements:
        return f"{tool_type} ({', '.join(requirements)})"
    return tool_type


def _matches_builtin_tool_rule(entry: Mapping[str, object], rule: Mapping[str, object]) -> bool:
    """判断 entry 是否包含目录规则要求的每个字面量或嵌套字段。"""

    for key, expected in rule.items():
        if key not in entry:
            return False
        actual = entry[key]
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping) or not _matches_builtin_tool_rule(actual, expected):
                return False
        elif actual != expected:
            return False
    return True


def builtin_tools_issue(resolved: ResolvedProvider, entries: tuple[Mapping[str, object], ...]) -> BuiltinToolsIssue | None:
    """返回已知的兼容性问题,未知主机则保持透传。"""

    policy = resolved.builtin_tools_by_wire
    if policy is None or not entries:
        return None
    configured = tuple(_builtin_tool_label(entry) for entry in entries)
    rules = policy.get(resolved.api)
    if rules is None:
        return BuiltinToolsIssue("wire", configured, supported_wires=tuple(sorted(policy)))
    unsupported = tuple(_builtin_tool_label(entry) for entry in entries if not any(_matches_builtin_tool_rule(entry, rule) for rule in rules))
    if unsupported:
        return BuiltinToolsIssue("entry", unsupported, supported_entries=tuple(_builtin_tool_label(rule) for rule in rules))
    return None


def nearest_reasoning_effort(effort: str, supported: tuple[str, ...]) -> str:
    """返回最接近的受支持规范化 effort,并列时偏向更高一级。"""

    if effort not in REASONING_LEVELS:
        return effort
    ranks = {level: rank for rank, level in enumerate(REASONING_LEVELS)}
    candidates = tuple(level for level in supported if level in ranks)
    if not candidates:
        return effort
    target = ranks[effort]
    return min(candidates, key=lambda level: (abs(ranks[level] - target), -ranks[level]))


def _model_rules(*groups: tuple[ModelRuleData, ...]) -> tuple[ModelRule, ...]:
    return tuple(ModelRule(rule.get("prefixes", ()), rule.get("pattern", ""), rule["value"]) for group in groups for rule in group)


def _effort_rules(*groups: tuple[ModelEffortRuleData, ...]) -> tuple[ModelEffortRule, ...]:
    return tuple(ModelEffortRule(rule.get("prefixes", ()), rule.get("pattern", ""), rule["levels"]) for group in groups for rule in group)


ModelRuleField = Literal[
    "api_rules",
    "chat_reasoning_rules",
    "chat_reasoning_history_rules",
    "reasoning_effort_off_rules",
    "responses_reasoning_effort_off_rules",
]


def _capability_model_rules(data: ProviderData, field: ModelRuleField) -> tuple[tuple[ModelRuleData, ...], ...]:
    groups: list[tuple[ModelRuleData, ...]] = []
    for name in data.get("model_capabilities", ()):
        capability = MODEL_CAPABILITIES[name]
        groups.append(cast(tuple[ModelRuleData, ...], capability.get(field, ())))
    return tuple(groups)


def _capability_effort_rules(data: ProviderData) -> tuple[tuple[ModelEffortRuleData, ...], ...]:
    return tuple(MODEL_CAPABILITIES[name].get("reasoning_effort_level_rules", ()) for name in data.get("model_capabilities", ()))


def _compatibility_profile(data: ProviderData) -> CompatibilityProfile:
    """编译单个供应商覆盖层及其可复用的模型能力集合。"""

    return CompatibilityProfile(
        api_rules=_model_rules(data.get("api_rules", ()), *_capability_model_rules(data, "api_rules")),
        chat_reasoning=data.get("chat_reasoning"),
        chat_reasoning_rules=_model_rules(data.get("chat_reasoning_rules", ()), *_capability_model_rules(data, "chat_reasoning_rules")),
        chat_reasoning_history=data.get("chat_reasoning_history", "all"),
        chat_reasoning_history_rules=_model_rules(data.get("chat_reasoning_history_rules", ()), *_capability_model_rules(data, "chat_reasoning_history_rules")),
        reasoning_effort_levels=data.get("reasoning_effort_levels", ()),
        reasoning_effort_level_rules=_effort_rules(data.get("reasoning_effort_level_rules", ()), *_capability_effort_rules(data)),
        reasoning_effort_off_rules=_model_rules(data.get("reasoning_effort_off_rules", ()), *_capability_model_rules(data, "reasoning_effort_off_rules")),
        responses_reasoning_effort_off_rules=_model_rules(
            data.get("responses_reasoning_effort_off_rules", ()),
            *_capability_model_rules(data, "responses_reasoning_effort_off_rules"),
        ),
        responses_reasoning_models=data.get("responses_reasoning_models"),
        prompt_cache_key=data.get("prompt_cache_key", True),
        strict_tools=data.get("strict_tools", False),
        strict_beta=data.get("strict_beta", False),
        suppress_temperature=data.get("suppress_temperature", False),
        suppress_temperature_models=data.get("suppress_temperature_models", ()),
        builtin_tools_by_wire=data.get("builtin_tools_by_wire"),
    )


def _compatibility_profiles(catalog: Mapping[str, ProviderData] = PROVIDER_CATALOG) -> dict[str, CompatibilityProfile]:
    profiles: dict[str, CompatibilityProfile] = {}
    for data in catalog.values():
        profile = _compatibility_profile(data)
        for host in data["hosts"]:
            if host in profiles:
                raise ValueError(f"duplicate provider compatibility host: {host}")
            profiles[host] = profile
    return profiles


COMPATIBILITY_PROFILES = _compatibility_profiles()
_FAMILY_SPLIT_RE = re.compile(r"[^0-9a-z]+")


def anthropic_model_version(model: str) -> tuple[int, int] | None:
    """返回 Claude 模型 ID 中第一个短数字代数(若有)。"""

    tokens = [token for token in _FAMILY_SPLIT_RE.split(model.lower()) if token]
    for index, token in enumerate(tokens):
        if not (token.isdigit() and len(token) <= 2):
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        minor = int(following) if following.isdigit() and len(following) <= 2 else 0
        return int(token), minor
    return None


def anthropic_thinking_params(model: str, reasoning: str, effort: str, budget_tokens: int) -> dict[str, object]:
    """为已知的 Claude 代际构造记录在案的 thinking 字段。

    未知别名保持不配置。网关可能把这类名称指向自适应思考边界的任一侧,
    而猜测会把一个有效别名变成 400 响应。
    """

    # 为什么:4.5 及更早要求手动 thinking;4.6 推荐 adaptive;4.7+ 拒绝手动 thinking。
    # Opus 4.5 是唯一将手动 thinking 与 output_config.effort 结合的模型。
    # 依据:https://platform.claude.com/docs/en/build-with-claude/extended-thinking
    #           https://platform.claude.com/docs/en/build-with-claude/effort
    version = anthropic_model_version(model)
    if version is None:
        return {}
    families = _FAMILY_SPLIT_RE.split(model.lower())
    adaptive = version >= ANTHROPIC_MODELS["adaptive_min_version"]
    always_thinking = any(family in families for family in ANTHROPIC_MODELS["always_thinking_families"])
    if reasoning == "off":
        return {"thinking": {"type": "disabled"}} if adaptive and not always_thinking else {}
    level = nearest_reasoning_effort(effort, ANTHROPIC_MODELS["effort_levels"]) if effort in REASONING_LEVELS else "high"
    if not adaptive:
        params: dict[str, object] = {"thinking": {"type": "enabled", "budget_tokens": budget_tokens}}
        if version == (4, 5) and "opus" in families:
            params["output_config"] = {"effort": level if level in ("low", "medium", "high") else "high"}
        return params
    if level == "xhigh" and version < ANTHROPIC_MODELS["xhigh_min_version"]:
        level = "max"
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}}


def anthropic_thinking_always_on(model: str) -> bool:
    families = _FAMILY_SPLIT_RE.split(model.lower())
    return any(family in families for family in ANTHROPIC_MODELS["always_thinking_families"])


def anthropic_keeps_prior_thinking(model: str) -> bool:
    """Claude 是否在其有效上下文中保留此前轮次的思考。"""

    # Opus 4.5 及所有带编号的 4.6+ 模型保留并计费全部历史思考。Sonnet/Haiku 4.5
    # 及更早的模型只保留最新一轮;未知别名保持保守处理。
    # 无论此区别如何,当前轮次的 thinking 块都是工具使用所必需的。
    # 依据:https://platform.claude.com/docs/en/build-with-claude/thinking
    version = anthropic_model_version(model)
    if version is None:
        return True
    families = _FAMILY_SPLIT_RE.split(model.lower())
    return version >= ANTHROPIC_MODELS["adaptive_min_version"] or (version == (4, 5) and "opus" in families)


def compatibility_for_host(host: str, profiles: Mapping[str, CompatibilityProfile] = COMPATIBILITY_PROFILES) -> CompatibilityProfile:
    """在尊重标签边界的前提下返回最具体的域名档案。"""

    matches = ((domain, profile) for domain, profile in profiles.items() if host == domain or host.endswith(f".{domain}"))
    return max(matches, key=lambda item: len(item[0]), default=("", CompatibilityProfile()))[1]
