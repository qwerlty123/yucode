"""yucode model client:provider 请求协议、流式输出与重试策略。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from json_repair import repair_json

from yucode.base import (
    ANTHROPIC_CONTENT_KEY,
    HTTP_USER_AGENT,
    MODEL_REQUEST_RETRIES,
    PAUSED_TURN_KEY,
    PROVIDER_ECHO_KEYS,
    RESPONSES_OUTPUT_KEY,
    SEARCH_SOURCES_KEY,
    SESSION_EVENT_KEY,
    ActiveResource,
    Json,
    ModelError,
    ModelOutputTruncated,
    ModelRequestRetry,
    ModelResponseTimeout,
    ModelUsage,
    ProviderConfig,
    Text,
    ToolArgs,
    ToolCall,
    ToolError,
    builtin_tool_label,
    request_budget_for,
)
from yucode.image import IMAGE_REFS_KEY, TOOL_IMAGE_OBSERVATION_KEY, ImageInputs
from yucode.model_catalog import THINKING_BUDGETS
from yucode.prompts import (
    COMPACTION_PROMPT,
    MEMORY_CONSOLIDATION_PROMPT,
)
from yucode.provider_compat import (
    ResolvedProvider,
    anthropic_keeps_prior_thinking,
    anthropic_thinking_always_on,
    anthropic_thinking_params,
    builtin_tools_issue,
)

if TYPE_CHECKING:
    # provider SDK 导入耗时约 0.8s,而首次请求之前并不需要它们;
    # 运行时导入(见下方)让它们不进入启动路径(MCPManager 采用了同样的模式)。
    from anthropic import Anthropic
    from openai import OpenAI

from yucode.session import QueuedInput, Session
from yucode.tools import (
    TOOL_REGISTRY,
    Tool,
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class PreparedRequest:
    messages: list[Json]
    tools: list[Json]
    pending: list[QueuedInput]


class ModelClient:
    """通过所选 provider 协议发送一次请求,并把响应统一成标准形态。

    Chat Completions、Responses 和 Anthropic Messages 都返回同样的三元组
    (assistant 消息, 工具调用, 文本),调用方无需知道具体走了哪条协议。历史保持一种归一化的
    model 形态;reasoning 等延续数据通过带命名空间的透传字段往返,因为 provider 会校验自己
    产出的内容必须原样返回——把它拍平成文本会破坏下一次请求。

    重试对调用方不可见:传输层和 5xx 失败采用有界退避,进度通过 session 状态发布给状态栏。
    模型缺失或模态被拒属于决策而非故障,会立即上报。流式只是同一个调用的形态,不是第二条路径。

    取消会关闭在途客户端,因此被阻塞的读取会立刻结束,而不是一直等到超时。
    """

    # 从错误文本中识别可重试状态码的宽松模式:兼容各家 SDK 对 "status code" 措辞的差异。
    # 与 _STATUS_CODE_RE 的区别:此处只匹配可重试的码值(408/409/425/429/5xx)。
    _RETRYABLE_STATUS_RE: ClassVar[re.Pattern] = re.compile(r"(?:error|status)?[_\s-]*code['\"]?\s*[:=]\s*['\"]?(408|409|425|429|5\d\d)\b")
    # 匹配任意 4xx/5xx 状态码,用于给状态栏展示简洁的重试原因。
    _STATUS_CODE_RE: ClassVar[re.Pattern] = re.compile(r"(?:error|status)?[_\s-]*code['\"]?\s*[:=]\s*['\"]?(4\d\d|5\d\d)\b")
    # 匹配 ```json 围栏(忽略大小写、支持跨行),压缩器输出常带这种围栏。
    _JSON_FENCE_RE: ClassVar[re.Pattern] = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)

    def __init__(self, session: Session):
        self.session = session
        self.cancel_requested = threading.Event()  # 跨线程取消信号:cancel() 置位后,请求线程从中断中醒来
        self.active_client: ActiveResource[OpenAI | Anthropic] = ActiveResource()  # 跟踪在途 SDK 客户端,便于 cancel() 统一关闭
        self.on_stream: Callable[[str, str], None] | None = None  # 流式回调:(事件类型, 增量文本);None 表示没有消费者
        # 对响应中报告的每个 provider 侧工具调用,以 (label, detail) 回调。
        # 从解析结果而不是流中上报:这样关闭流式时、以及在不显示实时状态的界面上,搜索日志的表现一致。
        self.on_builtin_call: Callable[[str, str], None] | None = None

    def cancel(self) -> None:
        self.cancel_requested.set()  # 先置位信号,让请求线程从任何阻塞读取中立即醒来
        with contextlib.suppress(Exception):  # close() 可能抛错(如阻塞在 I/O),但取消路径必须无条件继续
            self.active_client.apply(lambda client: client.close())

    def chat_messages(self, messages: list[Json]) -> list[Json]:
        """按 provider 文档规定的回放契约构建 Chat Completions 历史。"""

        provider = self.session.config.provider
        resolved = provider.resolve()
        history = resolved.chat_reasoning_history  # 该 provider 在 Chat 协议上默认如何保留 reasoning
        thinking = provider.extra_body.get("thinking")
        # 用户显式要求保留 thinking(extra_body 里的 preserve_thinking,
        # 或 thinking.keep="all" / clear_thinking=False)时,无论默认策略如何都全部保留。
        if provider.extra_body.get("preserve_thinking") is True or (
            isinstance(thinking, dict) and (thinking.get("keep") == "all" or thinking.get("clear_thinking") is False)
        ):
            history = "all"

        converted: list[Json] = []
        # 最近一条真实用户消息的下标;工具观察消息不算用户消息。
        # "current_turn" 策略据此只保留本轮对话的 reasoning。
        latest_user = max(
            (index for index, message in enumerate(messages) if message.get("role") == "user" and not ImageInputs.is_tool_observation(message)),
            default=-1,
        )
        for index, message in enumerate(messages):
            # 剔除 yucode 内部字段(provider 回显、镜像引用、工具观察、会话事件)——它们不是协议字段。
            clean = {
                key: value for key, value in message.items() if key not in (*PROVIDER_ECHO_KEYS, IMAGE_REFS_KEY, TOOL_IMAGE_OBSERVATION_KEY, SESSION_EVENT_KEY)
            }
            # 只有三种情况保留 reasoning:策略为 all;消息带工具调用且策略为 tool_calls;
            # 或策略为 current_turn 且消息在本轮(位于最近用户消息之后)。
            keep_reasoning = history == "all" or (
                bool(message.get("tool_calls")) and (history == "tool_calls" or (history == "current_turn" and index > latest_user))
            )
            if message.get("role") == "assistant" and not keep_reasoning:
                # 不保留时删掉 reasoning 字段:provider 只回放它自己产出的内容,这些字段会破坏校验。
                for key in ("reasoning_content", "reasoning", "reasoning_details"):
                    clean.pop(key, None)
            if message.get("role") == "user" and self.session.images.refs(message):
                clean["content"] = self.session.images.chat_content(message)  # 本地镜像引用 → 协议要求的 content 数组(base64)
            converted.append(clean)
        return Text.value(converted)

    def estimated_request_tokens(self, messages: list[Json], tools: list[Json] | None = None) -> int:
        """估算实际协议载荷的 token 数,而不是 yucode 归一化历史的大小。"""

        resolved = self.session.config.provider.resolve()
        api = resolved.api
        # 测量载荷绝不能因载荷本身而失败:某条 wire 拒绝的字段应由真正发请求的路径报错,
        # 而不是让状态栏、/status 或 resume 的估算因此崩溃。
        builtin = self.builtin_tools(resolved, strict=False)
        # 载荷构建器会把本地图片展开成 base64,而这里只关心字节数。用标签保留 wire 形状,
        # 图片 tile 的 token 在最后单独加一次。
        projected = [{key: value for key, value in message.items() if key != IMAGE_REFS_KEY} for message in messages]
        if api == "responses":
            payload: Json = {"input": self.responses_input(Text.value(projected))}  # 构造与真实请求完全一致的载荷
            if request_tools := [*self.responses_tool_schemas(tools or []), *builtin]:
                payload["tools"] = request_tools  # 与真实请求一致:只有工具非空才带 tools 字段
        elif api == "anthropic":
            # Anthropic 的 system 是独立字段:把历史中的 system 消息合并成一段。
            system = "\n\n".join(str(message.get("content") or "") for message in projected if message.get("role") == "system").strip()
            estimated_messages = projected
            if not anthropic_keeps_prior_thinking(self.session.config.provider.model):
                # 该模型不能保留先前的 thinking:估算时必须剥离历史里的 thinking 块,否则 token 数虚高。
                # 这只影响估算;真实请求的保留策略由 anthropic_messages 另作处理。
                latest_user = max(
                    (index for index, message in enumerate(projected) if message.get("role") == "user" and not ImageInputs.is_tool_observation(message)),
                    default=-1,
                )
                active_assistants = [index for index, message in enumerate(projected) if index > latest_user and message.get("role") == "assistant"]
                keep_from = (
                    latest_user
                    if active_assistants  # 本轮有 assistant 消息:本轮之后的 thinking 仍会真实发送,估算保留
                    else max((index for index, message in enumerate(projected) if message.get("role") == "assistant"), default=len(projected))
                )
                estimated_messages = []
                for index, message in enumerate(projected):
                    estimated = dict(message)
                    saved = estimated.get(ANTHROPIC_CONTENT_KEY)
                    if index < keep_from and isinstance(saved, list):
                        # 只对保留区之前的消息剥离 thinking/redacted_thinking 块。
                        estimated[ANTHROPIC_CONTENT_KEY] = [
                            block for block in saved if not isinstance(block, dict) or block.get("type") not in ("thinking", "redacted_thinking")
                        ]
                    estimated_messages.append(estimated)
            payload = {"system": system, "messages": self.anthropic_messages(Text.value(estimated_messages))}
            if request_tools := [*self.anthropic_tool_schemas(tools or []), *builtin]:
                payload["tools"] = request_tools
        else:
            payload = {"messages": self.chat_messages(projected)}
            if request_tools := [*(tools or []), *builtin]:
                payload["tools"] = request_tools

        # 递归清洗载荷,剔除只会让估算失真的传输状态字段。
        def prompt_value(value: object) -> object:
            if isinstance(value, list):
                return [prompt_value(item) for item in value]
            if not isinstance(value, dict):
                return value
            kind = value.get("type")
            clean: Json = {}
            for key, item in value.items():
                if key in ("encrypted_content", "signature"):
                    continue  # 密文与签名是传输状态,其字节长度不代表 prompt token
                if key == "data" and kind in ("reasoning.encrypted", "redacted_thinking"):
                    continue  # 加密 reasoning 的 data 同样剔除
                if (key == "data" and kind == "base64") or (key in ("image_url", "url") and isinstance(item, str) and item.startswith("data:")):
                    clean[key] = ""  # base64 图片与 data: URL 置空,只保留结构;图片 token 另行估算
                else:
                    clean[key] = prompt_value(item)
            return clean

        chars = len(json.dumps(prompt_value(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))  # 按 UTF-8 字节数计量
        images = ImageInputs.estimated_tokens(messages) if self.session.images.support() is not False else 0  # 图片 token 单独估算;不支持图片的模型按 0
        return (chars + 3) // 4 + images  # 1 token ≈ 4 字节,向上取整

    def call_client(self, client: OpenAI | Anthropic, request: Callable[[], _ResultT]) -> _ResultT:
        response_timeout = self.session.config.provider.response_timeout
        expired = threading.Event()  # 总时长超时信号:response_timeout 覆盖整个生成过程(含流式)
        timer: threading.Timer | None = None
        if response_timeout:

            def expire() -> None:
                expired.set()
                with contextlib.suppress(Exception):  # 关闭失败不阻碍超时路径
                    client.close()  # 关闭客户端让被阻塞的读取立刻返回错误

            timer = threading.Timer(response_timeout, expire)
            timer.daemon = True  # 守护线程:不阻止进程退出
        with self.active_client.track(client):  # 注册在途客户端,cancel() 可以关闭它
            if timer is not None:
                timer.start()
            try:
                result = request()
                if self.cancel_requested.is_set():
                    raise KeyboardInterrupt  # 请求成功但已被取消:同样视为中断
                if expired.is_set():
                    # 成功返回但总时长已超:超时优先于成功结果。
                    raise ModelResponseTimeout(
                        f"Model response exceeded provider.response_timeout={response_timeout}s; set it to 0 to disable the total-generation limit"
                    )
                return result
            except ModelResponseTimeout:
                raise  # 超时原样上抛,上层据此决定是否重试
            except Exception as error:
                if self.cancel_requested.is_set():
                    raise KeyboardInterrupt from None  # 取消优先于任何错误;不留原因链
                if expired.is_set():
                    raise ModelResponseTimeout(
                        f"Model response exceeded provider.response_timeout={response_timeout}s; set it to 0 to disable the total-generation limit"
                    ) from error
                raise ModelError(str(error)) from error  # 其余 SDK 异常统一包装成 ModelError
            finally:
                if timer is not None:
                    timer.cancel()  # 定时器只触发一次,用完即取消
                with contextlib.suppress(Exception):
                    client.close()  # 无论成败都关闭客户端,防止连接泄漏

    def request(self, messages: list[Json], tools: list[Json] | None = None) -> tuple[Json, list[ToolCall], str]:
        if missing := self.session.missing_config():
            raise ModelError("missing config: " + ", ".join(missing))  # 缺配置是决策错误,直接失败,不值得重试
        self.cancel_requested.clear()  # 复用 ModelClient 前清除上一次请求的取消信号
        tools = tools if tools is not None else Tool.resolved_schemas(self.session)
        state = self.session.state
        state.model_retry_reason = ""  # 只在真正重试时写入,状态栏据此显示
        try:
            attempt = 0
            while True:  # 重试循环:成功返回或达到重试上限才退出
                state.current_model_attempt = attempt + 1
                state.current_model_call_started_at = time.monotonic()  # 单调时钟:状态栏耗时不受系统时间调整影响
                try:
                    result = self.api_request(messages, tools)
                    self.session.images.note_success(messages)  # 记住本次哪些图片被模型接受
                    return result
                except KeyboardInterrupt:
                    if state.manual_model_retry_requested:
                        state.manual_model_retry_requested = False
                        raise ModelRequestRetry() from None  # 用户要求重试:转成一次新请求
                    raise
                except ModelError as error:
                    if self.session.images.note_error(messages, error):
                        provider = self.session.config.provider
                        identity = f"{self.session.config.active_provider}/{provider.model or '(no model)'}"
                        raise ModelError(
                            f"{identity} does not support image input. Switch to an image-capable model, or continue with image labels only."
                        ) from error  # 模型不支持图片:给出明确指引,重试无意义
                    retryable = self.retryable_error(error)
                    if attempt >= MODEL_REQUEST_RETRIES or not retryable:
                        if attempt:
                            raise ModelError(f"{error} (after {attempt + 1} attempts)") from error  # 重试过仍失败:附带尝试次数
                        raise
                    state.current_model_attempt = attempt + 2  # 预置下一次尝试的序号,状态栏显示"重试中"
                    state.model_retry_reason = self.retry_reason(error)
                    state.model_retry_count += 1
                    time.sleep(0.5 * (attempt + 1))  # 线性退避:0.5s、1s、1.5s…
                finally:
                    state.current_model_call_started_at = 0.0  # 本次调用结束(含异常路径)后清空
                attempt += 1
        finally:
            state.current_model_attempt = 0  # 所有路径结束后复位,状态栏不再显示重试信息
            state.model_retry_reason = ""

    @staticmethod
    def retryable_error(error: Exception) -> bool:
        # 延迟导入:把约 0.8s 的 provider SDK 导入挡在启动路径之外(参见上方 TYPE_CHECKING 块)
        import anthropic
        import openai

        # 截断是确定性的:同样的请求会再次撞上同样的输出上限,重试无意义。
        if isinstance(error, (ModelResponseTimeout, ModelOutputTruncated)):
            return False
        cause = getattr(error, "__cause__", None)  # 我们的包装统一用 `from` 保留原因链,从 cause 取底层异常

        # SDK 状态错误直接暴露 status_code。
        if isinstance(cause, (openai.APIStatusError, anthropic.APIStatusError)):
            return cause.status_code in {408, 409, 425, 429} or 500 <= cause.status_code < 600  # 429/5xx 可重试;408/409/425 也视为临时故障

        # SDK 连接/超时错误总是可重试的。
        if isinstance(
            cause,
            (openai.APIConnectionError, openai.APITimeoutError, anthropic.APIConnectionError, anthropic.APITimeoutError),
        ):
            return True

        # 内置网络/超时错误可重试。
        if isinstance(cause, (TimeoutError, asyncio.TimeoutError, ConnectionError, ConnectionResetError, ConnectionAbortedError)):
            return True

        # 兜底:从错误文本或 cause 属性里解析状态码。
        status: Any = getattr(cause, "status_code", None) or getattr(cause, "code", None)
        with contextlib.suppress(Exception):
            if int(status) in {408, 409, 425, 429, 500, 502, 503, 504}:
                return True
        text = str(error).lower()
        if ModelClient._RETRYABLE_STATUS_RE.search(text):
            return True
        return any(  # 文本特征兜底:无法解析状态码时按关键词判断
            part in text for part in ("internal server error", "timeout", "timed out", "connection reset", "connection aborted", "temporarily unavailable")
        )

    @staticmethod
    def retry_reason(error: Exception) -> str:
        cause = getattr(error, "__cause__", None)
        status: Any = getattr(cause, "status_code", None) or getattr(cause, "code", None)  # 优先用状态码作为展示原因
        with contextlib.suppress(Exception):
            status_code = int(status)
            if 400 <= status_code <= 599:  # 只接受合法 HTTP 状态码范围
                return str(status_code)
        text = str(error).lower()
        match = ModelClient._STATUS_CODE_RE.search(text)
        if match:
            return match.group(1)  # 错误文本里嵌的状态码
        if "timeout" in text or "timed out" in text:
            return "timeout"
        if any(part in text for part in ("connection", "reset", "aborted")):
            return "connection"
        if "internal server error" in text or "temporarily unavailable" in text:
            return "server error"
        return "transient error"  # 其余一律归为瞬时错误

    def truncated_output_error(self, usage: Any) -> ModelOutputTruncated:
        """报告一次生成被输出上限截断、且没有产出任何内容的失败。

        Reasoning 在 Responses 和 Anthropic 两条 wire 上也计入该上限,因此高 effort 可能耗尽
        整个预算,结果既没有文本也没有工具调用。整个回合以"无内容"失败,若不点名是上限
        导致的,它与模型给了空回答无法区分。仍然带有文本的截断则不加处理:
        部分答案可见,截断本身就是证据。
        """
        provider = self.session.config.provider
        # 未配置 max_tokens 时用 provider 默认上限来措辞。
        cap = f"provider.max_tokens={provider.max_tokens}" if provider.max_tokens > 0 else "the provider's own default output limit"
        completion = ModelUsage.field(usage, "completion_tokens", "output_tokens")
        reasoning = ModelUsage.field(usage, "completion_tokens_details.reasoning_tokens", "output_tokens_details.reasoning_tokens")
        spent = f" after {completion} output tokens" if completion else ""  # 同一字段在不同协议下名字不同,取非空的那个
        spent += f" ({reasoning} of them reasoning)" if reasoning else ""
        return ModelOutputTruncated(f"Model output was truncated at {cap}{spent}. Raise provider.max_tokens, or lower provider.reasoning.")

    def empty_length_error(self, usage: Any) -> ModelError:
        """Chat wire 上 `finish_reason=length` 且没有任何产出时存在歧义:可能是输出撞上了上限,
        也可能是输入超出了模型的 context window(部分 OpenAI 兼容 provider 把后者也报成 `length`)。
        只有"撞上限"这一种情况能从 usage 验证;其余情况把两个设置都点名,
        而不是盲目地把 max_tokens 调大。
        """
        provider = self.session.config.provider
        completion = ModelUsage.field(usage, "completion_tokens", "output_tokens")
        if provider.max_tokens > 0 and completion >= provider.max_tokens:
            return self.truncated_output_error(usage)  # 输出 token 数达到配置上限:可确证是截断
        cap = f"provider.max_tokens={provider.max_tokens}" if provider.max_tokens > 0 else "the provider's default output cap"
        spent = f" after {completion} output tokens" if completion else ""
        return ModelError(
            f"Generation stopped empty with `finish_reason=length`{spent}: either the output hit {cap} or "
            f"the input exceeded the model's context window. Check provider.max_tokens and runtime.max_context_tokens."
        )

    def _record_usage(self, usage: Any) -> None:
        """把一次完成的请求记入 session usage,并保留准备该请求时的预算,
        这样状态栏的填充比例使用请求当时的 denominator,而不是今天的配置。
        """
        # 预算取请求准备时的配置计算并随 usage 一起记录,状态栏填充率因此与请求当时的上下文一致。
        self.session.usage.add(
            usage,
            request_budget_for(self.session.settings.max_context_tokens, self.session.config.provider.output_token_budget()),
        )

    def chat_request(
        self,
        messages: list[Json],
        tools: list[Json] | None = None,
        *,
        allow_stream: bool = True,
        include_builtin_tools: bool = True,
    ) -> tuple[Json, list[ToolCall], str]:
        messages = self.chat_messages(messages)
        provider = self.session.config.provider
        resolved = provider.resolve()
        stream = allow_stream and provider.stream and self.on_stream is not None  # 三条件齐备才流式:允许、配置开启、且有消费者
        params: Json = {"model": provider.model, "messages": messages, "stream": stream}
        if provider.max_tokens > 0:  # 0 表示不设上限:不发送该字段
            params["max_tokens"] = provider.max_tokens
        builtin = self.builtin_tools(resolved) if include_builtin_tools else []
        if request_tools := [*(tools or []), *builtin]:  # 本地工具 + provider 内置工具合并;非空才发 tools
            params["tools"] = request_tools
            params["tool_choice"] = "auto"  # 显式要求 auto,避免部分主机默认拒绝工具
            params["parallel_tool_calls"] = True
        prompt_cache_key = self.prompt_cache_key(provider, tools, include_builtin_tools=include_builtin_tools)
        if prompt_cache_key:
            params["prompt_cache_key"] = prompt_cache_key  # 兼容部分兼容主机自定义的 prompt cache 字段
        self.apply_provider_params(params, provider, resolved)  # 应用 reasoning/thinking 等协议兼容参数
        if stream:
            params["stream_options"] = {"include_usage": True}  # 流式要求返回 usage,否则最后无法记账
        client = self.client()
        if stream:
            message, usage, finish_reason = self.call_client(client, lambda: self._chat_stream(client, params))
        else:
            response = self.call_client(client, lambda: client.chat.completions.create(**params))
            usage = getattr(response, "usage", None)
            message = response.choices[0].message  # 非流式只有一个(也是唯一)choice
            finish_reason = str(self.message_field(response.choices[0], "finish_reason") or "")
        self._record_usage(usage)
        assistant = self.assistant_message(message)
        calls = self.tool_calls(message)
        content = str(self.message_field(message, "content") or "")
        # 截断判断放在 call_client 之外:call_client 会把所有异常拍平成普通 ModelError,
        # 而截断需要精确类型让上层选择"不重试"。
        if finish_reason == "length" and not calls and not content.strip():
            raise self.empty_length_error(usage)  # 空输出 + length 终止:点名上限或上下文超限
        return assistant, calls, content

    def _chat_stream(self, client: OpenAI, params: Json) -> tuple[Json, Any, str]:
        """把流式 chat completion 重组成一条 assistant 消息及其 finish reason。

        工具调用是最难的部分。规范把工具调用按 `index` 分片流式下发,但各家 provider 要么省略
        index、要么重新计数、要么只给 `id`。`resolve_tool_call_index` 按可靠性从高到低,
        利用 chunk 携带的任何信息恢复对应关系;当没有任何信息能识别调用时宁可抛错也不猜——
        猜错会把两次调用的参数片段拼进同一个调用,产生损坏的 JSON,而模型无法修正它,
        因为那看起来像它自己写出来的内容。

        与 Responses 不同,Chat 没有独立的"文本完成"事件。不能在第一个工具 delta 出现时就
        认定文本已完:兼容 provider 的 delta 顺序可以不同。`finish_reason=tool_calls` 是
        第一个能证明该 assistant 消息已完整的协议边界。
        """
        content: list[str] = []  # 各字段按增量收集,结束时一次性拼接
        reasoning_content: list[str] = []
        reasoning: list[str] = []
        reasoning_details: list[Json] = []
        tool_calls: dict[int, Json] = {}  # index → 完整 tool_call
        tool_call_functions: dict[int, Json] = {}  # index → function 对象,便于原地追加参数
        tool_call_ids: dict[str, int] = {}  # call_id → index,反查用
        tool_call_positions: dict[int, int] = {}  # 增量在 chunk 内的位置 → index
        next_index = 0
        usage: Any = None
        output_promoted = False  # 防止重复上报"输出完成"
        finish_reason = ""

        def allocate_tool_call() -> int:
            nonlocal next_index
            while next_index in tool_calls:  # provider 可能省略或复用 index:跳过已占用
                next_index += 1
            index = next_index
            next_index += 1
            return index

        def resolve_tool_call_index(raw_index: object, call_id: str, position: int, chunk_size: int) -> int:
            nonlocal next_index
            if isinstance(raw_index, int):
                index = raw_index  # 最可靠:官方 index
            elif call_id and call_id in tool_call_ids:
                index = tool_call_ids[call_id]  # 其次:已知 id 反查
            elif call_id:
                index = allocate_tool_call()  # 有 id 但未见过的调用:分配新 index 并登记
            elif chunk_size == 1 and len(tool_calls) == 1:
                index = next(iter(tool_calls))  # 唯一增量 + 唯一调用:只能归属它
            elif position in tool_call_positions and chunk_size == len(tool_call_positions):
                index = tool_call_positions[position]  # 位置稳定且 chunk 数与已知调用数一致
            elif position not in tool_call_positions:
                index = allocate_tool_call()
            else:
                raise ModelError("Chat stream tool-call delta omitted both index and id; cannot associate it safely")  # 猜错会把两次调用的参数拼坏:宁可失败
            next_index = max(next_index, index + 1)  # 分配保持单调递增
            tool_call_positions[position] = index
            if call_id:
                tool_call_ids[call_id] = index
            return index

        try:
            for chunk in client.chat.completions.create(**params):
                if chunk_usage := self.message_field(chunk, "usage"):
                    usage = chunk_usage  # 流结束时最后一次出现的 usage 是最终值
                choices = self.message_field(chunk, "choices") or []
                if not choices:
                    continue  # 某些 chunk 只有 usage/role,没有 choices
                choice = choices[0]
                delta = self.message_field(choice, "delta")
                reasoning_content_delta = str(self.message_field(delta, "reasoning_content") or "")
                reasoning_delta = str(self.message_field(delta, "reasoning") or "")
                if reasoning_content_delta:
                    reasoning_content.append(reasoning_content_delta)
                    self._emit_stream("reasoning", reasoning_content_delta)
                elif reasoning_delta:
                    reasoning.append(reasoning_delta)  # 两种 reasoning 字段名(OpenAI 方言),按出现取其一
                    self._emit_stream("reasoning", reasoning_delta)
                raw_details = self.message_field(delta, "reasoning_details") or []
                details = [self.dump_message_item(item) for item in raw_details]
                reasoning_details.extend(item for item in details if item)  # 收集完整序列供回放
                if not reasoning_content_delta and not reasoning_delta:
                    for detail in details:
                        text = detail.get("text") if detail.get("type") == "reasoning.text" else detail.get("summary")
                        if text:
                            self._emit_stream("reasoning", str(text))  # 摘要/文本形式的 reasoning 也转发预览
                if content_delta := str(self.message_field(delta, "content") or ""):
                    content.append(content_delta)
                    self._emit_stream("output", content_delta)
                raw_tool_calls = self.message_field(delta, "tool_calls") or []
                for position, raw in enumerate(raw_tool_calls):
                    raw_index = self.message_field(raw, "index")
                    call_id = str(self.message_field(raw, "id") or "")
                    index = resolve_tool_call_index(raw_index, call_id, position, len(raw_tool_calls))
                    if index not in tool_calls:
                        function_target: Json = {"name": "", "arguments": ""}
                        tool_calls[index] = {"id": "", "type": "function", "function": function_target}
                        tool_call_functions[index] = function_target  # 新调用的首个增量:建空壳并登记
                    call = tool_calls[index]
                    if call_id:
                        call["id"] = call_id
                    function = self.message_field(raw, "function")
                    target = tool_call_functions[index]
                    if name := self.message_field(function, "name"):
                        target["name"] = str(name)
                    if arguments := self.message_field(function, "arguments"):
                        target["arguments"] = str(target["arguments"]) + str(arguments)  # 参数是增量追加:拼接而非覆盖
                if chunk_finish_reason := str(self.message_field(choice, "finish_reason") or ""):
                    finish_reason = chunk_finish_reason
                if finish_reason == "tool_calls" and content and tool_calls and not output_promoted:
                    # 这是 Chat 协议第一个能证明消息完整的边界:此时才提升"输出完成"。
                    self._emit_stream("output_done", "".join(content))
                    output_promoted = True
        finally:
            self._emit_stream("", "")  # 空事件标记流结束,UI 据此收尾
        message: Json = {"content": "".join(content) or None}  # 无内容用 None,与协议规范一致
        if reasoning_content:
            message["reasoning_content"] = "".join(reasoning_content)
        if reasoning:
            message["reasoning"] = "".join(reasoning)
        if reasoning_details:
            # OpenRouter 定义完整序列为各 delta 的 reasoning_details 数组按序拼接;
            # 原样回放到 assistant 消息上供后续请求回放。
            # 证据:https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
            message["reasoning_details"] = reasoning_details
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]  # 按 index 升序输出
        return message, usage, finish_reason

    def api_request(
        self,
        messages: list[Json],
        tools: list[Json] | None,
        *,
        allow_stream: bool = True,
        include_builtin_tools: bool = True,
    ) -> tuple[Json, list[ToolCall], str]:
        api = self.session.config.provider.resolve().api  # 按 provider 解析出的协议分派
        if api == "anthropic":
            request = self.anthropic_request
        elif api == "responses":
            request = self.responses_request
        else:
            request = self.chat_request
        if include_builtin_tools:
            return request(messages, tools) if allow_stream else request(messages, tools, allow_stream=False)
        return (
            request(messages, tools, include_builtin_tools=False) if allow_stream else request(messages, tools, allow_stream=False, include_builtin_tools=False)
        )  # allow_stream=False 用于压缩等必须一次返回的场景

    def responses_request(
        self,
        messages: list[Json],
        tools: list[Json] | None,
        *,
        allow_stream: bool = True,
        include_builtin_tools: bool = True,
    ) -> tuple[Json, list[ToolCall], str]:
        provider = self.session.config.provider
        resolved = provider.resolve()
        stream = allow_stream and provider.stream and self.on_stream is not None  # 与 chat 路径相同的流式条件
        params: Json = {
            "model": provider.model,
            "input": self.responses_input(Text.value(messages)),
            "stream": stream,
            "store": False,  # 无状态请求:不把响应存到 provider 侧
        }
        if provider.max_tokens > 0:
            params["max_output_tokens"] = provider.max_tokens  # Responses 协议中输出上限叫 max_output_tokens
        builtin = self.builtin_tools(resolved) if include_builtin_tools else []
        if request_tools := [*self.responses_tool_schemas(tools or []), *builtin]:
            params["tools"] = request_tools
            params["tool_choice"] = "auto"
            params["parallel_tool_calls"] = True
        if prompt_cache_key := self.prompt_cache_key(provider, tools, include_builtin_tools=include_builtin_tools):
            params["prompt_cache_key"] = prompt_cache_key
        # 无状态请求默认返回加密 reasoning 条目,因此下面的回放不需要 `include`;
        # effort 与 chat 路径一样走兼容折叠;当 reasoning 关闭时,定义了显式 "off" 写法的
        # 主机仍然会收到它。
        if resolved.responses_reasoning:
            if effort := resolved.reasoning_effort:
                params["reasoning"] = {"effort": effort}  # 用解析后的 effort 值
            elif provider.reasoning == "off":
                # 该模型不支持 off 语义:必须明确报错,而不是静默忽略配置。
                raise ModelError("reasoning off is not defined for this Responses model; use a supported effort or configure a documented provider endpoint")
        if provider.temperature is not None and not resolved.suppress_temperature:
            params["temperature"] = provider.temperature  # 部分主机在思考模式下固定/拒绝 temperature
        if provider.extra_body:
            params["extra_body"] = provider.extra_body  # 透传扩展字段
        client = self.client()
        if stream:
            result = self.call_client(client, lambda: self._responses_stream(client, params))
            streamed = True
        else:
            result = self.call_client(client, lambda: client.responses.create(**params))
            streamed = False
        self._record_usage(self.message_field(result, "usage"))
        return self.responses_result(result, streamed)

    def _responses_stream(self, client: OpenAI, params: Json) -> Any:
        """消费 Responses 流,在工具参数结束前把已完成的文本提升为"输出完成"。

        文本完成与函数调用发现是相互独立的事件,谁先到都不一定。因此提升是一个
        双条件状态转移,而不是对顺序的假设;终态 response 仍会被正常消费,
        用于历史、工具调用与 usage。
        """

        terminal: Any = None
        output: list[str] = []
        text_done = handoff_seen = output_promoted = False

        def promote_output() -> None:
            nonlocal output_promoted
            if text_done and handoff_seen and output and not output_promoted:  # 文本完、工具边界已见、有内容且未提升过
                self._emit_stream("output_done", "".join(output))
                output_promoted = True

        try:
            for event in client.responses.create(**params):
                event_type = str(self.message_field(event, "type") or "")
                # 同一事件有两种拼写:总结 reasoning 的主机流式发 summary,暴露原始链路的主机发 text。
                # DeepSeek 只发后者,并文档说明它根本不生成 summary,所以只监听一种拼写
                # 会让思考模型完全没有预览。
                # 证据:https://api-docs.deepseek.com/guides/responses_api
                if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                    self._emit_stream("reasoning", str(self.message_field(event, "delta") or ""))
                elif event_type in ("response.output_text.delta", "response.refusal.delta"):
                    delta = str(self.message_field(event, "delta") or "")
                    output.append(delta)  # refusal 与正常文本走同一收集路径
                    self._emit_stream("output", delta)
                elif event_type in ("response.output_text.done", "response.refusal.done"):
                    text_done = True
                    promote_output()
                elif event_type == "response.output_item.added":
                    item = self.message_field(event, "item")
                    item_type = str(self.message_field(item, "type") or "")
                    if item_type == "function_call":
                        handoff_seen = True  # 本地函数调用:文本已定稿
                        promote_output()
                    elif item_type.endswith("_call"):
                        # provider 侧工具与本地函数调用一样是持久的工具边界:它之前的已完文本
                        # 必须立刻交接,避免下面的实时状态覆盖一个已完成的回答。
                        # provider 侧工具运行在请求内部,没有本地工具行可显示,因此状态标签
                        # 是回合仍在推进的唯一信号。
                        handoff_seen = True
                        promote_output()
                        self._emit_stream(builtin_tool_label(item_type), "")
                elif event_type == "response.output_item.done":
                    item = self.message_field(event, "item")
                    item_type = str(self.message_field(item, "type") or "")
                    # provider 侧调用没有本地工具行,所以流一旦完成它就要立刻上报,
                    # 让转录实时可见。流与终态输出携带相同的调用,因此流式请求下
                    # 解析结果的扫描保持静默;这里就是它们唯一且一次的上报。
                    if item_type.endswith("_call") and item_type != "function_call":
                        # 部分兼容 provider 会省略 output_item.added 事件,所以这里的
                        # 持久上报必须自己建立提升边界。
                        handoff_seen = True
                        promote_output()
                        action = self.message_field(item, "action")
                        query = self.message_field(action, "query") if action is not None else ""
                        self.report_builtin_call(item_type, str(query or ""))
                elif event_type == "response.function_call_arguments.delta":
                    handoff_seen = True  # 函数参数开始流式:文本一定已结束
                    promote_output()
                elif event_type in ("response.completed", "response.incomplete"):
                    # 兼容 provider 可能省略 response.output_text.done;被接受的终态 response
                    # 证明流式文本已定稿,所以它是文本完成的终态兜底。工具边界守卫保证
                    # 普通响应不被提升。
                    text_done = True
                    promote_output()
                    terminal = self.message_field(event, "response")
                elif event_type == "response.failed":
                    terminal = self.message_field(event, "response")  # 失败也取终态,由调用方报错
        finally:
            self._emit_stream("", "")
        if terminal is None:
            raise ModelError("Responses stream ended without a terminal response")  # 流异常结束(如连接断开):没有终态必须显式报错
        return terminal

    def _emit_stream(self, kind: str, delta: str) -> None:
        if self.on_stream is not None:  # 无消费者时直接跳过,零开销
            self.on_stream(kind, delta)

    def responses_input(self, messages: list[Json]) -> list[Json]:
        converted: list[Json] = []
        seen_output_ids: set[str] = set()  # 同一次请求中可能重复出现的 output id 去重
        for message in messages:
            role = str(message.get("role") or "")
            content = message.get("content")
            saved_output = message.get(RESPONSES_OUTPUT_KEY)  # 本消息上一次保存的原始 output 条目
            if role == "assistant" and isinstance(saved_output, list):
                # 有保存条目的 assistant 消息:回放原始条目,保证 provider 校验通过。
                for item in saved_output:
                    if not isinstance(item, dict) or not self.replayable_output_item(item):
                        continue  # 空壳条目(无加密载荷的 reasoning)不能回放
                    if content is None and item.get("type") == "message":
                        continue  # 没有可见文本的 message 条目跳过
                    item_id = str(item.get("id") or "")
                    if item_id and item_id in seen_output_ids:
                        continue
                    if item_id:
                        seen_output_ids.add(item_id)
                    converted.append(item)
                continue
            if role == "tool":
                converted.append(
                    {
                        "type": "function_call_output",  # 工具结果 → function_call_output 条目
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": str(message.get("content") or ""),
                    }
                )
                continue
            if role not in ("system", "developer", "user", "assistant"):
                continue  # 未知角色(如内部事件消息)直接丢弃
            if content is not None:
                # 有内容才生成普通消息条目;带图片的 user 消息在此展开为 Responses 内容格式。
                converted.append(
                    {
                        "role": role,
                        "content": self.session.images.responses_content(message) if role == "user" and self.session.images.refs(message) else str(content),
                    }
                )
            if role == "assistant":
                for raw in message.get("tool_calls") or []:  # 补充工具调用条目
                    if not isinstance(raw, dict):
                        continue
                    raw_function = raw.get("function")
                    function = raw_function if isinstance(raw_function, dict) else {}
                    converted.append(
                        {
                            "type": "function_call",
                            "call_id": str(raw.get("id") or uuid.uuid4().hex),  # 协议要求 call_id 非空:缺 id 时生成
                            "name": str(function.get("name") or ""),
                            "arguments": str(function.get("arguments") or "{}"),  # 缺参数时用空对象
                        }
                    )
        return converted

    @staticmethod
    def replayable_output_item(item: Json) -> bool:
        """判断一个保存的 output 条目是否仍携带后续请求可用的内容。

        无状态 reasoning 在加密载荷中传输,而响应从未被存储,仅凭 id 无法替代它。
        主机若既不返回该载荷也不返回可读的 reasoning,条目就只是个空壳,应丢弃而非回放。
        """
        return item.get("type") != "reasoning" or any(item.get(key) for key in ("encrypted_content", "content", "summary"))

    @staticmethod
    def responses_tool_schemas(tools: list[Json]) -> list[Json]:
        converted: list[Json] = []
        for schema in tools:
            raw_function = schema.get("function")
            function = raw_function if isinstance(raw_function, dict) else {}
            converted.append(
                {
                    "type": "function",
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") if isinstance(function.get("parameters"), dict) else {},
                    "strict": bool(function.get("strict", False)),  # strict schema 开关原样透传
                }
            )
        return converted

    def responses_result(self, result: Any, streamed: bool = False) -> tuple[Json, list[ToolCall], str]:
        if self.message_field(result, "status") == "failed":
            error = self.message_field(result, "error") or "unknown error"
            raise ModelError(f"Responses request failed: {error}")  # 请求级失败(如内容审核),不是部分结果
        output = self.message_field(result, "output") or []
        saved_output = [self.dump_message_item(item) for item in output]  # 归一化为纯 JSON 保存,供后续请求回放
        text_parts: list[str] = []
        tool_calls: list[Json] = []
        calls: list[ToolCall] = []
        for item in output:
            item_type = self.message_field(item, "type")
            if item_type == "message":
                for part in self.message_field(item, "content") or []:
                    part_type = self.message_field(part, "type")
                    if part_type == "output_text":
                        text_parts.append(str(self.message_field(part, "text") or ""))
                    elif part_type == "refusal":
                        text_parts.append(str(self.message_field(part, "refusal") or ""))  # refusal 也当作文本展示
            elif item_type == "function_call":
                name = str(self.message_field(item, "name") or "")
                call_id = str(self.message_field(item, "call_id") or self.message_field(item, "id") or uuid.uuid4().hex)
                arguments = str(self.message_field(item, "arguments") or "{}")
                try:
                    payload = json.loads(arguments, strict=False)
                except json.JSONDecodeError:
                    payload = {}  # 参数 JSON 损坏时按空参数处理,由模型自纠
                tool_calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})
                calls.append(self.tool_call(call_id, name, payload))
        text = "".join(text_parts) or str(self.message_field(result, "output_text") or "")  # 部分主机把文本放在 output_text 顶层字段
        # 流式请求中 provider 侧调用已经实时上报过,且流与终态输出携带相同的调用,
        # 再扫一遍会重复上报——而没有 id 的调用根本无法去重。非流式请求则只能靠这里的扫描。
        if not streamed:
            for item in saved_output:
                item_type = str(item.get("type") or "")
                if item_type.endswith("_call") and item_type != "function_call":
                    action = item.get("action")
                    query = action.get("query") if isinstance(action, dict) else ""
                    self.report_builtin_call(item_type, query if isinstance(query, str) else "")
        if not calls and not text.strip() and self.message_field(result, "status") == "incomplete":
            details = self.message_field(result, "incomplete_details")
            if self.message_field(details, "reason") == "max_output_tokens":  # 只有撞输出上限一种情况可确证为截断
                raise self.truncated_output_error(self.message_field(result, "usage"))
        assistant: Json = {"role": "assistant", "content": text or None, RESPONSES_OUTPUT_KEY: saved_output}  # 原始条目挂在命名空间字段下回放
        if sources := self.responses_sources(saved_output):
            assistant[SEARCH_SOURCES_KEY] = sources  # 搜索结果同样挂命名空间字段
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        return assistant, calls, text

    @classmethod
    def responses_sources(cls, saved_output: list[Json]) -> list[Json]:
        """收集 Responses 主机附加在一条响应上的引用来源。

        两个主机、两个位置:OpenAI 通过消息上的 `url_citation` 注解内联引用,
        而 Qwen 完全不返回引用,只在搜索调用上报告来源。两处都读,一个渲染器就能兼容两者。
        """
        groups: list[Any] = []
        for item in saved_output:
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict):
                        groups.append(part.get("annotations"))  # 消息内容块上的注解
                continue
            action = item.get("action")
            groups.append(action.get("sources") if isinstance(action, dict) else None)
            groups.append(item.get("results"))  # 搜索调用上的结果/来源
        return cls.collect_sources(*groups)

    @staticmethod
    def dump_message_item(item: Any) -> Json:
        if isinstance(item, dict):
            return Text.value(item)
        if hasattr(item, "model_dump"):
            dumped = item.model_dump(mode="json", exclude_none=True)  # SDK 对象(如 pydantic)转纯 JSON
            if isinstance(dumped, dict):
                return Text.value(dumped)
        return {}

    def compact(self, context: str) -> Json:
        return self.internal_json_request(COMPACTION_PROMPT, context, source="compactor")

    def consolidate_memory(self, context: str) -> Json:
        """Run one isolated, non-streaming, tool-free memory-maintenance request."""

        return self.internal_json_request(MEMORY_CONSOLIDATION_PROMPT, context, source="memory consolidator")

    def internal_json_request(self, system: str, context: str, *, source: str) -> Json:
        self.cancel_requested.clear()  # 内部请求不受上一次主回合取消状态影响
        messages = [{"role": "system", "content": system}, {"role": "user", "content": Text.clean(context)}]
        _, calls, content = self.api_request(messages, [], allow_stream=False, include_builtin_tools=False)
        if calls:
            raise ModelError(f"{source} returned an unexpected tool call")
        return self.parse_json_object(content, source=source)

    @classmethod
    def parse_json_object(cls, text: str, *, source: str = "compactor") -> Json:
        text = cls.strip_json_fence(Text.clean(text).strip())  # 去掉 ```json 围栏
        if not text:
            raise ModelError(f"{source} returned empty output")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = repair_json(text, return_objects=True)  # 常规解析失败时用 json_repair 尽力修复
        if isinstance(data, dict):
            return data
        raise ModelError(f"{source} returned invalid JSON: " + Tool.compact(text, 200))  # 错误信息带上截断后的原文便于排查

    @staticmethod
    def strip_json_fence(text: str) -> str:
        match = ModelClient._JSON_FENCE_RE.match(text)
        return (match.group(1) if match else text).strip()

    def client(self) -> OpenAI:
        provider = self.session.config.provider
        if missing := self.session.missing_config():
            raise ModelError("missing config: " + ", ".join(missing))
        # 延迟导入:把约 0.8s 的 provider SDK 导入挡在启动路径之外(参见上方 TYPE_CHECKING 块)
        from openai import OpenAI

        return OpenAI(
            api_key=provider.key, base_url=provider.resolve().base_url, timeout=provider.timeout, max_retries=0, default_headers={"User-Agent": HTTP_USER_AGENT}
        )  # max_retries=0:重试由 ModelClient 自己管理(有进度上报与原因展示),SDK 内置重试会绕过它

    def anthropic_client(self) -> Anthropic:
        provider = self.session.config.provider
        if missing := self.session.missing_config():
            raise ModelError("missing config: " + ", ".join(missing))
        url = provider.resolve().base_url.rstrip("/")  # 去掉尾部斜杠,避免拼出双斜杠
        # 延迟导入:把约 0.8s 的 provider SDK 导入挡在启动路径之外(参见上方 TYPE_CHECKING 块)
        from anthropic import Anthropic

        return Anthropic(
            api_key=provider.key,
            base_url=url.removesuffix("/v1"),  # SDK 会自行追加 /v1:配置里已带 /v1 时去掉,避免重复
            timeout=provider.timeout,
            max_retries=0,
            default_headers={"User-Agent": HTTP_USER_AGENT},
        )

    def report_builtin_call(self, name: str, detail: object) -> None:
        if self.on_builtin_call is not None:  # 无监听者时零开销
            self.on_builtin_call(builtin_tool_label(name), str(detail or "").strip())

    @staticmethod
    def collect_sources(*groups: Any) -> list[Json]:
        """把 provider 侧搜索来源拍平成 `{"url", "title"}` 记录,先出现者胜。

        每家主机用不同的字段名报告同样的两件事,所以在这里统一形状,而不是在每个调用点各归一化一次。
        没有 URL 的记录被丢弃:它无法作为来源展示,只有标题反而会暗示不存在的归属。
        """
        sources: dict[str, Json] = {}  # 用 URL 做键天然去重,并保持"先出现者胜"
        for group in groups:
            for raw in group or []:
                item = raw if isinstance(raw, dict) else ModelClient.dump_message_item(raw)  # 兼容 SDK 对象与 dict
                if not isinstance(item, dict):
                    continue
                # OpenAI 与 OpenRouter 把字段嵌套在 `url_citation` 下一层。
                nested = item.get("url_citation")
                if isinstance(nested, dict):
                    item = nested
                url = str(item.get("url") or "")
                if url and url not in sources:
                    sources[url] = {"url": url, "title": str(item.get("title") or "")}
        return list(sources.values())

    def builtin_tools(self, resolved: ResolvedProvider | None = None, *, strict: bool = True) -> list[Json]:
        """provider 侧工具条目;返回副本,避免请求意外修改已加载的配置。

        这些条目原样进入每条协议的 `tools` 数组。每个主机都以当前协议的形状表达其内置工具
        ——包括同时支持 Chat 与 Responses 的 OpenRouter——所以一个直通实现服务所有主机。
        Qwen Chat 则通过 `extra_body.enable_search` 配置搜索。

        有文档的 provider 会限制每条 wire 能携带哪些 provider 原生条目。wire 之外的条目保持
        配置但处于非激活状态,因此切换模型从不需要破坏性的配置修改。活跃 wire 上的格式错误
        或不支持条目仍然在本地失败。未知主机保持通用直通。

        ``strict=False`` 在不上抛的情况下报告同样条目,用于只读核算(如 token 估算):
        拒绝不支持的条目属于将要发送它的请求,而不是状态栏、`/status` 或仅测量载荷的 resume。
        """

        provider = self.session.config.provider
        entries = provider.builtin_tools
        if not entries:
            return []  # 未配置内置工具:零成本返回
        resolved = resolved or provider.resolve()
        issue = builtin_tools_issue(resolved, entries)  # 检查当前 wire 是否支持这些条目
        if issue is not None:
            if issue.reason == "wire":
                return []  # 整组条目都不属于这条 wire:静默返回空,条目仍保留在配置里
            if not strict:
                return [dict(entry) for entry in entries]  # 只读核算路径:照常返回,不抛错
            raise ModelError(
                f"provider.builtin_tools {', '.join(issue.configured)} are not supported on the {resolved.api} wire "
                f"for {provider.model or '(no model)'} ({resolved.host or 'this provider'}) yet; "
                f"supported provider tools: {', '.join(issue.supported_entries) or '(none)'}"
            )  # 真实请求路径:明确报错,列明支持列表
        return [dict(entry) for entry in entries]  # 副本:请求不得改写配置

    def prompt_cache_key(self, provider: ProviderConfig, tools: list[Json] | None, *, include_builtin_tools: bool = True) -> str:
        configured = provider.prompt_cache_key
        if configured == "off":
            return ""  # 显式关闭
        if configured != "auto":
            return configured  # 用户自定义 key 直接使用
        resolved = provider.resolve()
        if not resolved.prompt_cache_key:
            return ""  # 主机不支持 prompt cache:自动模式返回空
        tool_names: list[str] = []
        for schema in tools or []:
            raw_function = schema.get("function")
            function = raw_function if isinstance(raw_function, dict) else {}
            tool_names.append(str(function.get("name") or schema.get("name") or "(unknown)"))
        # 内置工具也属于缓存前缀的一部分:启用搜索会改变主机在 system prompt 之前渲染的工具块,
        # 因此缓存 key 必须随之变化。
        if include_builtin_tools:
            tool_names.extend(str(entry.get("type") or "(unknown)") for entry in self.builtin_tools(resolved))
        payload = {
            "api": resolved.api,
            "cwd": self.session.cwd,
            "host": resolved.host,
            "model": provider.model,
            "tools": ",".join(sorted(tool_names)) or "(none)",  # 排序:与顺序无关的稳定摘要
        }
        # key 与模型、API、cwd、主机、工具集合绑定,任一变化都使缓存失效。
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return "yucode-" + digest[:24]  # 固定前缀 + 截断摘要,控制在 key 长度限制内

    def anthropic_request(
        self,
        messages: list[Json],
        tools: list[Json] | None,
        *,
        allow_stream: bool = True,
        include_builtin_tools: bool = True,
    ) -> tuple[Json, list[ToolCall], str]:
        messages = Text.value(messages)
        params = self.anthropic_params(messages, tools, include_builtin_tools=include_builtin_tools)
        client = self.anthropic_client()
        stream = allow_stream and self.session.config.provider.stream and self.on_stream is not None  # 同 chat 路径:三条件齐备才流式
        if stream:
            result = self.call_client(client, lambda: self._anthropic_stream(client, params))
            streamed = True
        else:
            result = self.call_client(client, lambda: client.messages.create(**params))
            streamed = False
        self._record_usage(self.message_field(result, "usage"))
        assistant, calls, content = self.anthropic_result(result, streamed)
        return assistant, calls, content

    def _anthropic_stream(self, client: Anthropic, params: Json) -> Any:
        """消费 Messages 内容块,当文本块与工具块都已确定时提升输出完成状态。

        内容块并不保证文本在 `tool_use` 之前,因此块的 start/stop 事件驱动与 Responses 相同的
        顺序无关状态转移。当文本块先完成时,输入 JSON 可能在提升后仍在继续流式。
        """
        output: list[str] = []
        text_blocks: set[int] = set()  # 已开始(尚未结束)的文本块 index
        server_tools: dict[int, dict[str, str]] = {}  # index → server_tool 信息,stop 时上报
        text_done = handoff_seen = output_promoted = False

        def promote_output() -> None:
            nonlocal output_promoted
            if text_done and handoff_seen and output and not output_promoted:  # 双条件状态转移:文本完 + 工具边界已见
                self._emit_stream("output_done", "".join(output))
                output_promoted = True

        try:
            with client.messages.stream(**params) as stream:
                for event in stream:
                    event_type = self.message_field(event, "type")
                    if event_type == "content_block_start":
                        block = self.message_field(event, "content_block")
                        block_type = self.message_field(block, "type")
                        if block_type == "text":
                            text_blocks.add(int(self.message_field(event, "index") or 0))
                        elif block_type == "tool_use":
                            handoff_seen = True  # 本地工具调用:文本已定稿
                            promote_output()
                        elif block_type == "server_tool_use":
                            # provider 侧工具与本地 tool_use 一样是持久的工具边界:它之前的
                            # 已完文本是定稿,必须在下方实时状态覆盖预览之前完成交接。
                            handoff_seen = True
                            promote_output()
                            self._emit_stream(builtin_tool_label(str(self.message_field(block, "name") or "")), "")
                            # query 通过 input_json_delta 流式到达,到 content_block_stop 才完整,
                            # 所以现在登记块、在 stop 时上报,让搜索实时出现在转录中。
                            # 部分主机在 content_block_start 上一次性给出完整 input 而不是流式
                            # 发送;把它作为 stop 处理器在从未收到 partial_json 时的兜底 query。
                            start_input = self.message_field(block, "input")
                            server_tools[int(self.message_field(event, "index") or 0)] = {
                                "id": str(self.message_field(block, "id") or ""),
                                "name": str(self.message_field(block, "name") or ""),
                                "json": "",
                                "query": str(start_input.get("query") or "") if isinstance(start_input, dict) else "",
                            }
                        continue
                    if event_type == "content_block_stop":
                        index = int(self.message_field(event, "index") or 0)
                        if index in text_blocks:
                            text_done = True
                            promote_output()  # 文本块结束:文本定稿
                        elif index in server_tools:
                            info = server_tools.pop(index)
                            # 这里也防御性地建立边界:下面的持久上报不能是文本块完成后第一个
                            # 持久的工具信号。
                            handoff_seen = True
                            promote_output()
                            query = info["query"]
                            if info["json"]:
                                with contextlib.suppress(json.JSONDecodeError):
                                    parsed = json.loads(info["json"])
                                    if isinstance(parsed, dict) and parsed.get("query"):
                                        query = str(parsed["query"])  # 流式拼出的 JSON 里取 query;解析失败退回 start 时的值
                            self.report_builtin_call(info["name"], query)
                        continue
                    if event_type != "content_block_delta":
                        continue  # 其余事件(如 message_start)与本函数无关
                    delta = self.message_field(event, "delta")
                    delta_type = self.message_field(delta, "type")
                    if delta_type == "thinking_delta":
                        self._emit_stream("reasoning", str(self.message_field(delta, "thinking") or ""))  # 思考内容转发实时状态
                    elif delta_type == "text_delta":
                        text = str(self.message_field(delta, "text") or "")
                        output.append(text)
                        self._emit_stream("output", text)
                    elif delta_type == "input_json_delta":
                        index = int(self.message_field(event, "index") or 0)
                        if index in server_tools:
                            server_tools[index]["json"] += str(self.message_field(delta, "partial_json") or "")  # query JSON 增量拼接
                return stream.get_final_message()  # 返回最终消息对象,复用非流式解析路径
        finally:
            self._emit_stream("", "")

    def anthropic_params(self, messages: list[Json], tools: list[Json] | None, *, include_builtin_tools: bool = True) -> Json:
        provider = self.session.config.provider
        resolved = provider.resolve()
        system_text = "\n\n".join(
            str(message.get("content") or "") for message in messages if message.get("role") == "system"
        ).strip()  # 多个 system 消息合并成一段
        # Anthropic 的 prompt caching 是前缀匹配,只在显式 cache_control 断点处生效;
        # 没有断点的话,每一轮都要从头重算整个 prompt。渲染顺序是 tools -> system -> messages,
        # 所以在(唯一的)system 块上打断点,稳定的 tools+system 前缀会被缓存,
        # 之后的每一轮都直接复用。
        system: str | list[Json] = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}] if system_text else system_text
        params: Json = {
            "model": provider.model,
            "system": system,
            "messages": self.anthropic_messages(messages),
            "max_tokens": provider.anthropic_output_cap(),  # Anthropic 协议的 max_tokens 必填
        }
        # 思考模式会把 temperature 钉在默认值;发送其他任何值都会被拒绝。
        builtin = self.builtin_tools(resolved) if include_builtin_tools else []
        if request_tools := [*self.anthropic_tool_schemas(tools or []), *builtin]:
            params["tools"] = request_tools
            params["tool_choice"] = {"type": "auto"}  # Anthropic 的 tool_choice 是对象形式
        effort = provider.reasoning_effort()
        thinking_params = anthropic_thinking_params(
            provider.model,
            provider.reasoning,
            effort,
            self.anthropic_thinking_budget(effort, provider.anthropic_output_cap()),
        )
        params.update(thinking_params)
        thinking = thinking_params.get("thinking")
        thinking_active = anthropic_thinking_always_on(provider.model) or (isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"))
        if provider.temperature is not None and not thinking_active:  # 思考恒开的模型同样不能带 temperature
            params["temperature"] = provider.temperature
        return params

    @staticmethod
    def anthropic_thinking_budget(effort: str, max_tokens: int) -> int:
        """某一 effort 的手动思考预算,保持在请求自身输出预算之内。

        API 要求预算严格小于 max_tokens,因此配置了更小的 `provider.max_tokens` 时,
        预算必须跟着下调而不是让请求失败。1,024 token 下限是文档规定的最小值;
        低于它预算根本无法满足,此时 provider 自己的报错才是最诚实的回答。
        证据:https://platform.claude.com/docs/en/build-with-claude/extended-thinking
        """
        budget = THINKING_BUDGETS.get(effort, THINKING_BUDGETS["medium"])  # 未列出的 effort 按 medium 处理
        return max(1024, min(max_tokens - 1024, budget))  # 夹在 [1024, max_tokens-1024] 区间内

    def anthropic_messages(self, messages: list[Json]) -> list[Json]:
        converted: list[Json] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue  # system 已并入 anthropic_params 的 system 字段
            if role == "user":
                self.append_anthropic_message(converted, "user", self.session.images.anthropic_content(message))  # 本地图片引用 → Anthropic content 数组
            elif role == "assistant":
                blocks = self.anthropic_assistant_blocks(message)
                if blocks:
                    self.append_anthropic_message(converted, "assistant", blocks)
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": str(message.get("tool_call_id") or ""), "content": str(message.get("content") or "")}
                self.append_anthropic_message(converted, "user", [block])  # 工具结果必须是 user 角色下的 tool_result 块
        return converted or [{"role": "user", "content": ""}]  # 消息列表不能为空(协议要求):兜底空 user 消息

    @staticmethod
    def append_anthropic_message(messages: list[Json], role: str, content: str | list[Json]) -> None:
        if messages and messages[-1].get("role") == role:  # 协议要求角色交替:相邻同角色合并
            previous = messages[-1].get("content")
            if isinstance(previous, list) and isinstance(content, list):
                previous.extend(content)  # 列表 + 列表:直接扩展
                return
            if isinstance(previous, list) and isinstance(content, str):
                if content:
                    previous.append({"type": "text", "text": content})  # 列表 + 字符串:包装成 text 块
                return
            if isinstance(previous, str) and isinstance(content, list):
                messages[-1]["content"] = ([{"type": "text", "text": previous}] if previous else []) + content  # 字符串 + 列表:转 text 块后拼接
                return
            if isinstance(previous, str) and isinstance(content, str):
                messages[-1]["content"] = (previous + "\n\n" + content).strip()  # 字符串 + 字符串:换行拼接
                return
        messages.append({"role": role, "content": content})  # 角色不同:新开一条

    def anthropic_assistant_blocks(self, message: Json) -> list[Json]:
        # API 校验 thinking 块必须原样返回,包括签名,因此它产出的回合直接回显保存的块,
        # 而不是从文本和工具调用重新拼装。
        saved = message.get(ANTHROPIC_CONTENT_KEY)
        if isinstance(saved, list) and saved:
            # 消息没有可见文本时,回显中丢弃冗余的 text 块(纯思考回合只回放思考块)。
            return [block for block in saved if isinstance(block, dict) and (message.get("content") is not None or block.get("type") != "text")]
        blocks: list[Json] = []
        content = message.get("content")
        if isinstance(content, str) and content:
            blocks.append({"type": "text", "text": content})  # 无保存块:从 content 重建 text 块
        for raw in message.get("tool_calls") or []:
            if not isinstance(raw, dict):
                continue
            raw_function = raw.get("function")
            function = raw_function if isinstance(raw_function, dict) else {}
            try:
                # strict=False:工具调用参数串常含字面换行(如多行 git commit 消息),
                # 严格模式下它们不是合法 JSON。
                payload = json.loads(str(function.get("arguments") or "{}"), strict=False)
            except json.JSONDecodeError:
                payload = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(raw.get("id") or uuid.uuid4().hex),
                    "name": str(function.get("name") or ""),
                    "input": payload if isinstance(payload, dict) else {"args": [payload]},  # 参数不是对象时包装成 args 数组
                }
            )
        return blocks

    @staticmethod
    def anthropic_tool_schemas(tools: list[Json]) -> list[Json]:
        # Chat 的 function schema 转 Anthropic 的 input_schema 形状。
        def convert(schema: Json) -> Json:
            raw_function = schema.get("function")
            function = raw_function if isinstance(raw_function, dict) else {}
            return {
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters") if isinstance(function.get("parameters"), dict) else {},
            }

        return [convert(schema) for schema in tools]

    def anthropic_result(self, result: Any, streamed: bool = False) -> tuple[Json, list[ToolCall], str]:
        text_parts: list[str] = []
        tool_calls: list[Json] = []
        calls: list[ToolCall] = []
        content_blocks = self.message_field(result, "content") or []
        saved_content = [self.dump_message_item(block) for block in content_blocks]  # 保存原始块供回放
        for block in content_blocks:
            block_type = self.message_field(block, "type")
            # 流式请求中每个 server tool 已实时上报;非流式只能靠这里的扫描。
            if block_type == "server_tool_use" and not streamed:
                raw_input = self.message_field(block, "input")
                query = raw_input.get("query") if isinstance(raw_input, dict) else ""
                self.report_builtin_call(str(self.message_field(block, "name") or ""), query)
            if block_type == "text":
                text_parts.append(str(self.message_field(block, "text") or ""))
            elif block_type == "tool_use":
                raw_input = self.message_field(block, "input")
                payload = raw_input if isinstance(raw_input, dict) else {}
                name = str(self.message_field(block, "name") or "")
                call_id = str(self.message_field(block, "id") or uuid.uuid4().hex)
                arguments = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))  # input 是对象:转成 Chat 形状的参数字符串
                tool_calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})
                calls.append(self.tool_call(call_id, name, payload))
        text = "".join(text_parts)
        if not calls and not text.strip() and self.message_field(result, "stop_reason") == "max_tokens":
            raise self.truncated_output_error(self.message_field(result, "usage"))  # 空结果且撞上输出上限:报告截断
        assistant: Json = {"role": "assistant", "content": text or None, ANTHROPIC_CONTENT_KEY: [block for block in saved_content if block]}
        # 长时间的 server 侧工具运行可以在回合中途暂停并交还。回合通过把这条消息原样发回继续,
        # 上面保存的内容块已经做到了这一点。
        # 证据:https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
        if self.message_field(result, "stop_reason") == "pause_turn":
            assistant[PAUSED_TURN_KEY] = True  # 标记暂停回合:后续原样续发
        if sources := self.anthropic_sources(saved_content):
            assistant[SEARCH_SOURCES_KEY] = sources
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        return assistant, calls, text

    @classmethod
    def anthropic_sources(cls, saved_content: list[Json]) -> list[Json]:
        """收集 Messages 响应的来源:先是引用文本,再是原始搜索结果。

        当搜索本身失败时,`web_search_tool_result` 携带的是错误对象而不是结果列表,
        `collect_sources` 会因为没有 URL 而跳过它。
        """
        groups: list[Any] = []
        for block in saved_content:
            if not isinstance(block, dict):
                continue
            groups.append(block.get("citations"))  # 每个块的引用注解
            if block.get("type") == "web_search_tool_result":
                content = block.get("content")
                groups.append(content if isinstance(content, list) else None)  # 搜索结果列表
        return cls.collect_sources(*groups)

    def apply_provider_params(self, params: Json, provider: ProviderConfig, resolved: ResolvedProvider | None = None) -> None:
        resolved = resolved or provider.resolve()  # 允许调用方复用已解析结果
        chat_reasoning = resolved.chat_reasoning
        reasoning_enabled = provider.reasoning != "off"
        effort = provider.reasoning_effort()
        # 部分原生 API 在全部或部分思考模式下固定或拒绝 temperature。
        if provider.temperature is not None and not resolved.suppress_temperature:
            params["temperature"] = provider.temperature  # 用户未设置则不发送;主机声明抑制时不发送
        extra: Json = {}
        if reasoning_enabled and chat_reasoning == "reasoning":
            # 解析后的 effort,与下面每个控制项一样:文档声明了缩减刻度(如 1-5)的主机
            # 必须在这里折叠该值,而不是让折叠静默地只作用于它的同类项。
            extra["reasoning"] = {"effort": resolved.reasoning_effort or effort}  # 解析值优先,兜底用原始值
        elif chat_reasoning == "reasoning_effort":
            if value := resolved.reasoning_effort:
                params["reasoning_effort"] = value  # 原生支持 reasoning_effort 字段的主机
        elif chat_reasoning == "thinking":
            extra["thinking"] = {"type": "enabled" if reasoning_enabled else "disabled"}  # 需要显式 thinking 开关的主机
            if reasoning_enabled:
                params["reasoning_effort"] = resolved.reasoning_effort
        elif chat_reasoning in ("thinking_toggle", "thinking_effort"):
            extra["thinking"] = {"type": "enabled" if reasoning_enabled else "disabled"}  # 支持开关 + effort 组合的主机
            if reasoning_enabled and chat_reasoning == "thinking_effort":
                params["reasoning_effort"] = resolved.reasoning_effort
        elif chat_reasoning == "enable_thinking":
            extra["enable_thinking"] = reasoning_enabled  # Qwen 风格:布尔开关
            if reasoning_enabled:
                extra["thinking_budget"] = THINKING_BUDGETS.get(effort, THINKING_BUDGETS["medium"])
        # provider 声明的扩展(如 Qwen 网页搜索)原样透传;yucode 自己的 reasoning 字段叠加在上层,
        # 键冲突时保持权威。
        extra_body = {**provider.extra_body, **extra}
        configured_thinking = provider.extra_body.get("thinking")
        managed_thinking = extra.get("thinking")
        if isinstance(configured_thinking, dict) and isinstance(managed_thinking, dict):
            extra_body["thinking"] = {**configured_thinking, **managed_thinking}  # 两层 thinking 都是对象时深合并,managed 覆盖
        if extra_body:
            params["extra_body"] = extra_body

    def assistant_message(self, message: Any) -> Json:
        data: Json = {"role": "assistant", "content": self.message_field(message, "content")}
        for key in ("reasoning_content", "reasoning"):
            value = self.message_field(message, key)
            if value:
                data[key] = Text.value(value)  # 归一化后保留 reasoning 字段
        raw_details = self.message_field(message, "reasoning_details") or []
        details = [item for item in (self.dump_message_item(raw) for raw in raw_details) if item]
        if details:
            data["reasoning_details"] = details
        # 会引用的 Chat 主机(OpenAI 搜索模型、OpenRouter 网页插件)把注解挂在消息上。
        # 在响应级而非消息级报告搜索的主机不在这里处理;它们的来源保持在 provider 放置的位置。
        if sources := self.collect_sources(self.message_field(message, "annotations")):
            data[SEARCH_SOURCES_KEY] = sources  # 消息级 annotations 归一化后挂命名空间字段
        tool_calls: list[Json] = []
        for call in self.message_field(message, "tool_calls") or []:
            function = self.message_field(call, "function")
            tool_calls.append(
                {
                    "id": str(self.message_field(call, "id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(self.message_field(function, "name") or ""),
                        "arguments": str(self.message_field(function, "arguments") or "{}"),
                    },
                }
            )
        if tool_calls:
            data["tool_calls"] = tool_calls
        return data

    @staticmethod
    def message_field(message: Any, key: str) -> Any:
        if isinstance(message, dict):
            return message.get(key)
        value = getattr(message, key, None)  # 兼容 SDK 对象与纯 dict
        if value is not None:
            return value
        extra = getattr(message, "model_extra", None)  # OpenAI SDK 未声明的字段存在 model_extra
        if isinstance(extra, dict) and key in extra:
            return extra[key]
        if hasattr(message, "model_dump"):
            dumped = message.model_dump(mode="json")  # 最后兜底:整体 dump 再取
            if isinstance(dumped, dict):
                return dumped.get(key)
        return None

    def tool_calls(self, message: Any) -> list[ToolCall]:
        calls = []
        for raw in self.message_field(message, "tool_calls") or []:
            function = self.message_field(raw, "function")
            call_id = str(self.message_field(raw, "id") or "")
            name = str(self.message_field(function, "name") or "")
            arguments = str(self.message_field(function, "arguments") or "{}")
            try:
                # strict=False 让参数串中的字面换行(如多行 git commit 消息)能够解析,
                # 而不是丢弃该调用的参数。
                payload = json.loads(arguments, strict=False)
            except json.JSONDecodeError:
                calls.append(ToolCall(id=call_id, name=name, args=[]))  # 参数损坏:记为空参数调用,由模型自纠
                continue
            calls.append(self.tool_call(call_id, name, payload))
        return calls

    @classmethod
    def tool_payload(cls, name: str, payload: object) -> ToolArgs:
        if isinstance(payload, dict) and (tool := TOOL_REGISTRY.get(name)):
            # strict schema 把可选参数表达为可空,因此模型可能对省略的参数显式发送 null。
            # 在 yucode 的所有工具里 null 都表示"缺省",所以直接丢弃。
            cleaned = cls.drop_nulls(payload)
            assert isinstance(cleaned, dict)
            return tool.payload_args(cleaned)  # 注册过 schema 的工具走参数校验
        return [payload]  # 未知工具:按原始参数列表透传

    @classmethod
    def drop_nulls(cls, value: object) -> object:
        if isinstance(value, dict):
            return {key: cls.drop_nulls(item) for key, item in value.items() if item is not None}  # 递归清洗嵌套结构
        if isinstance(value, list):
            return [cls.drop_nulls(item) for item in value]
        return value

    @classmethod
    def tool_call(cls, call_id: str, name: str, payload: object) -> ToolCall:
        # payload_args 可能拒绝畸形参数(如 Bash 的空命令)。把该错误捕获到调用上,
        # 执行时作为工具结果回放给模型自纠,而不是让异常逃逸、中断整个 agent 回合。
        try:
            return ToolCall(id=call_id, name=name, args=cls.tool_payload(name, payload))
        except ToolError as error:  # 只捕获参数校验错误,其他异常照常抛出
            return ToolCall(id=call_id, name=name, args=[], error=str(error))
