"""yucode 引擎:组装 context、model 和 tools 的 agent 回合(turn)循环。"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable

from yucode.base import (
    PAUSED_TURN_KEY,
    SEARCH_SOURCES_KEY,
    Json,
    MalformedToolCallError,
    ModelError,
    ModelRequestRetry,
    Text,
    ToolCall,
)
from yucode.context import ContextManager
from yucode.image import UserInput
from yucode.memory import MemoryConsolidationOutcome, MemoryConsolidator
from yucode.model import ModelClient, PreparedRequest
from yucode.prompts import (
    INTERRUPT_MARKER,
    LIVE_FOLLOWUP_PREFIX,
    SYSTEM_PROMPT,
)
from yucode.runner import ToolRunner
from yucode.session import QueuedInput, Session
from yucode.tools import (
    Tool,
)

# 识别"文本化"的 <invoke> 调用:整段(含结尾 </invoke>)必须位于消息末尾(\\Z),
# 调用名只允许 [A-Za-z0-9_.:-]——模型用纯文本而非原生 tool_calls 调用工具时走这条路。
_TEXTUAL_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*(?P<quote>[\"'])(?P<name>[A-Za-z0-9_.:-]{1,128})(?P=quote)\s*>"
    r"(?:(?!<invoke\b).)*</invoke>\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
# 识别 Markdown 围栏(``` 或 ~~~)与引用块(>)的起始行,用于判断文本是否在字面量里。
_FENCE_RE = re.compile(r" {0,3}(?P<marker>`{3,}|~{3,})(?P<rest>.*)$")
_BLOCKQUOTE_RE = re.compile(r" {0,3}>")
# 文本化调用最多纠正 5 次:超过即视为模型学不会原生工具协议,直接终止回合并报错。
MAX_TEXTUAL_TOOL_CORRECTIONS = 5


class Agent:
    """运行一个用户回合直到给出最终答案,负责组装 context、model 与 tools。

    一个回合是一笔事务:消息先累积在本地列表里,checkpoint 进 session 的 active-turn
    缓冲区,只有 commit、被中断后 settle、或出错 flush 时才会写入持久化历史。回合
    进行中,任何其他代码都不得向历史追加消息。

    循环在模型请求和工具批次之间交替,直到模型给出不再调用工具的答案、`max_steps`
    耗尽,或用户取消。取消来自另一个线程,并且只在上述边界处被观察到。

    排队输入按请求逐个认领,并且只在请求成功后确认,因此重试永远不会吞掉一条
    跟进消息(follow-up)。
    """

    def __init__(self, session: Session, input_fn=input, output_fn=print):
        self.session = session
        self.model = ModelClient(session)
        self.context = ContextManager(session, self.model)
        self.tools = ToolRunner(session, self.context, input_fn=input_fn, output_fn=output_fn)
        memory = getattr(session, "memory", None)  # 极简嵌入/测试 Session 可以显式不提供可选 memory Module
        self.memory_consolidator = MemoryConsolidator(memory) if memory is not None else None
        self.output_fn = output_fn
        self.cancel_requested = threading.Event()  # 跨线程取消信号:只在模型/工具调用之间检查
        # provider 自带搜索(如内置 web search)在上一回合报告出的来源,按出现顺序存放。
        # UI 把它们渲染在答案下方;回合存储的消息本身保持原样,不加任何来源信息。
        self.turn_sources: list[Json] = []
        # 排队消息被刷入回合时回调(携带这些消息),UI 借此把它们从活动队列区移到
        # scrollback 日志里。由 CommandLoop 设置。
        self.on_queue_flush: Callable[[list[str]], None] | None = None

    def cancel(self) -> None:
        # 取消是"协作式"的:置位信号并通知工具/模型中断,让运行中的请求自行退出。
        self.cancel_requested.set()  # 主循环只在请求边界检查该信号,不打断进行中的调用
        self.tools.cancel()  # 终止正在运行的 Bash 等活跃工具
        self.model.cancel()  # 中止 in-flight 的模型请求

    def consolidate_memory(self, *, on_start: Callable[[], None] | None = None) -> MemoryConsolidationOutcome:
        """Run due project-memory maintenance after a completed main-thread turn."""

        if self.memory_consolidator is None:
            return MemoryConsolidationOutcome()
        return self.memory_consolidator.run_if_due(self.session, self.model, on_start=on_start)

    def raise_if_cancelled(self) -> None:
        # 把取消翻译成 KeyboardInterrupt,复用 run() 里 Ctrl-C 的同一套收尾路径。
        if self.cancel_requested.is_set():
            raise KeyboardInterrupt

    def run(self, user_input: str | UserInput) -> str:
        # —— 回合开始:重置每回合状态 ——
        self.cancel_requested.clear()  # 上一回合可能留下取消信号,必须清掉
        self.turn_sources = []  # 本回合内 provider 搜索来源从零收集
        self.session.clear_quick_hints()  # 新回合使上一回合给出的 quick hints 全部失效
        self.session.state.round_count += 1  # 回合计数:跨回合状态(如 diff 归属)靠它判定
        self.session.state.turn_step = 0  # 步数在下面循环里每次迭代 +1
        tool_batches = 0  # 本回合已执行的工具批次数,用于给后续批次加后缀区分
        malformed_tool_names: list[str] = []  # 跨纠正轮次累计文本化调用名,超限即报错
        user_message = self.session.images.message(user_input)
        user_text = self.session.images.label_text(user_message)
        turn_messages: list[Json] = [user_message]
        if self.session.mcp is not None:
            mentions = self.session.mcp.resolve_mentions(user_text)  # 解析 @server[.tool] 提及
            if mentions:
                # MCP 提及展开成一条独立 user 消息,让工具说明以原文出现在历史里。
                turn_messages.append({"role": "user", "content": mentions})
        if self.session.skills is not None:
            skill_mentions = self.session.skills.resolve_mentions(user_text)  # 解析 $skill 提及
            if skill_mentions:
                turn_messages.append({"role": "user", "content": skill_mentions})
        self.checkpoint_turn(turn_messages)  # 先落一个快照:任何一步出错都能从"回合开始"恢复
        try:
            # 外层循环:一步 = 一次"模型请求 + 一批工具执行";超限后强制结束回合。
            for step in range(self.session.settings.max_steps):
                self.session.state.turn_step = step + 1
                # 非终止批次的提示会被后续步骤取代,只有终止批次保留自己的提示。
                self.session.clear_quick_hints()
                while True:
                    # 内层循环:ModelRequestRetry(可重试的瞬时错误)时原样重发。
                    try:
                        self.raise_if_cancelled()
                        request = self.prepare_request(turn_messages)
                        assistant, tool_calls, content = self.model.request(request.messages, request.tools)
                        self.record_sources(assistant)  # 每次请求都可能附带 provider 搜索来源
                        self.raise_if_cancelled()  # 请求完成后复查取消,避免在发出纠正前被取消
                        # 请求已送达 provider,因此它的跟进消息从这里起归属历史,之后发出的任何
                        # 纠正都落在它们后面——历史必须保持 provider 看到的顺序,因为已发送的
                        # 消息永远无法收回。
                        self.accept_pending_inputs(turn_messages, request.pending)
                        assistant, tool_calls, content = self.correct_textual_tool_calls(
                            assistant,
                            tool_calls,
                            content,
                            base_messages=request.messages,
                            tools=request.tools,
                            names=malformed_tool_names,
                            turn_messages=turn_messages,
                        )
                        break
                    except ModelRequestRetry:
                        continue  # 可重试错误:不落地任何消息,换用同一批消息重发
                if assistant.get(PAUSED_TURN_KEY) and not tool_calls:
                    # provider 暂停了一个长时间运行的服务端工具,而不是结束回合。恢复的方式是
                    # 原样回发这条消息再问一次,所以它像其他任何一步一样进入回合:受 max_steps
                    # 约束、会被 checkpoint,并且即使不带我们的 tool call 也绝不会被当成答案。
                    turn_messages.append(self.assistant_turn_message(assistant, [], content))
                    if content.strip():
                        self.output_fn(content.strip())  # 暂停期间模型说的话仍然展示给用户
                    self.checkpoint_turn(turn_messages)
                    continue  # 进入下一步(受 max_steps 限制,避免无限"暂停-恢复")
                if not tool_calls:
                    if not content.strip():
                        raise ModelError("empty final response")  # 空答案视为模型故障,不落库
                    answer = content.strip()
                    self.finish_turn(turn_messages, self.assistant_turn_message(assistant, [], answer))
                    return answer  # 唯一正常出口:模型不再调用工具,回合结束
                if content.strip() and self.terminal_next_hints(tool_calls):
                    return self.finish_with_next_hints(turn_messages, assistant, tool_calls, content, tool_batches)
                assistant = self.assistant_turn_message(assistant, tool_calls, content)
                turn_messages.append(assistant)
                if content.strip():
                    self.output_fn(content.strip())  # 输出"边想边说"的内容,让用户看到进展
                tool_batches += 1
                # 执行本批工具;同回合的后续批次带 "·N" 后缀,便于用户区分批次。
                turn_messages.extend(self.tools.run(tool_calls, batch_suffix=f"·{tool_batches}" if tool_batches > 1 else ""))
                self.raise_if_cancelled()  # 工具可能跑很久,执行完必须再查一次取消
                self.checkpoint_turn(turn_messages)
            stopped = f"Stopped after max_agent_steps={self.session.settings.max_steps}"
            self.finish_turn(turn_messages, {"role": "assistant", "content": stopped})
            return stopped  # max_steps 耗尽:正常结束,但用户看到的是"被停止"而不是答案
        except KeyboardInterrupt:
            # 用户取消(含跨线程取消):释放排队输入,把被打断的回合按规矩 settle 掉。
            self.session.release_user_inputs()
            self.settle_interrupted_turn(turn_messages)
            self.session.save_snapshot()
            raise  # 重新抛出,让上层打印 "Cancelled"
        except Exception:
            # 兜底:把 active-turn 缓冲刷进历史再落盘,保证已发生的对话不因异常丢失。
            self.session.release_user_inputs()
            self.session.messages.extend(self.session._active_turn_messages)
            self.session._active_turn_messages.clear()
            self.session.state.turn_messages = 0
            self.session.save_snapshot()
            raise

    def correct_textual_tool_calls(
        self,
        assistant: Json,
        tool_calls: list[ToolCall],
        content: str,
        *,
        base_messages: list[Json],
        tools: list[Json],
        names: list[str],
        turn_messages: list[Json],
    ) -> tuple[Json, list[ToolCall], str]:
        """模型以文本形式输出 <invoke> 标记时,用一条真实的协议纠正消息驱动重试。

        每条纠正消息都会先加入回合再发送,重试时保持工具列表不变:凡是到达过 provider 的
        内容必须进入历史,而且工具块属于 prompt cache 前缀的一部分——两者都不能为了单次
        请求而被改写。"""
        corrections: list[Json] = []
        # 只要模型仍在输出文本化调用,就逐条纠正;判据用每次重试后最新返回的 content。
        while not tool_calls and (textual_tool := self.textual_tool_call(content, tools)):
            self.start_textual_tool_correction(names, textual_tool)
            correction: Json = {"role": "user", "content": self.tool_call_correction(textual_tool)}
            corrections.append(correction)
            turn_messages.append(correction)
            self.checkpoint_turn(turn_messages)  # 纠正消息也是一条真实消息,照常 checkpoint
            correction_messages = [*base_messages, *corrections]  # 从原始请求起重放,纠正逐条追加
            while True:
                try:
                    assistant, tool_calls, content = self.model.request(correction_messages, tools)
                    self.record_sources(assistant)
                    break
                except ModelRequestRetry:
                    continue  # 纠正请求同样可重试,不落地任何东西
            self.raise_if_cancelled()  # 一次纠正就是一次完整模型调用,结束后检查取消
        return assistant, tool_calls, content

    def record_sources(self, assistant: Json) -> None:
        """累积整个回合每次请求中 provider 侧搜索产生的来源。

        搜索可能发生在任何一步,而不只是给出答案的那一步,所以按请求逐个收集,才能让
        页脚(footer)描述完整回合。去重由渲染时按 URL 完成,这同时也覆盖了被重试或
        纠正过的请求。"""
        for source in assistant.get(SEARCH_SOURCES_KEY) or []:
            if isinstance(source, dict):  # 只收字典形态的来源,防御异常 payload
                self.turn_sources.append(source)

    def checkpoint_turn(self, turn_messages: list[Json]) -> None:
        # 把当前回合消息快照进 active-turn 缓冲区并落盘:出错或崩溃时这些消息不会丢。
        self.session._active_turn_messages = list(turn_messages)
        self.session.save_snapshot()

    def finish_turn(self, turn_messages: list[Json], assistant: Json) -> None:
        # 回合终结:消息(含最终 assistant 消息)写入持久历史,清空活动缓冲。
        self.session.messages.extend([*turn_messages, assistant])
        self.session._active_turn_messages.clear()
        self.session.state.turn_messages = 0  # 不再有"进行中"的消息,状态行归零

    def terminal_next_hints(self, tool_calls: list[ToolCall]) -> bool:
        """当且仅当一批调用全部是 NextHints 时为 True——这种批次直接终结回合。"""
        # 全 NextHints 的批次不再执行工具,而是把附带文本当答案收尾(见 finish_with_next_hints)。
        return bool(tool_calls) and all(call.name == "NextHints" for call in tool_calls)

    def finish_with_next_hints(self, turn_messages: list[Json], assistant: Json, tool_calls: list[ToolCall], content: str, tool_batches: int) -> str:
        """执行一个全 NextHints 的批次,并用 `content` 作为答案在单次模型调用里收尾回合。

        带工具调用的 assistant 消息只保留调用本身;答案作为独立的最终消息出现,
        保证它在历史里恰好出现一次。"""
        answer = content.strip()
        tool_message = dict(assistant or {})
        tool_message["content"] = None  # 原内容已作为答案独立落一条消息,这里置空避免重复
        tool_message.pop("tool_calls", None)
        turn_messages.append(self.assistant_turn_message(tool_message, tool_calls, ""))
        batches = tool_batches + 1
        turn_messages.extend(self.tools.run(tool_calls, batch_suffix=f"\u00b7{batches}" if batches > 1 else ""))
        self.raise_if_cancelled()
        self.finish_turn(turn_messages, {"role": "assistant", "content": answer})
        return answer

    def settle_interrupted_turn(self, turn_messages: list[Json]) -> None:
        """处理用户 Ctrl-C 打断的回合,与 CLI 展示的两种情况对应。

        *Retract(撤回)*:agent 还什么都没说、没做,回合整体丢弃,仿佛这条消息从未发送——
        model context 与持久化 session 都碰不到它,不过输入历史仍保留它以便 Ctrl-P 回忆。
        *Interrupt(中断)*:agent 已经说过话或调过工具,则部分回合保留(CLI 已展示的就是
        既成事实),再追加一个中断标记,既保持 context 合法,又告诉模型回合提前结束。"""
        self.session._active_turn_messages.clear()  # 活动缓冲先清掉,重放逻辑只看 turn_messages
        self.session.state.turn_messages = 0
        # 若回合里除了 user 消息外什么都没有,即"撤回"情形:直接返回,历史不落任何东西。
        if not any(message.get("role") != "user" for message in turn_messages):
            return
        # 已收到结果的工具调用 ID 集合:只有"发出但未完成"的调用需要补一条 Cancelled 结果,
        # 否则历史里会留下无应答的 tool_calls,在多数 provider 上都是非法状态。
        answered = {message.get("tool_call_id") for message in turn_messages if message.get("role") == "tool"}
        for message in turn_messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id and call_id not in answered:
                    turn_messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": "Cancelled: the user interrupted before this tool call finished."}
                    )
                    answered.add(call_id)
        turn_messages.append({"role": "user", "content": INTERRUPT_MARKER})  # 标记回合提前结束,模型下次从这里续接
        self.session.messages.extend(turn_messages)  # 部分回合正式落库

    def prepare_request(self, turn_messages: list[Json]) -> PreparedRequest:
        pending = self.session.claim_user_inputs()  # 认领排队输入:此后只有本请求负责确认它们
        # 排队的跟进消息带 LIVE_FOLLOWUP_PREFIX 前缀拼进请求,让模型知道它们是实时跟进。
        request_turn = [*turn_messages, *(item.message(LIVE_FOLLOWUP_PREFIX) for item in pending)]
        self.session.state.turn_messages = len(request_turn)
        tools = Tool.resolved_schemas(self.session)  # 每次请求都解析最新工具 schema(可能被 /strict 等改动)
        messages = self.context.prepare_messages(self.model, SYSTEM_PROMPT, request_turn, tools)
        self.context.update_percent(messages, tools)  # 更新上下文占用百分比供状态栏显示
        return PreparedRequest(messages, tools, pending)

    @classmethod
    def textual_tool_call(cls, content: str, tools: list[Json]) -> str | None:
        """识别文本形式的 <invoke> 调用,但不解析它的任何参数(参数留给模型自己纠正)。"""

        match = _TEXTUAL_INVOKE_RE.search(content)
        # 找不到匹配,或匹配落在 Markdown 代码块/引用块里(那只是文档示例),都不算调用。
        if match is None or cls.inside_markdown_literal(content, match.start()):
            return None
        # 从工具 schema 里收集已知函数名:只对真实存在的工具发起纠正,避免浪费请求。
        known = {str(function.get("name") or "") for schema in tools if isinstance(schema, dict) and isinstance((function := schema.get("function")), dict)}
        name = match.group("name")
        return name if name in known else None

    @staticmethod
    def inside_markdown_literal(content: str, offset: int) -> bool:
        """offset 位置是否落在 Markdown 字面量(缩进代码块、引用块或围栏代码块)内。"""
        line_start = content.rfind("\n", 0, offset) + 1
        prefix = content[line_start:offset]
        leading_whitespace = prefix[: len(prefix) - len(prefix.lstrip(" \t"))]
        # 4 空格缩进 = 代码块;行首 ">" = 引用块。两者里的 <invoke> 都只是示例文本。
        if len(leading_whitespace.expandtabs(4)) >= 4 or _BLOCKQUOTE_RE.match(prefix):
            return True

        # 扫描 offset 之前的所有行,跟踪围栏的开合状态:开头由 3+ 个 ` 或 ~ 界定,
        # 用相同字符且长度不小于开头的围栏闭合;关闭后内容回到普通文本。
        fence: tuple[str, int] | None = None
        for line in content[:offset].splitlines():
            match = _FENCE_RE.match(line)
            if match is None:
                continue
            marker = match.group("marker")
            rest = match.group("rest")
            if fence is None:
                # 行内代码(``)不能开围栏;rest 含 "`" 说明是 ```lang 这类带标注的开头,同样略过
                if marker[0] == "`" and "`" in rest:
                    continue
                fence = marker[0], len(marker)
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not rest.strip():
                fence = None  # 遇到闭合围栏(字符相同、长度足够、行内无其他内容),围栏状态归零
        return fence is not None  # 扫描结束时围栏仍开着,说明 offset 在代码块内

    def start_textual_tool_correction(self, names: list[str], name: str) -> None:
        if len(names) >= MAX_TEXTUAL_TOOL_CORRECTIONS:
            # 累计超过上限:模型学不会原生工具协议,直接终止回合并报错(附全部名单)。
            raise self.malformed_tool_call_error([*names, name])
        names.append(name)  # 先记录再纠正:错误信息里能看到完整纠正历史
        on_stream = getattr(self.model, "on_stream", None)
        if callable(on_stream):
            on_stream(f"correcting malformed tool call {len(names)}/{MAX_TEXTUAL_TOOL_CORRECTIONS} · {name}", "")  # 实时反馈:告知用户正在纠正

    @staticmethod
    def tool_call_correction(name: str) -> str:
        # 纠正文案以"协议纠正"名义出现:明确告诉模型上次输出未被执行,请改用原生工具接口。
        return "\n".join(
            [
                "[Runtime protocol correction]",
                f"The previous generation printed a textual <invoke> for {name}. Nothing was executed.",
                "Continue the same task using the native tool interface. Do not output tool markup.",
            ]
        )

    @staticmethod
    def malformed_tool_call_error(names: list[str]) -> MalformedToolCallError:
        count = len(names)
        # 同一个工具名反复出错时用单数表述,否则按先后顺序列出全部名字,便于定位问题。
        if len(set(names)) == 1:
            return MalformedToolCallError(f"Model emitted {names[0]} as text {count} times; none of the textual calls were executed.")
        sequence = ", then ".join(names)
        return MalformedToolCallError(f"Model emitted tool calls as text {count} times ({sequence}); none of the textual calls were executed.")

    def accept_pending_inputs(self, turn_messages: list[Json], pending: list[QueuedInput]) -> None:
        if not pending:
            return  # 没有排队输入就无事可做
        texts = [item.text for item in pending]
        # 按"发给 provider 时带的前缀"落库,而不是裸文本:这里丢掉前缀等于改写已在缓存前缀
        # 里的消息,还会让模型对这条消息的应答失去解释。
        turn_messages.extend(item.message(LIVE_FOLLOWUP_PREFIX) for item in pending)
        self.session.acknowledge_user_inputs(pending)  # 请求已成功,队列确认这些输入(重试不会重复吞)
        if self.on_queue_flush:
            self.on_queue_flush(texts)  # 通知 UI 把消息从活动队列区移进 scrollback

    @staticmethod
    def assistant_turn_message(assistant: Json, tool_calls: list[ToolCall], content: str) -> Json:
        # 规范化 assistant 消息:统一 role,把 ToolCall 对象序列化成 provider 需要的 JSON 形态。
        message = dict(assistant or {})
        message["role"] = "assistant"
        # content 优先用模型返回值,为空才回退到调用方给的 content;都为空则置 None。
        message["content"] = message.get("content") if message.get("content") is not None else (content.strip() or None)
        if not tool_calls:
            message.pop("tool_calls", None)  # 没有调用就不该带 tool_calls 字段
        elif not message.get("tool_calls"):
            # 模型没给 tool_calls 细节时,按规范补全:arguments 把 args 包一层 {"args": ...}。
            message["tool_calls"] = [
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps({"args": call.args}, ensure_ascii=False)}}
                for call in tool_calls
            ]
        return Text.value(message)
