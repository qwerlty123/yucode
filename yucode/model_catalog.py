"""声明式的模型能力与供应商兼容性数据。

该目录是建议性的,而非白名单。未匹配的供应商和模型名仍走 yucode 的通用协议路径,
而 ``provider_compat`` 会把这些记录在案的例外折叠进最终解析出的请求策略。
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ModelRuleData(TypedDict):
    """由模型家族前缀或记录在案的版本模式选出的值。"""

    value: str
    prefixes: NotRequired[tuple[str, ...]]
    pattern: NotRequired[str]


class ModelEffortRuleData(TypedDict):
    """某个模型家族支持的规范化 effort 级别。"""

    levels: tuple[str, ...]
    prefixes: NotRequired[tuple[str, ...]]
    pattern: NotRequired[str]


BuiltinToolRuleData = dict[str, object]


class CompatibilityData(TypedDict, total=False):
    """编译进单个供应商兼容性档案的数据。"""

    model_capabilities: tuple[str, ...]
    api_rules: tuple[ModelRuleData, ...]
    chat_reasoning: str
    chat_reasoning_rules: tuple[ModelRuleData, ...]
    chat_reasoning_history: str
    chat_reasoning_history_rules: tuple[ModelRuleData, ...]
    reasoning_effort_levels: tuple[str, ...]
    reasoning_effort_level_rules: tuple[ModelEffortRuleData, ...]
    reasoning_effort_off_rules: tuple[ModelRuleData, ...]
    responses_reasoning_effort_off_rules: tuple[ModelRuleData, ...]
    responses_reasoning_models: tuple[str, ...] | None
    prompt_cache_key: bool
    strict_tools: bool
    strict_beta: bool
    suppress_temperature: bool
    suppress_temperature_models: tuple[str, ...]
    # 供应商侧(内置)工具是原样透传的供应商原生 JSON。每条规则是一个必备的 JSON 子集,
    # 使目录能区分类型相同但生命周期不同的条目(例如 Kimi 的 builtin_function/$web_search)。
    # ``None`` 表示未知主机继续走通用透传;空映射表示该已知供应商通过 ``tools`` 数组
    # 不提供任何受支持的供应商侧工具。
    builtin_tools_by_wire: dict[str, tuple[BuiltinToolRuleData, ...]] | None


class ProviderData(CompatibilityData):
    """一个具名供应商,以及其策略生效的主机域名。"""

    hosts: tuple[str, ...]


class AnthropicModelData(TypedDict):
    adaptive_min_version: tuple[int, int]
    xhigh_min_version: tuple[int, int]
    always_thinking_families: tuple[str, ...]
    effort_levels: tuple[str, ...]


REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")

# 为什么:手动 thinking API 使用整数 token 预算,而模型原生 effort API 只暴露 yucode
# 规范化 effort 标尺的子集。``provider_compat`` 应用共用的就近级别回退;预算表是线格式
# 上的取值,而非 effort 兼容性规则。
# 依据:https://platform.claude.com/docs/en/build-with-claude/extended-thinking
#           https://api-docs.deepseek.com/guides/thinking_mode/
#           https://docs.qwencloud.com/api-reference/chat/openai-chat
THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32768,
}

ANTHROPIC_MODELS: AnthropicModelData = {
    "adaptive_min_version": (4, 6),
    "xhigh_min_version": (4, 7),
    "always_thinking_families": ("fable", "mythos"),
    "effort_levels": ("low", "medium", "high", "xhigh", "max"),
}

# OpenAI 的 effort 支持随模型代际而异。未知的未来模型仍走通用透传路径;
# 只有记录在案的家族会被折叠到就近接受的级别。
# 依据:https://developers.openai.com/api/docs/guides/latest-model
#           https://developers.openai.com/api/docs/models/gpt-5.5
#           https://developers.openai.com/api/docs/models/gpt-5.4-pro
#           https://developers.openai.com/api/docs/models/gpt-5.3-codex
#           https://developers.openai.com/api/docs/models/gpt-5.1
#           https://developers.openai.com/api/docs/models/gpt-5
OPENAI_EFFORT_CAPABILITY: CompatibilityData = {
    "reasoning_effort_level_rules": (
        {"levels": ("low", "medium", "high", "xhigh", "max"), "pattern": r"gpt-5\.6(?:-|$)"},
        {"levels": ("medium", "high", "xhigh"), "pattern": r"gpt-5\.(?:2|4|5)-pro(?:-|$)"},
        {"levels": ("low", "medium", "high", "xhigh"), "pattern": r"gpt-5\.(?:2|3)-codex(?:-|$)"},
        {"levels": ("low", "medium", "high", "xhigh"), "pattern": r"gpt-5\.(?:2|4|5)(?:-|$)"},
        {"levels": ("low", "medium", "high"), "pattern": r"gpt-5\.1(?:-|$)"},
        {"levels": ("high",), "pattern": r"gpt-5-pro(?:-|$)"},
        {"levels": ("minimal", "low", "medium", "high"), "pattern": r"gpt-5(?:-|$)"},
        {"levels": ("low", "medium", "high"), "pattern": r"o[1-4](?:-|$)"},
    ),
    "reasoning_effort_off_rules": (
        {"value": "medium", "pattern": r"gpt-5\.(?:2|4|5)-pro(?:-|$)"},
        {"value": "low", "pattern": r"gpt-5\.(?:2|3)-codex(?:-|$)"},
        {"value": "high", "pattern": r"gpt-5-pro(?:-|$)"},
        {"value": "none", "pattern": r"gpt-5\.(?:[1-9]\d*)(?:-|$)"},
    ),
    "responses_reasoning_effort_off_rules": (
        {"value": "medium", "pattern": r"gpt-5\.(?:2|4|5)-pro(?:-|$)"},
        {"value": "low", "pattern": r"gpt-5\.(?:2|3)-codex(?:-|$)"},
        {"value": "high", "pattern": r"gpt-5-pro(?:-|$)"},
        {"value": "none", "pattern": r"gpt-5\.(?:[1-9]\d*)(?:-|$)"},
    ),
}

MODEL_CAPABILITIES: dict[str, CompatibilityData] = {
    "openai_effort": OPENAI_EFFORT_CAPABILITY,
    # DeepSeek V4 使用 thinking.type,并接受 low/high/max 以及模型特有的 xhigh 兼容级别。
    # 依据:https://api-docs.deepseek.com/guides/thinking_mode/
    "deepseek_v4": {
        "chat_reasoning_rules": ({"value": "thinking", "prefixes": ("deepseek-v4-",)},),
        "reasoning_effort_level_rules": ({"levels": ("low", "high", "xhigh", "max"), "prefixes": ("deepseek-v4-",)},),
    },
    # K3 使用规范化 effort,K2.5/K2.6 使用 thinking.type,K2.7 始终开启思考。
    # K3 与 K2.7 跨轮次保留思考;开放平台上 K3 无法关闭思考。
    # 依据:https://platform.kimi.com/docs/guide/use-thinking-models
    "kimi_open": {
        "chat_reasoning_rules": (
            {"value": "reasoning_effort", "prefixes": ("kimi-k3",)},
            {"value": "thinking_toggle", "prefixes": ("kimi-k2.5", "kimi-k2.6")},
            {"value": "mandatory_thinking", "prefixes": ("kimi-k2.7-code",)},
        ),
        "chat_reasoning_history_rules": ({"value": "all", "prefixes": ("kimi-k3", "kimi-k2.7-code")},),
        "reasoning_effort_level_rules": ({"levels": ("low", "high", "max"), "prefixes": ("kimi-k3",)},),
        "reasoning_effort_off_rules": ({"value": "low", "prefixes": ("kimi-k3",)},),
    },
    # Z.AI 标准 API 对 GLM-4.5+ 使用 thinking.type,对 GLM-5.2+ 使用 reasoning_effort。
    # 依据:https://docs.z.ai/guides/capabilities/thinking-mode
    "zai_standard": {
        "chat_reasoning_rules": (
            {"value": "thinking_effort", "prefixes": ("glm-5.2",)},
            {"value": "thinking_toggle", "prefixes": ("glm-4.5", "glm-4.6", "glm-4.7", "glm-5")},
        ),
    },
    # OpenCode 目前对其记录在案的 GLM-5 家族暴露相同的控制项。
    # 依据:https://opencode.ai/docs/zen
    "zai_opencode": {
        "chat_reasoning_rules": (
            {"value": "thinking_effort", "prefixes": ("glm-5.2",)},
            {"value": "thinking_toggle", "prefixes": ("glm-5",)},
        ),
    },
    # Qwen3.8 Chat 使用顶层 reasoning_effort,包括用 none 关闭思考。
    # 依据:https://docs.qwencloud.com/api-reference/chat/openai-chat
    "qwen3_8": {
        "chat_reasoning_rules": ({"value": "reasoning_effort", "prefixes": ("qwen3.8-",)},),
        "reasoning_effort_off_rules": ({"value": "none", "prefixes": ("qwen3.8-",)},),
    },
    # Kimi Code 的模型 ID 与开放平台的 K3 off 语义均不同。
    # 依据:https://www.kimi.com/code/docs/kimi-code/models.html
    "kimi_code": {
        "chat_reasoning_rules": (
            {"value": "reasoning_effort", "prefixes": ("k3",)},
            {"value": "mandatory_thinking", "prefixes": ("kimi-for-coding",)},
        ),
        "chat_reasoning_history_rules": ({"value": "all", "prefixes": ("k3", "kimi-for-coding")},),
        "reasoning_effort_level_rules": ({"levels": ("low", "high", "max"), "prefixes": ("k3",)},),
        "reasoning_effort_off_rules": ({"value": "none", "prefixes": ("k3",)},),
    },
}


PROVIDER_CATALOG: dict[str, ProviderData] = {
    # 为什么:Chat Completions 仅对推理型模型家族接受 reasoning_effort,
    # 而严格函数 schema 是 OpenAI 的能力,而非通用默认行为。
    # GPT-5.1 及以后的 Responses 模型把 `none` 记录为无推理的 effort;最初的 GPT-5 则没有。
    # 可选的 Responses reasoning 对象只限于这些推理家族:GPT-4.1 支持 Responses,但明确是非推理型。
    # 推理家族拒绝 temperature,而 gpt-4o 等同类聊天模型仍保留它。
    # 依据:https://developers.openai.com/api/docs/guides/reasoning
    #           https://developers.openai.com/api/docs/models/gpt-5
    #           https://developers.openai.com/api/docs/models/gpt-5.1
    #           https://developers.openai.com/api/docs/models/gpt-4.1
    #           https://developers.openai.com/api/docs/guides/function-calling#strict-mode
    "openai": {
        "hosts": ("api.openai.com",),
        "model_capabilities": ("openai_effort",),
        "chat_reasoning_rules": ({"value": "reasoning_effort", "prefixes": ("o", "gpt-5")},),
        "responses_reasoning_models": ("o", "gpt-5"),
        "strict_tools": True,
        "suppress_temperature_models": ("o", "gpt-5"),
        # 为什么:OpenAI 在 Responses API 上记录了供应商侧 web_search;Chat Completions
        # 拒绝非 function 类型的工具条目。目前只支持 web_search;其他服务器工具
        # 需要文件/容器/媒体的审批生命周期先行落地。
        # 依据:https://developers.openai.com/api/docs/guides/tools-web-search
        "builtin_tools_by_wire": {"responses": ({"type": "web_search"},)},
    },
    # 为什么:OpenRouter 在其顶层 reasoning 对象之后统一规范各供应商。
    # 依据:https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
    "openrouter": {
        "hosts": ("openrouter.ai",),
        "chat_reasoning": "reasoning",
        # 为什么:OpenRouter 把服务器工具记录为 Chat 或 Responses tools 数组中的
        # `openrouter:*` 条目。旧的 `plugins`/`:online` 搜索配置已弃用。
        # 依据:https://openrouter.ai/docs/guides/features/server-tools/overview
        "builtin_tools_by_wire": {
            "chat": (
                {"type": "openrouter:web_search"},
                {"type": "openrouter:web_fetch"},
                {"type": "openrouter:datetime"},
            ),
            "responses": (
                {"type": "openrouter:web_search"},
                {"type": "openrouter:web_fetch"},
                {"type": "openrouter:datetime"},
            ),
        },
    },
    # 为什么:同一个 OpenCode base URL 按模型复用多种线协议,因此 api=auto 无法从 URL
    # 推断协议:Claude 和 Qwen 走 Messages,GPT 和 Grok 走 Responses,其余走 Chat Completions。
    # 其模型特质复用上面规范的 effort 能力;只有路由是 OpenCode 供应商特有的覆盖层。
    # 依据:https://opencode.ai/docs/zen
    "opencode": {
        "hosts": ("opencode.ai",),
        "model_capabilities": ("openai_effort", "deepseek_v4", "zai_opencode", "kimi_open"),
        "api_rules": (
            {"value": "anthropic", "prefixes": ("claude-", "qwen")},
            {"value": "responses", "prefixes": ("gpt-", "grok-")},
        ),
        # 为什么:Zen 只记录端点路由;它的 websearch/webfetch 是客户端工具,
        # 并非 Zen API 的服务器工具,因此不假定存在任何供应商侧工具。
        # 依据:https://opencode.ai/docs/zen
        "builtin_tools_by_wire": {},
    },
    # 为什么:DeepSeek 使用 thinking.type 加缩减后的 effort 标尺,不定义 OpenAI 的
    # prompt_cache_key,且严格函数 schema 需要 /beta 端点。普通轮次可以省略 reasoning,
    # 但每条助手工具调用消息都必须保留它。
    # 依据:https://api-docs.deepseek.com/guides/thinking_mode/
    #           https://api-docs.deepseek.com/api/create-chat-completion/
    #           https://api-docs.deepseek.com/guides/tool_calls
    #           https://api-docs.deepseek.com/guides/thinking_mode
    "deepseek": {
        "hosts": ("api.deepseek.com",),
        "model_capabilities": ("deepseek_v4",),
        "chat_reasoning": "thinking",
        "chat_reasoning_history": "tool_calls",
        "reasoning_effort_levels": ("low", "high", "xhigh", "max"),
        "prompt_cache_key": False,
        "strict_tools": True,
        "strict_beta": True,
        # 为什么:DeepSeek 的 Chat schema 只接受 function 工具;它没有供应商侧工具。
        # 依据:https://api-docs.deepseek.com/api/create-chat-completion/
        "builtin_tools_by_wire": {},
    },
    # 为什么:Qwen 默认忽略上一轮的 reasoning,而工具循环应重放它。
    # 显式的 preserve_thinking=true 在请求时被折叠进参数。
    # 依据:https://platform.qianwenai.com/docs/developer-guides/text-generation/thinking
    "qwen": {
        "hosts": ("aliyuncs.com",),
        "model_capabilities": ("qwen3_8",),
        "chat_reasoning_history": "current_turn",
        # 为什么:Qwen Responses 把 web_search/web_extractor 记录为供应商侧工具,而
        # Qwen Chat Completions 在请求体里配置搜索。其余 Responses 工具需要
        # 输出/资源生命周期支持先行落地。
        # 依据:https://help.aliyun.com/en/model-studio/web-search
        #           https://help.aliyun.com/en/model-studio/web-extractor
        "builtin_tools_by_wire": {"responses": ({"type": "web_search"}, {"type": "web_extractor"})},
    },
    # 为什么:国际版与中国版 Kimi 开放平台在不同区域域名上暴露相同的模型控制项。
    # 它们的 temperature 取值固定;显式的 thinking.keep="all" 在请求时折叠进参数。
    # 依据:https://platform.kimi.ai/docs/guide/use-kimi-k2-thinking-model
    #           https://platform.kimi.com/docs/guide/use-kimi-k2-thinking-model
    #           https://platform.kimi.ai/docs/api/models-overview
    #           https://platform.kimi.ai/docs/api/chat
    "kimi_open": {
        "hosts": ("moonshot.ai", "moonshot.cn"),
        "model_capabilities": ("kimi_open",),
        "chat_reasoning_history": "current_turn",
        "reasoning_effort_levels": ("low", "high", "max"),
        "strict_tools": True,
        "suppress_temperature": True,
        # 为什么:Kimi 的内置函数($web_search)是模型回调的 Chat 工具条目。
        # 依据:https://platform.kimi.ai/docs/guide/use-web-search
        "builtin_tools_by_wire": {"chat": ({"type": "builtin_function", "function": {"name": "$web_search"}},)},
    },
    # 为什么:Kimi Code 是独立的订阅 API,其官方客户端工具(WebSearch、FetchURL)
    # 在客户端运行;不存在编码端点的服务器工具契约。
    # 依据:https://platform.kimi.ai/docs/api/chat
    "kimi_code": {
        "hosts": ("kimi.com",),
        "model_capabilities": ("kimi_code",),
        "reasoning_effort_levels": ("low", "high", "max"),
        "builtin_tools_by_wire": {},
    },
    # 为什么:Z.AI 两个区域共用 thinking 控制与自动上下文缓存。
    # 依据:https://docs.z.ai/guides/capabilities/thinking
    #           https://docs.z.ai/guides/capabilities/cache
    "zai": {
        "hosts": ("z.ai",),
        "model_capabilities": ("zai_standard",),
        "chat_reasoning_history": "current_turn",
        "prompt_cache_key": False,
        # 为什么:Z.AI 的 web_search 条目位于 Chat tools 数组;检索与服务器 MCP
        # 需要各自的生命周期处理落地后才能提供。
        # 依据:https://docs.z.ai/guides/tools/web-search
        "builtin_tools_by_wire": {"chat": ({"type": "web_search", "web_search": {}},)},
    },
    # 为什么:中国区 BigModel 端点记录的是相同的 thinking 与自动缓存契约。
    # 依据:https://docs.bigmodel.cn/cn/guide/capabilities/thinking
    #           https://docs.bigmodel.cn/cn/guide/capabilities/cache
    "bigmodel": {
        "hosts": ("bigmodel.cn",),
        "model_capabilities": ("zai_standard",),
        "chat_reasoning_history": "current_turn",
        "prompt_cache_key": False,
        "builtin_tools_by_wire": {"chat": ({"type": "web_search", "web_search": {}},)},
    },
    # 为什么:Anthropic 服务器工具(web_search_20250305)是 Messages 工具定义;目前只提供
    # 经过测试的 web search 版本。OpenCode Zen 只记录端点路由,没有网关服务器工具契约,
    # 因此不为其假定任何供应商侧工具。
    # 依据:https://platform.claude.com/docs/en/build-with-claude/tool-use
    "anthropic": {
        "hosts": ("api.anthropic.com",),
        "builtin_tools_by_wire": {"anthropic": ({"type": "web_search_20250305", "name": "web_search"},)},
    },
}
