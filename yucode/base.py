"""yucode 基础模块:错误类型、文本工具、配置与共享数据类型。"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import shutil
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, ClassVar, Generic, TypeVar
from urllib.parse import urlparse

from prompt_toolkit.utils import get_cwidth

from yucode.model_catalog import REASONING_LEVELS
from yucode.provider_compat import COMPATIBILITY_PROFILES, CompatibilityProfile, ResolvedProvider, compatibility_for_host

try:
    import pygments
    from pygments.token import Token
except ImportError:  # pragma: no cover - 可选的高亮依赖,缺失时静默降级
    pygments = None
    Token = None  # 保留 Token 名称,避免类体内/全局的 Token 查找抛 NameError

__version__ = "0.20.0"

_ResourceT = TypeVar("_ResourceT")

Json = dict[str, Any]
ToolArgs = list[Any]


HTTP_USER_AGENT = "yucode/" + __version__
logging.getLogger("fastmcp.client.auth.oauth").setLevel(logging.WARNING)
# 刷新失败/重新认证会回落到 yucode 自身的处理逻辑,向用户展示可操作的
# "authentication required" 提示;这里压制该 logger 的 ERROR 级 traceback 刷屏
# (包括 yucode 作为控制流主动抛出的 RuntimeError)。
logging.getLogger("mcp.client.auth.oauth2").setLevel(logging.CRITICAL)
DEFAULT_MAX_CONTEXT_TOKENS = 256 * 1024
MAX_TOOL_OUTPUT_TOKENS = 6_000
MODEL_REQUEST_RETRIES = 5
PROVIDER_API_CHOICES = ("auto", "chat", "responses", "anthropic")
IMAGE_INPUT_CHOICES = ("auto", "on", "off")
REASONING_CHOICES = ("off", *REASONING_LEVELS)
CHAT_REASONING_CHOICES = (
    "auto",
    "off",
    "reasoning",
    "reasoning_effort",
    "thinking",
    "thinking_toggle",
    "thinking_effort",
    "enable_thinking",
    "mandatory_thinking",
)
# assistant 轮次把 provider 的原始回复原样挂在这些键下 —— Responses 的 output items
# 与 Anthropic 的 content blocks —— 以便工具循环把协议要求原样回传的不透明推理内容
# 重放回去。它们是 yucode 的内部记账,绝不会进入请求体。
RESPONSES_OUTPUT_KEY = "_responses_output"
ANTHROPIC_CONTENT_KEY = "_anthropic_content"
# provider 端搜索附加在某条 assistant 消息上的来源列表。仅用于渲染与恢复会话,
# 绝不重放:provider 自身的搜索状态已由上面那些 echo 键携带。
SEARCH_SOURCES_KEY = "_search_sources"
# 当 provider 暂停一个耗时的服务端工具运行、以"未结束本轮"的方式结束响应时置位。
# 该消息必须原样回传才能恢复,因此这个标记作为元数据跟随消息一起往返。
PAUSED_TURN_KEY = "_paused_turn"
PROVIDER_ECHO_KEYS = (RESPONSES_OUTPUT_KEY, ANTHROPIC_CONTENT_KEY, SEARCH_SOURCES_KEY, PAUSED_TURN_KEY)


def builtin_function_names(entries: Iterable[Json]) -> tuple[str, ...]:
    """provider 会回调客户端、而非完全独立运行的 builtin 工具名称。

    Kimi 的 builtin functions 在声明方式上与其他 builtin 工具相同,但模型会为它们发出真实的
    tool call 并期望客户端应答,因此 runner(要识别该调用)与 no-tools 守卫(要保留它)
    都必须拿到这些已声明的名称。"""
    names: list[str] = []
    for entry in entries:
        if entry.get("type") != "builtin_function":
            continue  # 只认 builtin_function 类型,其余工具声明跳过
        function = entry.get("function")
        name = function.get("name") if isinstance(function, dict) else ""  # function 可能是非 dict(异常声明),防御性取空
        if isinstance(name, str) and name:  # 名称必须是非空字符串,否则不纳入
            names.append(name)
    return tuple(names)


def builtin_tool_label(name: str) -> str:
    """为 provider 自行运行的某个工具生成展示标签。

    同一个工具在不同协议里名字不同 —— Responses 的 output item 叫 `web_search_call`,
    Messages 的 server tool 叫 `web_search`,Kimi 的 builtin function 叫 `$web_search` ——
    而它们在 transcript 里都应读作同一阶段。"""
    return (name.lstrip("$").removesuffix("_call").replace("_", " ").strip() or "provider tool").title()


# 生命周期/上下文检查点消息的协议无关元数据。provider 适配器会移除这个键,
# 同时保留对话日志中规范的 role/content 对。
SESSION_EVENT_KEY = "_session_event"
# 为单次请求的回答预留的输出空间,不计入输入预算。它是规划层面的储备而非线上参数,
# 因此无论用户是否配置了上限都保持不变(参见 output_token_budget)。
DEFAULT_OUTPUT_RESERVE_TOKENS = 16_384
# 用户未设置时发送到 Anthropic 线路上的保守 `max_tokens`:Anthropic 强制要求该参数,
# 8K 覆盖当前所有模型(Claude 3.5 Haiku 的上限即 8K)。上限更低的旧版 Claude 3
# 模型已从 API 退役。
ANTHROPIC_DEFAULT_MAX_TOKENS = 8_192
# 未设置:Chat 与 Responses 省略该上限,让 provider 采用自身默认值;而 Anthropic
# 线路上该参数必填,因此用 ANTHROPIC_DEFAULT_MAX_TOKENS 代替。
DEFAULT_MAX_TOKENS = 0
MIN_CONTEXT_SAFETY_TOKENS = 4_096


def request_budget_for(max_context_tokens: int, output_budget: int) -> int:
    """单次请求的输入预算:上下文上限减去输出预留与安全余量。

    纯函数,让 ContextManager 与用量记录器共用同一个分母。"""
    # 安全余量:至少 4K,或上下文的 2%((n+49)//50 即向上取整),预算逼近上限时不至于超限
    safety = max(MIN_CONTEXT_SAFETY_TOKENS, (max_context_tokens + 49) // 50)
    return max(1, max_context_tokens - output_budget - safety)  # 至少留 1 token,防止空预算/除零


# 选择器专用哨兵:分别表示"返回上一级"、"自由文本输入"与"用户未作答即关闭"。
SELECTION_BACK = object()
SELECTION_FREE_TEXT = object()
DISMISSED = "(The user dismissed the question without answering.)"


# 异常层次:YucodeError 为根;ConfigError 面向配置错误,ModelError 面向模型请求
# (含超时/截断/畸形 tool call 三个子类),ModelRequestRetry 表示可重试的瞬时故障,
# ToolError 面向工具执行错误。
class YucodeError(Exception): ...


class ConfigError(YucodeError): ...


class ModelError(YucodeError): ...


class ModelResponseTimeout(ModelError): ...


class ModelOutputTruncated(ModelError): ...


class MalformedToolCallError(ModelError): ...


class ModelRequestRetry(YucodeError): ...


class ToolError(YucodeError): ...


class Text:
    BASE36: ClassVar[str] = "0123456789abcdefghijklmnopqrstuvwxyz"

    @staticmethod
    def clean(text: str) -> str:
        # 以 UTF-8 重编码,非法字节替换为 U+FFFD,保证下游拿到的一定是合法文本
        return text.encode("utf-8", errors="replace").decode("utf-8")

    @classmethod
    def base36(cls, value: int) -> str:
        out = ""
        while value:  # 从低位到高位逐位取出 36 进制数字;value 为 0 时循环体不执行
            value, digit = divmod(value, 36)
            out = cls.BASE36[digit] + out
        return out or "0"  # 输入 0 时返回 "0" 而不是空串

    @classmethod
    def value(cls, value: Any) -> Any:
        # 递归清洗:字符串按 UTF-8 修复,字典/列表/元组递归处理,其余类型原样返回
        if isinstance(value, str):
            return cls.clean(value)
        if isinstance(value, dict):
            return {cls.clean(str(key)): cls.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls.value(item) for item in value]
        return value

    @staticmethod
    def elapsed_since(started_at: float, *, precise: bool = False) -> str:
        # 单调时钟不受系统时间调整影响;started_at 为 0 视为未开始,差值钳制到非负
        raw = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        if raw < 60:  # 一分钟内只报秒;precise 时保留一位小数
            return f"{raw:.1f}s" if precise else f"{int(raw)}s"
        minutes, seconds = divmod(int(raw), 60)
        return f"{minutes}m{seconds:02d}s"  # 秒数补零成两位,如 1m05s

    @staticmethod
    def age(seconds: float) -> str:
        """墙钟时长,取仍具意义的最粗粒度单位。`elapsed_since` 用单调时钟测量正在进行的
        轮次;这里读的是存储的时间戳,分钟级别很少有意义。"""
        for unit, size in (("d", 86400.0), ("h", 3600.0), ("m", 60.0)):
            if seconds >= size:  # 从大到小取第一个能整除的单位
                return f"{int(seconds // size)}{unit} ago"
        return "just now"  # 不足一分钟视为刚刚

    @staticmethod
    def clip_width(text: str, width: int) -> str:
        width = max(0, width)  # 钳制为非负,防止负数宽度导致切片反转
        if get_cwidth(text) <= width:  # get_cwidth 按东亚宽字符计宽;已放得下则原样返回
            return text
        ellipsis = "." * min(3, width)  # 省略号最多 3 个点,窄宽度时相应减少
        available = width - get_cwidth(ellipsis)
        clipped = []
        used = 0
        for char in text:
            char_width = max(0, get_cwidth(char))  # 宽字符(中文/emoji)按 2 计
            if used + char_width > available:
                break
            clipped.append(char)
            used += char_width
        return "".join(clipped).rstrip() + ellipsis  # 去尾空白再接省略号,避免悬挂空格

    @staticmethod
    def wrap_styled(
        prefix: list[tuple[str, str]],
        continuation: list[tuple[str, str]],
        content: list[tuple[str, str]],
        width: int | None = None,
    ) -> list[list[tuple[str, str]]]:
        """按显示宽度折行并保留每个片段的样式。

        `content` 为待折行的片段;首行行首附加 `prefix`,续行附加 `continuation`(两者通常
        一个是标签行、一个是等宽对齐的续行前缀)。返回按行切分、相邻同风格片段已合并的
        片段列表,供 UI 直接渲染。width 为 None 表示不限制。"""
        logical_lines: list[list[tuple[str, str, int]]] = [[]]
        for style, text in content:
            for char in text:
                if char == "\n":  # 逻辑行按显式换行切分
                    logical_lines.append([])
                else:
                    logical_lines[-1].append((style, char, get_cwidth(char)))  # 预计算每字符宽度,折行时免重复计算

        def row_segments(row_prefix: list[tuple[str, str]], cells: list[tuple[str, str, int]]) -> list[tuple[str, str]]:
            row = list(row_prefix)
            for style, char, _char_width in cells:
                if row and row[-1][0] == style:
                    row[-1] = (style, row[-1][1] + char)
                else:
                    row.append((style, char))
            return row

        rows: list[list[tuple[str, str]]] = []
        row_prefix = prefix
        for logical in logical_lines:
            remaining = logical
            while True:
                prefix_width = sum(get_cwidth(text) for _style, text in row_prefix)  # 当前行前缀的实际宽度
                available = max(1, width - prefix_width) if width else None  # 续行前缀可能更宽,每轮重算
                if available is None or sum(cell_width for _style, _char, cell_width in remaining) <= available:
                    rows.append(row_segments(row_prefix, remaining))  # 不限制宽度或本行放得下:整行输出
                    break
                used = 0
                fit = 0
                while fit < len(remaining) and used + remaining[fit][2] <= available:  # 贪心数出能容纳的字符数
                    used += remaining[fit][2]
                    fit += 1
                fit = max(1, fit)  # 至少容纳一个字符,防止无限循环
                whitespace = max((index for index in range(fit) if remaining[index][1].isspace()), default=-1)  # 在可容纳范围内找最后一个空白,避免劈词
                cut = whitespace if whitespace > 0 else fit  # whitespace 为 0 表示行首即空白,不能整行丢弃
                rows.append(row_segments(row_prefix, remaining[:cut]))
                remaining = remaining[cut + 1 :] if whitespace > 0 else remaining[cut:]  # 空白处断行连空格一起吞掉
                row_prefix = continuation
            row_prefix = continuation
        return rows


@dataclass
class ProviderConfig:
    """单个 provider 的配置;`resolve` 把它折叠成一次请求所需的完整策略。"""

    COMPATIBILITY: ClassVar[dict[str, CompatibilityProfile]] = COMPATIBILITY_PROFILES

    url: str = ""
    key: str = ""
    model: str = ""
    api: str = "auto"
    stream: bool = True
    image_input: str = "auto"
    prompt_cache_key: str = "auto"
    available_models: tuple[str, ...] = ()
    temperature: float | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    strict_tools: bool = False
    reasoning: str = "medium"
    chat_reasoning: str = "auto"
    timeout: int = 120
    response_timeout: int = 600
    extra_body: Json = field(default_factory=dict)
    builtin_tools: tuple[Json, ...] = ()

    @classmethod
    def from_dict(cls, data: Json) -> ProviderConfig:
        api = Config.str(data, "api", "auto")
        image_input = Config.str(data, "image_input", "auto")
        prompt_cache_key = cls.clean_prompt_cache_key(Config.str(data, "prompt_cache_key", "auto"))
        reasoning = Config.str(data, "reasoning", "medium")
        chat_reasoning = Config.str(data, "chat_reasoning", "auto")
        # 枚举型配置统一校验:取值必须在白名单内,非法值立即报错而不是静默回退,
        # 让拼写错误在配置加载阶段就暴露
        for key, value, choices in (
            ("api", api, PROVIDER_API_CHOICES),
            ("image_input", image_input, IMAGE_INPUT_CHOICES),
            ("reasoning", reasoning, REASONING_CHOICES),
            ("chat_reasoning", chat_reasoning, CHAT_REASONING_CHOICES),
        ):
            if value not in choices:
                raise ConfigError("provider." + key + " must be one of " + ", ".join(choices))
        return cls(
            url=Config.str(data, "url"),
            key=Config.str(data, "key"),
            model=Config.str(data, "model"),
            api=api,
            stream=Config.bool(data, "stream", True),
            image_input=image_input,
            prompt_cache_key=prompt_cache_key,
            available_models=Config.str_tuple(data, "available_models"),
            temperature=Config.float(data, "temperature", None),
            max_tokens=max(0, Config.int(data, "max_tokens", DEFAULT_MAX_TOKENS)),  # 负数钳为 0(=不设上限)
            strict_tools=Config.bool(data, "strict_tools", False),
            reasoning=reasoning,
            chat_reasoning=chat_reasoning,
            timeout=Config.int(data, "timeout", 120),
            response_timeout=max(0, Config.int(data, "response_timeout", 600)),  # 0 = 禁用总时长限制
            extra_body=Config.table(data, "extra_body"),
            builtin_tools=Config.table_tuple(data, "builtin_tools"),
        )

    def builtin_function_names(self) -> tuple[str, ...]:
        """已声明的 builtin functions;runner 会应答这些调用,而不是当作未知工具拒绝。
        依据:https://platform.kimi.ai/docs/guide/use-web-search"""
        return builtin_function_names(self.builtin_tools)

    def resolve(self) -> ResolvedProvider:
        """把显式配置与文档化的兼容性规则折叠成一次请求的完整策略。"""

        # 剥掉协议专属路径后缀,还原 provider 的 base URL
        url = self.url.rstrip("/").removesuffix("/chat/completions").removesuffix("/responses").removesuffix("/messages")
        host = (urlparse(url).hostname or "").lower()  # 主机名小写化,供按域名匹配兼容性档案
        profile = compatibility_for_host(host, self.COMPATIBILITY)
        model = self.model.lower()  # 模型名小写,规则前缀匹配因此不区分大小写

        api = self.api
        if api == "auto":  # 未显式指定时按"URL 路径 → 模型规则 → 默认 chat"的优先级推断
            path = urlparse(self.url.rstrip("/")).path
            suffix_api = next(
                (value for suffix, value in (("/responses", "responses"), ("/messages", "anthropic"), ("/chat/completions", "chat")) if path.endswith(suffix)),
                None,
            )
            api = suffix_api or profile.rule_value(profile.api_rules, model) or "chat"  # 路径推断优先于模型规则

        chat_reasoning = self.chat_reasoning
        if chat_reasoning == "auto":  # chat 协议的推理开关同样按模型规则解析,缺省关闭
            chat_reasoning = profile.rule_value(profile.chat_reasoning_rules, model) or profile.chat_reasoning or "off"

        if self.reasoning == "off":
            # 推理显式关闭时,effort 仍可能被规则要求传(部分模型必须在请求里带 effort)
            reasoning_effort = profile.rule_value(profile.reasoning_effort_off_rules, model)
            if api == "responses":  # Responses API 用独立的关闭规则表,优先于通用规则
                reasoning_effort = profile.rule_value(profile.responses_reasoning_effort_off_rules, model) or reasoning_effort
        else:
            effort = self.reasoning_effort()
            reasoning_effort = profile.reasoning_effort_value(model, effort)  # 把用户级别翻译成该模型支持的取值

        # 兼容性档案可强制抑制 temperature;推理模式下部分 chat 端点也会忽略/报错,主动抑制避免请求失败
        suppress_temperature = profile.suppress_temperature or any(model.startswith(prefix) for prefix in profile.suppress_temperature_models)
        if not suppress_temperature:
            reasoning_enabled = self.reasoning != "off"
            suppress_temperature = reasoning_enabled and chat_reasoning in ("thinking", "enable_thinking")

        # strict tools 需要 provider 支持且协议为 chat/responses(Anthropic 端点不支持)
        strict_tools_active = self.strict_tools and profile.strict_tools and api in ("chat", "responses")
        if strict_tools_active and profile.strict_beta and not url.endswith("/beta"):
            url += "/beta"  # strict tools 走 beta 端点;已带 /beta 则不重复拼接

        return ResolvedProvider(
            api=api,
            base_url=url,
            host=host,
            chat_reasoning=chat_reasoning,
            chat_reasoning_history=profile.rule_value(profile.chat_reasoning_history_rules, model) or profile.chat_reasoning_history,
            reasoning_effort=reasoning_effort,
            responses_reasoning=profile.responses_reasoning_models is None or any(model.startswith(prefix) for prefix in profile.responses_reasoning_models),
            suppress_temperature=suppress_temperature,
            prompt_cache_key=profile.prompt_cache_key,
            strict_tools_active=strict_tools_active,
            builtin_tools_by_wire=profile.builtin_tools_by_wire,
        )

    def reasoning_effort(self) -> str:
        # 非法级别回退到 "medium",保证下游拿到的永远是合法枚举值
        return self.reasoning if self.reasoning in REASONING_LEVELS else "medium"

    def output_token_budget(self) -> int:
        # 未配置上限时按 16K 规划,输入预算据此扣减;纯规划值,不随协议变化
        return self.max_tokens or DEFAULT_OUTPUT_RESERVE_TOKENS

    def anthropic_output_cap(self) -> int:
        """发送到 Anthropic 线路上的 `max_tokens`:已配置的上限,或保守默认值
        (Anthropic 强制要求该参数,Chat 与 Responses 则不然)。"""
        return self.max_tokens or ANTHROPIC_DEFAULT_MAX_TOKENS

    @staticmethod
    def clean_prompt_cache_key(value: str) -> str:
        value = value.strip()  # 去首尾空白,防止配置里手误多打空格导致 key 漂移
        if not value:
            return "auto"  # 空串等价于未设置
        lower = value.lower()
        if lower in {"auto", "off"}:  # 关键字大小写不敏感
            return lower
        # 上限 64 字符且不含空白:保证 key 稳定,可做精确匹配(空白会被服务器规范化处理)
        if len(value) > 64 or any(char.isspace() for char in value):
            raise ConfigError("provider.prompt_cache_key must be auto, off, or a stable key up to 64 chars without whitespace")
        return value  # 其余任意稳定字符串都可作为显式 key 原样使用


@dataclass
class RuntimeSettings:
    shell_timeout: int = 60
    # Bash 前台等待预算:命令在此秒数内未退出时,运行中的进程会被提升为后台任务
    # (见 BashTool.stream_process),控制权带着部分输出先交回模型。设为 0 则禁用提升
    # (回退为在 shell_timeout 时直接杀死进程)。
    bash_wait_timeout: int = 10
    max_steps: int = 200
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    session_retention_days: int = 7
    # 一个模型批次里最多并发执行的只读工具调用数;1 表示禁用并行。
    max_parallel_tools: int = 4
    yolo: bool = False
    quick_hints: bool = True
    theme: str = "auto"

    @classmethod
    def from_dict(cls, data: Json, *, yolo: bool = False, theme: str = "") -> RuntimeSettings:
        runtime = Config.table(data, "runtime")
        # 数值字段全部用 max() 钳制到合法区间,防配置 0/负数破坏下游逻辑
        return cls(
            shell_timeout=Config.int(runtime, "shell_timeout", 60),
            bash_wait_timeout=max(0, Config.int(runtime, "bash_wait_timeout", 10)),  # 负数钳为 0(=禁用提升)
            max_steps=max(1, Config.int(runtime, "max_agent_steps", 200)),  # 至少 1 步,防"0 步直接判死"
            max_context_tokens=max(1, Config.int(runtime, "max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            max_parallel_tools=max(1, Config.int(runtime, "max_parallel_tools", 4)),
            session_retention_days=max(0, Config.int(runtime, "session_retention_days", 7)),
            yolo=yolo or Config.bool(runtime, "yolo", False),  # 命令行参数优先于配置文件
            quick_hints=Config.bool(runtime, "quick_hints", True),
            theme=theme or Config.str(runtime, "theme", "auto"),
        )


@dataclass
class Config:
    active_provider: str = "default"
    providers: dict[str, ProviderConfig] = field(default_factory=lambda: {"default": ProviderConfig()})
    data_dir: str = "~/.yucode"
    mcp: Json = field(default_factory=dict)

    # 向后兼容:数据目录曾先后位于 ~/.nanocode、~/.minacode,最终迁移到 ~/.yucode。
    LEGACY_DATA_DIR: ClassVar[str] = "~/.minacode"

    def __post_init__(self) -> None:
        # 数据目录仍是新默认值但尚未创建、而旧版 ~/.minacode 目录存在时,继续用旧目录,
        # 已有的 sessions、skills 与缓存无需迁移步骤即可被找到。
        if (
            self.data_dir == "~/.yucode"  # 用户显式配置了其他目录则绝不干预
            and not os.path.exists(os.path.expanduser(self.data_dir))
            and os.path.exists(os.path.expanduser(self.LEGACY_DATA_DIR))
        ):
            self.data_dir = self.LEGACY_DATA_DIR  # 只切换路径,不复制任何数据

    @property
    def provider(self) -> ProviderConfig:
        return self.providers[self.active_provider]  # from_dict 已保证 active 存在,这里必然命中

    @classmethod
    def from_dict(cls, data: Json) -> Config:
        provider_root = cls.table(data, "provider")
        active = cls.str(provider_root, "active", "default")
        # "active" 是选择键而非 provider 定义;非 dict 项(如注释残留)直接跳过
        providers = {name: ProviderConfig.from_dict(value) for name, value in provider_root.items() if name != "active" and isinstance(value, dict)}
        if not providers:
            providers = {active: ProviderConfig.from_dict(provider_root)}  # 未命名任何 provider 时,把整个块当作内联的单个定义
        if active not in providers:
            raise ConfigError(f"provider.active `{active}` does not exist")  # 显式检查激活名,给出可读错误而非 KeyError
        paths = cls.table(data, "paths")
        return cls(active_provider=active, providers=providers, data_dir=cls.str(paths, "data_dir", "~/.yucode"), mcp=cls.table(data, "mcp"))

    @staticmethod
    def table(data: Json, key: str) -> Json:
        return value if isinstance((value := data.get(key)), dict) else {}  # 缺失或类型不符一律回退空表,调用方无需判空

    @staticmethod
    def table_tuple(data: Json, key: str) -> tuple[Json, ...]:
        """按原样透传的表格列表,只校验所有宿主共有的形状。

        条目会不加修改地到达线路,校验内容意味着要跟踪每个宿主的工具目录。`type` 是所有
        有文档的 builtin 工具都携带的字段,要求它存在可以把拼写错误变成配置错误,
        而不是 provider 返回 400。"""
        value = data.get(key)
        if value is None:
            return ()  # 未配置:空元组,上游按"无内置工具"处理
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"config value `{key}` must be a list of tables")  # 形状错误立即报错,而不是悄悄忽略
        entries: list[Json] = []
        for item in value:
            if not isinstance(item, dict):
                raise ConfigError(f"config value `{key}` must be a list of tables")
            if not (isinstance(item.get("type"), str) and item["type"]):
                raise ConfigError(f"config value `{key}` entries must each set a non-empty `type`")  # type 是唯一能跨宿主校验的字段
            entries.append(dict(item))  # 拷贝一份,防止调用方后续改动污染配置对象
        return tuple(entries)

    @staticmethod
    def str(data: Json, key: str, default: str = "") -> str:
        return default if (value := data.get(key)) is None else str(value)  # 缺失回退默认值;数字/布尔强转字符串

    @staticmethod
    def str_tuple(data: Json, key: str) -> tuple[str, ...]:
        value = data.get(key)
        if value is None:
            return ()
        if isinstance(value, str):
            # 兼容逗号分隔的字符串写法 "a,b";空项剔除
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return tuple(value)  # 纯字符串列表原样接受
        raise ConfigError(f"config value `{key}` must be a string list")  # 混入数字等其他类型视为配置错误

    @staticmethod
    def bool(data: Json, key: str, default: bool = False) -> bool:
        value = data.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value  # 真布尔直接返回,避免 0/1 走字符串分支产生歧义
        lower = value.lower() if isinstance(value, str) else ""  # 字符串统一小写再匹配,大小写均可
        if lower in {"on", "true", "yes", "1", "off", "false", "no", "0"}:
            return lower in {"on", "true", "yes", "1"}  # TOML 常用布尔拼写全兼容
        raise ConfigError(f"config value `{key}` must be boolean")  # 其余取值(数字等)一律拒绝

    @staticmethod
    def int(data: Json, key: str, default: int) -> int:
        value = data.get(key)
        if value is None:
            return default
        # bool 是 int 的子类,必须显式排除;float 也拒绝(1.0 不应静默变 1)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"config value `{key}` must be integer")
        return value

    @staticmethod
    def float(data: Json, key: str, default: float | None) -> float | None:
        value = data.get(key)
        if value is None:
            return default
        # false / "off" 显式表示"不设置"(如关闭 temperature),返回 None
        if value is False or (isinstance(value, str) and value.lower() == "off"):
            return None
        # 此处 bool 只可能是 True(False 已在上方返回);int/float 之外一律拒绝
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"config value `{key}` must be number or off")
        return float(value)  # int 也归一为 float,调用方拿到统一类型


class ConfigFile:
    DEFAULT_PATH: ClassVar[str] = os.path.join(os.path.expanduser("~"), ".yucode", "config.toml")
    LEGACY_PATH: ClassVar[str] = os.path.join(os.path.expanduser("~"), ".minacode", "config.toml")
    # 只有 provider 块是必需的;其余键都回退到内置默认值,因此下面被注释掉的行
    # 只是文档性质,用于说明常用旋钮及其默认值。
    DEFAULT_TEXT: ClassVar[str] = """# yucode configuration — unset keys use built-in defaults.

[provider]
active = "default"

[provider.default]
url = ""
key = ""
model = ""
# api = "auto"                 # auto | chat | responses | anthropic
# stream = true
# image_input = "auto"         # auto | on | off
# reasoning = "medium"
# max_tokens = 0               # output cap per request, reasoning included; 0 leaves it to the provider
                               # (Anthropic sends a conservative 8K). 16K is still reserved from the
                               # input budget, trading against runtime.max_context_tokens one for one
# timeout = 120                # transport inactivity
# response_timeout = 600       # total generation time; 0 disables
# available_models = ["gpt-5", "gpt-5-mini"]

# builtin_tools = [{ type = "web_search" }]   # provider-side tools, passed through verbatim
                                              # OpenAI/Qwen: { type = "web_search" }
                                              # Anthropic:   { type = "web_search_20250305", name = "web_search" }
                                              # Z.AI:        { type = "web_search", web_search = { enable = "True" } }

# [runtime]                    # optional overrides (defaults shown)
# yolo = false
# quick_hints = true           # model-suggested next-step chips; toggle with /hints
# max_context_tokens = 262144      # 256K; how much of the model's window to use, not its size.
                               # Raise it for a 1M-window model; lower it for a smaller one.
# max_agent_steps = 200
# shell_timeout = 60

# [mcp.example]                # url (+ auth = "oauth") for remote, or command/args for stdio
# url = "https://example.com/mcp"
# auto_connect = false
"""

    @classmethod
    def resolve_path(cls, path: str | None) -> str:
        if path:
            return os.path.expanduser(path)  # 显式传入的路径优先
        # 向后兼容:新的 ~/.yucode/config.toml 尚不存在而旧版 ~/.minacode/config.toml 存在时,
        # 读旧文件,老用户升级后无需手动迁移配置。
        if not os.path.exists(cls.DEFAULT_PATH) and os.path.exists(cls.LEGACY_PATH):
            return cls.LEGACY_PATH
        return cls.DEFAULT_PATH

    @classmethod
    def init(cls, path: str | None = None) -> tuple[str, bool]:
        config_path = cls.resolve_path(path)
        if os.path.exists(config_path):
            return config_path, False  # 已存在则跳过写入,created=False
        os.makedirs(os.path.dirname(config_path), exist_ok=True)  # 自定义路径的父目录可能不存在,先建目录
        with open(config_path, "w", encoding="utf-8") as file:
            file.write(cls.DEFAULT_TEXT)
        return config_path, True  # created=True:调用方据此提示用户"已生成示例配置"

    @classmethod
    def load(cls, path: str | None = None) -> Json:
        config_path = cls.resolve_path(path)
        try:
            with open(config_path, "rb") as file:  # tomllib 只接受二进制模式
                data = tomllib.load(file)
        except FileNotFoundError as error:
            # 缺失时给出下一步指引(--init-config),而不是裸 traceback
            raise ConfigError(f"config not found: {config_path}; run --init-config") from error
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"invalid config {config_path}: {error}") from error  # 语法错误保留原始信息并归类为 ConfigError
        return data if isinstance(data, dict) else {}  # 空文件/非法根结构按空配置处理


@dataclass
class ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_prompt_tokens: int = 0
    last_prompt_tokens: int = 0
    last_prompt_budget: int = 0
    last_cached_prompt_tokens: int = 0
    last_cache_write_prompt_tokens: int = 0

    @staticmethod
    def field(usage: Any, *paths: str) -> int:
        """返回 `usage` 中第一个命中的点分路径(dict 键或属性)对应的 int;都没有则为 0。"""
        for path in paths:  # 候选路径按顺序尝试,取第一个命中的
            raw = usage
            for key in path.split("."):
                # 兼容 dict 与对象两种 usage 形态(OpenAI 给 dict,部分 SDK 给对象)
                raw = raw.get(key) if isinstance(raw, dict) else getattr(raw, key, None)
                if raw is None:
                    break  # 中途断掉(缺字段)就换下一条候选路径
            else:  # 点分路径全部命中才算有效
                return int(raw or 0)  # None/0 统一归一为 0
        return 0

    def add(self, usage: Any, budget: int | None = None) -> None:
        self.calls += 1  # 每次模型请求计一次调用
        # OpenAI 用 prompt_tokens,Anthropic 用 input_tokens,按候选顺序取第一个
        prompt_tokens = self.field(usage, "prompt_tokens", "input_tokens")
        completion_tokens = self.field(usage, "completion_tokens", "output_tokens")
        # fmt: off
        cached_tokens = self.field(usage, "prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens", "prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens")
        cache_write_tokens = self.field(
            usage,
            "cache_creation_input_tokens",
            "prompt_tokens_details.cache_write_tokens",
            "input_tokens_details.cache_write_tokens",
        )
        # fmt: on
        # OpenAI 形态的 usage 把缓存命中计入 `prompt_tokens` 之内,而 Anthropic 的
        # `input_tokens` 只统计既未读缓存也未写缓存的部分。把缓存两腿加回来,让 prompt
        # 总量在每家 provider 下含义一致;否则缓存命中率极高的 Anthropic 请求会报出
        # 远超 100% 的命中率和一个偏小的 token 总数。
        if not self.field(usage, "prompt_tokens"):  # 无 prompt_tokens 字段即 Anthropic 形态:回补缓存读写
            prompt_tokens += self.field(usage, "cache_read_input_tokens") + self.field(usage, "cache_creation_input_tokens")
        total_tokens = self.field(usage, "total_tokens") or prompt_tokens + completion_tokens  # 部分 provider 不报 total,回退为两段之和
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.cached_prompt_tokens += cached_tokens
        self.cache_write_prompt_tokens += cache_write_tokens
        self.last_prompt_tokens = prompt_tokens
        if budget is not None:
            self.last_prompt_budget = budget  # 未传预算时保留上一次的值,不清零
        self.last_cached_prompt_tokens = cached_tokens
        self.last_cache_write_prompt_tokens = cache_write_tokens


@dataclass
class UpdateStatus:
    # 版本号解析:主版本必填,次/补丁版本可选,如 "2"、"2.1"、"2.1.3"
    _VERSION_RE: ClassVar[re.Pattern] = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?")
    latest: str = ""
    checking: bool = False
    error: str = ""

    def newer_than(self, current: str) -> bool:
        current_version = self.version_tuple(current)
        latest_version = self.version_tuple(self.latest)
        # 任一版本解析失败(空元组)时按"无更新"处理,比较不会崩溃
        return bool(current_version and latest_version and latest_version > current_version)

    @staticmethod
    def version_tuple(value: str) -> tuple[int, ...]:
        match = UpdateStatus._VERSION_RE.match(value)
        # 缺省段补 0("2" 视为 2.0.0);解析失败返回空元组作哨兵
        return tuple(int(part or 0) for part in match.groups()) if match else ()


@dataclass
class SystemInfo:
    # 系统概览要探测的命令清单;只保留 PATH 里实际存在的(见 detect)
    # fmt: off
    COMMANDS: ClassVar[tuple[str, ...]] = (
        "bash", "git", "rg", "sed", "grep", "find", "awk", "python3", "jq", "xargs", "cat", "head", "tail", "wc",
        "sort", "uniq", "make", "cmake", "gcc", "g++", "clang", "clang++", "node", "npm", "uv", "pytest",
    )
    # fmt: on

    cwd: str
    os: str
    arch: str
    commands: tuple[str, ...]

    @classmethod
    def detect(cls, cwd: str) -> SystemInfo:
        return cls(
            cwd=cwd,
            os=platform.system() or sys.platform,  # platform.system() 部分平台返回空串,回退 sys.platform
            arch=platform.machine() or "unknown",  # 架构名可能为空,兜底 unknown
            commands=tuple(name for name in cls.COMMANDS if shutil.which(name)),  # 只收集可执行命令,找不到的跳过
        )


@dataclass
class ToolCall:
    id: str
    name: str
    args: ToolArgs
    # 解析调用时捕获的参数格式错误。延迟呈现:它作为工具结果返回,模型可以据此修正,
    # 而不是在解析阶段就中断整个轮次。
    error: str = ""


class LogEdge(Enum):
    """日志树边线:BRANCH 表示有下级,CONTINUE 延续同层,END 收尾;NONE 无前缀。"""

    NONE = ""
    BRANCH = "├"
    CONTINUE = "│"
    END = "└"


class LogRole(Enum):
    """日志行角色,决定配色与语义:工具调用/自动步骤/元信息/输出/错误/静默/差异。"""

    TOOL = auto()
    AUTO = auto()
    META = auto()
    OUTPUT = auto()
    ERROR = auto()
    MUTED = auto()
    DIFF = auto()


@dataclass(frozen=True)
class LogLine:
    label: str
    text: str = ""
    role: LogRole = LogRole.OUTPUT
    edge: LogEdge = LogEdge.NONE
    meta: str = ""
    syntax: str = ""

    def text_prefix(self) -> str:
        edge = "" if self.edge is LogEdge.NONE else self.edge.value + " "  # 树线字符后跟一个空格
        separator = "  " if self.edge is LogEdge.NONE else " "  # 有树线时只需单空格;无树线用双空格对齐
        return edge + self.label + (separator if self.label and self.text else "")  # label 或 text 为空时不加分隔,避免尾随空格


@dataclass
class LogBlock:
    INDENT: ClassVar[str] = "  "
    items: list[LogLine | LogBlock]

    @classmethod
    def hierarchy(cls, root: LogLine | None, children: list[LogLine]) -> LogBlock:
        items: list[LogLine | LogBlock] = [root] if root else []  # 根节点可选:没有根就从子列表开始
        if children:  # 非空子列表折叠成唯一子块,形成两级结构
            items.append(cls(list(children)))
        return cls(items)

    @property
    def has_children(self) -> bool:
        return any(isinstance(item, LogBlock) for item in self.items)

    @classmethod
    def margin(cls, level: int) -> str:
        return cls.INDENT * level

    @classmethod
    def prefix(cls, level: int, edge: LogEdge = LogEdge.NONE) -> str:
        return cls.margin(level) + ((edge.value + " ") if edge is not LogEdge.NONE else "")

    def walk(self, parent_level: int = 0):
        level = parent_level + 1  # 层级从父级 +1 开始,嵌套块递归时自然递增
        for item in self.items:
            if isinstance(item, LogLine):
                yield item, level
            else:
                yield from item.walk(level)

    def __str__(self) -> str:
        rows = []
        for line, level in self.walk():
            prefix = self.margin(level) + line.text_prefix()  # 首行前缀 = 缩进 + 边线 + 标签
            continuation = self.margin(level) + " " * get_cwidth(line.text_prefix())  # 续行按文本前缀实际宽度对齐,折行不歪
            rows.extend(Text.wrap_styled([("", prefix)], [("", continuation)], [("", line.text + line.meta)]))
        return "\n".join("".join(text for _style, text in row) for row in rows)


@dataclass
class TurnBox:
    ROOT_LEVEL: ClassVar[int] = 0
    CONTENT_LEVEL: ClassVar[int] = 1
    SEPARATOR: ClassVar[str] = ""
    messages: list[Json]

    @classmethod
    def group(cls, messages: list[Json]) -> list[TurnBox]:
        boxes: list[TurnBox] = []
        current: list[Json] = []
        for message in messages:
            current.append(message)
            # 一个轮次在"不带 tool_calls 的 assistant 消息"处收尾:后续 user 消息开启下一轮
            if message.get("role") == "assistant" and not message.get("tool_calls"):
                boxes.append(cls(current))
                current = []
        if current:  # 末尾未收尾的消息(如最后是 user 提问)单独成盒
            boxes.append(cls(current))
        return boxes


class ActiveResource(Generic[_ResourceT]):
    """线程安全的资源生命周期管理:另一个线程可能需要在运行中取消该资源。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: _ResourceT | None = None

    @contextlib.contextmanager
    def track(self, value: _ResourceT) -> Iterator[None]:
        with self.lock:
            self.value = value
        try:
            yield
        finally:
            with self.lock:
                # 身份比较:中途可能已换成别的资源,只有仍是同一对象才清空,避免覆盖新值
                if self.value is value:
                    self.value = None

    def apply(self, action: Callable[[_ResourceT], None]) -> None:
        with self.lock:  # 快照读取,锁外执行 action:持有锁调用用户代码可能死锁/阻塞
            value = self.value
        if value is not None:  # 没有活动资源时静默跳过
            action(value)
