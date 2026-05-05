"""yucode context:模型消息投影、去重与压缩。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Hashable
from typing import ClassVar, TypeVar

from yucode.base import (
    ANTHROPIC_CONTENT_KEY,
    MAX_TOOL_OUTPUT_TOKENS,
    PROVIDER_ECHO_KEYS,
    RESPONSES_OUTPUT_KEY,
    SESSION_EVENT_KEY,
    Json,
    Text,
    request_budget_for,
)
from yucode.image import IMAGE_REFS_KEY, TOOL_IMAGE_OBSERVATION_KEY, ImageInputs
from yucode.model import ModelClient
from yucode.prompts import (
    COMPACTION_SUMMARY_TITLE,
    CURRENT_TURN_CONTEXT_TRIMMED,
    PREVIOUS_CONTEXT_TRIMMED,
)
from yucode.prompts import (
    compaction_input as format_compaction_input,
)
from yucode.session import HistorySegment, Session
from yucode.tools import (
    Tool,
)

_IdentityT = TypeVar("_IdentityT", bound=Hashable)


class ContextManager:
    """把 session 状态投影成一次请求的消息,并在超出预算时压缩以适配。

    请求投影在发送边界即时推导、从不存储:回放变换绝不能写回历史。压缩是有意为之的持久化
    例外。当投影后的请求超出预算时,较早的消息被存入保留历史,并替换为一条 summary 检查点。
    分层顺序服务于 prompt-cache 稳定性——版本稳定的 system 与 tools 在前,
    接着是会话稳定的环境/能力上下文,然后才是只追加的对话与当前回合。
    可变的工作状态以工具历史或压缩检查点的形式写入;把重建的块插入对话前缀会
    使后续的缓存复用失效。

    请求局部的变换属于这里而非存储的消息:重复的 MCP schema 与 skill 加载在首次副本处
    折叠为指针,压缩移除它时再重新提升。

    预算 = 上下文上限 − provider 的输出预留 − 安全余量,以真正跨过 wire 的载荷为度量。
    超出预算先压缩先前历史,仍超出才压缩当前回合。
    """

    COMPACT_RECENT_MESSAGES: ClassVar[int] = 8
    MCP_DESCRIBE_BLOCK: ClassVar[re.Pattern] = re.compile(r"<MCPDescribe server=(\".*?\") tool=(\".*?\")>.*?</MCPDescribe>", re.DOTALL)
    SKILL_BLOCK: ClassVar[re.Pattern] = re.compile(r"<Skill name=(\".*?\")>.*?</Skill>", re.DOTALL)
    TOOL_RECORD_KEY: ClassVar[re.Pattern] = re.compile(r"\btr\.\d+\b")

    def __init__(self, session: Session, model: ModelClient | None = None):
        self.session = session
        self.model = model
        # 自动压缩在请求投影内部运行,位于 UI 层之下。这个生命周期钩子让编排层能暴露该真实阶段,
        # 而不必让 context 依赖渲染器。False 在 finally 块中发出,包括模型失败回退到裁剪的情况。
        self.on_compaction: Callable[[bool], None] | None = None

    def model_messages(self, base_system: str, turn_messages: list[Json] | None = None) -> list[Json]:
        # 消息布局固定:system → Environment → 能力上下文 → 对话,保持 prompt-cache 前缀稳定。
        messages: list[Json] = [
            {"role": "system", "content": base_system.strip()},
            {"role": "user", "content": "--- Environment ---\n" + (self.environment() or "(empty)")},
        ]
        for context in (self.memory_context(), self.skills_context(), self.mcp_tools_context()):
            if context:
                messages.append({"role": "user", "content": context})  # 空上下文跳过,顺序固定
        conversation = [*self.session.messages, *(turn_messages or [])]  # 已提交历史 + 本轮暂存消息
        messages.extend(self.dedup_skill_loads(self.dedup_mcp_describes(conversation)))  # 去重只在投影时生效,不写回历史
        return Text.value(messages)

    def dedup_mcp_describes(self, messages: list[Json]) -> list[Json]:
        """把重复的 MCP 描述指向第一次完整描述;压缩删除首个副本后,下一个被提升为新的指向目标。"""
        return self._dedup_tool_blocks(
            messages,
            self.MCP_DESCRIBE_BLOCK,
            lambda match: (str(json.loads(match.group(1))), str(json.loads(match.group(2)))),
            lambda identity, key: f"(repeat describe of {identity[0]}.{identity[1]}; schema shown earlier at {key}, unchanged)",
        )

    def dedup_skill_loads(self, messages: list[Json]) -> list[Json]:
        """与 dedup_mcp_describes 同理:把重复的 skill 加载折叠为对首次加载的引用。"""
        return self._dedup_tool_blocks(
            messages,
            self.SKILL_BLOCK,
            lambda match: str(json.loads(match.group(1))),
            lambda name, key: f"(repeat load of skill {name}; instructions shown earlier at {key}, unchanged)",
        )

    @staticmethod
    def _dedup_tool_blocks(
        messages: list[Json],
        block: re.Pattern,
        identity_from: Callable[[re.Match[str]], _IdentityT],
        marker_for: Callable[[_IdentityT, str], str],
    ) -> list[Json]:
        seen: dict[_IdentityT, str] = {}  # identity → 首个副本所在消息的 tr.N 键
        result: list[Json] = []
        for message in messages:
            content = message.get("content")
            if message.get("role") != "tool" or not isinstance(content, str):
                result.append(message)
                continue  # 只处理字符串内容的 tool 消息
            match = block.search(content)
            if match is None:
                result.append(message)
                continue  # 无匹配块:原样保留
            try:
                identity = identity_from(match)
            except (json.JSONDecodeError, ValueError):
                result.append(message)
                continue  # 身份解析失败(如未闭合引号):不折叠,保留原文
            first_key = seen.get(identity)
            if first_key is None:
                key = ContextManager.TOOL_RECORD_KEY.search(content)
                seen[identity] = key.group(0) if key else "above"  # 用消息里的 tr.N 作指向目标;找不到用 "above"
                result.append(message)
                continue  # 首次出现:原样通过并登记
            marker = marker_for(identity, first_key)
            result.append({**message, "content": block.sub(lambda _, marker=marker: marker, content)})  # 只改 content;lambda 捕获 marker 避免闭包问题
        return result

    def mcp_tools_context(self) -> str:
        return self.session.mcp.render_tools_index() if self.session.mcp else ""  # 无 MCP 管理器时返回空串

    def memory_context(self) -> str:
        return self.session.memory.context() if self.session.memory else ""  # Module 内部冻结为会话稳定的启动快照

    def skills_context(self) -> str:
        return self.session.skills.index() if self.session.skills else ""

    def request_token_budget(self) -> int:
        return request_budget_for(
            self.session.settings.max_context_tokens,
            self.session.config.provider.output_token_budget(),
        )  # 上下文上限减去输出预留

    def request_tokens(self, messages: list[Json], tools: list[Json] | None = None) -> int:
        if self.model is not None:
            return self.model.estimated_request_tokens(messages, tools)  # 有 ModelClient:按真实协议估算
        return self.estimated_tokens(messages) + (self.estimated_tokens(tools) if tools else 0)  # 兜底:本地近似估算

    def update_percent(self, messages: list[Json], tools: list[Json] | None = None) -> int:
        self.session.state.context_percent = min(
            100, self.request_tokens(messages, tools) * 100 // self.request_token_budget()
        )  # 钳制到 100,避免估算超预算时显示异常
        return self.session.state.context_percent

    def update_current_tokens(self, base_system: str) -> int:
        messages = self.model_messages(base_system, self.session._active_turn_messages)  # 用当前暂存回合估算
        tools = Tool.resolved_schemas(self.session)
        tokens = self.request_tokens(messages, tools)
        self.session.state.context_percent = min(100, tokens * 100 // self.request_token_budget())
        return tokens

    def prepare_messages(self, model: ModelClient, base_system: str, turn_messages: list[Json] | None = None, tools: list[Json] | None = None) -> list[Json]:
        messages = self.model_messages(base_system, turn_messages)
        budget = self.request_token_budget()
        raw = self.request_tokens(messages, tools)
        if raw < budget and not self._overdue_by_usage():
            return messages  # 估算未超预算且上次实际用量未超标:直接返回
        compacted, keep = self.compaction_parts()
        if self._compact_messages(model, compacted, keep, PREVIOUS_CONTEXT_TRIMMED, tool_messages=turn_messages):
            messages = self.model_messages(base_system, turn_messages)  # 压缩成功:重新投影
        if turn_messages is not None and self.request_tokens(messages, tools) >= budget:
            compacted, keep = self.turn_compaction_parts(turn_messages)  # 历史压缩后仍超预算:压缩当前回合
            if self._compact_messages(model, compacted, keep, CURRENT_TURN_CONTEXT_TRIMMED, turn_messages=turn_messages):
                messages = self.model_messages(base_system, turn_messages)
        return messages

    def _overdue_by_usage(self) -> bool:
        """上一次完成的请求已占预算约 99%,因此即使估算仍放得下,下一次请求也要压缩。
        估算是主要触发条件;这是它被关闭时的最后一道防线,
        代价是可能压缩一个本可放下的较小后续请求。
        """
        usage = self.session.usage
        return usage.last_prompt_budget > 0 and usage.last_prompt_tokens * 100 >= usage.last_prompt_budget * 99  # 整数比较避免浮点误差;无预算记录时不触发

    def _compact_messages(
        self,
        model: ModelClient,
        compacted: list[Json],
        keep: list[Json],
        fallback_note: str,
        *,
        tool_messages: list[Json] | None = None,
        turn_messages: list[Json] | None = None,
    ) -> bool:
        if not compacted:
            return False  # 没有可压缩内容:不触发钩子
        on_compaction = self.on_compaction
        if on_compaction is not None:
            on_compaction(True)  # 压缩阶段开始
        try:
            try:
                data = model.compact(self.compaction_input(compacted))
            except Exception:  # noqa: BLE001 - 任何模型失败都降级为确定性裁剪
                data = None
            self.apply_compaction(
                data,
                keep,
                tool_messages,
                turn_messages=turn_messages,
                fallback_note=fallback_note if data is None else "",  # 只有模型失败时才附加裁剪说明
                compacted=compacted,
            )
        finally:
            if on_compaction is not None:
                on_compaction(False)  # 无论成败都结束阶段标记
        return True

    def environment(self) -> str:
        info = self.session.system_info
        assert info is not None
        rows = [
            f"- cwd: {info.cwd}",
            f"- session_started_at: {self.session.created_at}",
            # 告诉模型哪些可执行文件可以通过 Bash 驱动。
            "- detected_commands (available via Bash): " + (", ".join(info.commands) or "(none)"),
            f"- os: {info.os}",
            f"- arch: {info.arch}",
            f"- shell_timeout: {self.session.settings.shell_timeout}s",
        ]
        return "\n".join(rows)

    def compaction_input(self, messages: list[Json]) -> str:
        older, recent = self.compaction_parts_for(messages)
        return format_compaction_input(
            state=self.session.state.format(),
            previous_summary=self.session.state.summary,
            older_messages=self.messages_text(older),
            recent_messages=self.messages_text(recent),
        )  # 压缩 prompt 由 prompts 模块统一构造,提示词与代码分离

    def compaction_parts(self) -> tuple[list[Json], list[Json]]:
        """把历史切分为可压缩部分与保留部分,供手动压缩和第一轮自动压缩使用。"""
        messages = self.session.messages
        index = self.latest_user_index(messages)  # 以最近一条真实用户消息为界
        if index is None:
            return self.without_compaction_summaries(messages), []  # 没有用户消息(如纯工具对话):全部可压缩,无保留
        compacted_tail, keep_tail = self.compaction_parts_for(messages[index + 1 :])
        compacted = self.without_compaction_summaries(messages[:index] + compacted_tail)  # 检查点之前的消息可压缩
        keep = self.without_compaction_summaries([messages[index]] + keep_tail)  # 用户消息本身保留:新回合的锚点
        return compacted, keep

    def turn_compaction_parts(self, messages: list[Json]) -> tuple[list[Json], list[Json]]:
        index = self.latest_user_index(messages)
        if index is None:
            compacted, keep = self.compaction_parts_for(messages)
            return self.without_compaction_summaries(compacted), self.without_compaction_summaries(keep)  # 回合内无用户消息:整体可压缩
        compacted, keep = self.compaction_parts_for(messages[index + 1 :])
        return self.without_compaction_summaries(compacted), self.without_compaction_summaries(messages[: index + 1] + keep)  # 用户消息及之前全部保留

    def without_compaction_summaries(self, messages: list[Json]) -> list[Json]:
        return [message for message in messages if not self.is_compaction_summary(message)]  # 检查点不可再被压缩,避免套娃

    def compaction_parts_for(self, messages: list[Json]) -> tuple[list[Json], list[Json]]:
        """把消息切分为可压缩的头部与最近的尾部,绝不在工具交互中间切断。

        切口会向后跨过一连串工具结果以及调用它们的 assistant 消息,因为工具调用被压缩掉结果、
        或结果找不到对应调用的历史,会被所有 provider 拒绝。让 summary 多占几条消息是更便宜的损失。
        """
        cut = max(0, len(messages) - self.COMPACT_RECENT_MESSAGES)  # 默认保留最近 8 条
        if cut < len(messages) and messages[cut].get("role") == "tool":
            while cut > 0 and messages[cut - 1].get("role") == "tool":
                cut -= 1  # 切口落在工具结果上:向前跨过整串结果
            if cut > 0 and messages[cut - 1].get("role") == "assistant" and messages[cut - 1].get("tool_calls"):
                cut -= 1  # 连同发起调用的 assistant 消息一起保留
        return messages[:cut], messages[cut:]

    def messages_text(self, messages: list[Json]) -> str:
        return (
            "\n\n".join(f"{message.get('role', 'message')}:\n{ImageInputs.label_text(message)}" for message in messages) or "(empty)"
        )  # 图片消息用标签文本代替 base64

    def history_title(self, messages: list[Json]) -> str:
        for message in messages:
            if (
                message.get("role") == "user"
                and not message.get(SESSION_EVENT_KEY)
                and not str(message.get("content") or "").startswith(COMPACTION_SUMMARY_TITLE)
                and not ImageInputs.is_tool_observation(message)
            ):
                return Tool.compact(str(message.get("content") or ""), 80)  # 第一条真实用户消息作标题
        return Tool.compact(self.messages_text(messages[:1]), 80) or "compacted context"  # 兜底:第一段文本,再不行用固定文案

    def store_history_segment(self, compacted: list[Json]) -> HistorySegment:
        key = f"seg.{len(self.session.history) + 1}"  # 段编号递增
        text = self.bound_output(self.messages_text(compacted))  # 段文本受输出上限约束
        segment = HistorySegment(key=key, title=self.history_title(compacted), text=text)
        self.session.history.append(segment)
        return segment

    def _summary_block(self, segment: HistorySegment | None) -> list[Json]:
        """一条持久检查点,包含压缩前缀之后所需的一切。"""
        rows = [
            COMPACTION_SUMMARY_TITLE,
            "Summary:",
            self.session.state.summary or "(empty)",
            "",
            "Working state:",
            self.session.state.format(),
        ]
        if segment is not None:
            rows.extend(("", f"Stored history segment: {segment.key}: {segment.title}"))
        return [{"role": "user", "content": "\n".join(rows), SESSION_EVENT_KEY: "compaction_checkpoint"}]  # 内部事件:UI 隐藏、持久化保留

    def apply_compaction(
        self,
        data: Json | None,
        keep: list[Json],
        tool_messages: list[Json] | None = None,
        *,
        turn_messages: list[Json] | None = None,
        fallback_note: str = "",
        compacted: list[Json] | None = None,
    ) -> None:
        self.session.state.compaction_count += 1  # 压缩计数,状态栏可展示
        segment = self.store_history_segment(compacted) if compacted else None  # 被压缩消息存入历史段,供日后召回
        if data is not None:
            self.session.state.apply(data)  # 应用模型压缩结果(goal/plan/summary 等)
        if fallback_note:
            self.session.state.summary = (self.session.state.summary + "\n" + fallback_note).strip()  # 模型失败:说明追加进 summary
        summary_block = self._summary_block(segment)
        if turn_messages is None:
            self.session.messages = summary_block + keep  # 无回合消息:直接替换已提交历史
            prune_context = (self.session.messages if data is not None else [*keep]) + (tool_messages or [])  # 模型失败时只按保留消息裁剪工具记录
        else:
            index = self.latest_user_index(keep)
            insert = len(keep) if index is None else index + 1  # 检查点插在最近用户消息之后
            turn_messages[:] = keep[:insert] + summary_block + keep[insert:]  # 原地修改暂存回合
            prune_context = [*self.session.messages, *turn_messages]
        self.prune_tool_records(prune_context)
        # 已记录的 usage 描述的是压缩前的载荷,不再反映下一次请求将携带的内容
        # (手动 /compact 也会跑一次压缩请求,其 usage 刚覆盖了 last-* 字段)。
        # 清空它们,让"是否超期"守卫和状态栏回退到本地估算,直到下一次普通请求报告真实 usage。
        # 累计总数保留:压缩请求同样计费。
        usage = self.session.usage
        usage.last_prompt_tokens = 0
        usage.last_prompt_budget = 0
        usage.last_cached_prompt_tokens = 0
        usage.last_cache_write_prompt_tokens = 0
        # 上下文替换成功即开启新的 cache generation。普通回合冻结的 Project Memory
        # 索引在这里一次性失效,让下一次投影从磁盘重建;应用前取消/失败不会到达此处。
        if self.session.memory is not None:
            self.session.memory.reset_context()

    def prune_tool_records(self, keep_messages: list[Json]) -> None:
        records = self.session.tool_records
        keep = set(self.TOOL_RECORD_KEY.findall(self.messages_text(keep_messages)))  # 从可见消息中提取所有 tr.N 引用
        self.session.tool_records = [record for record in records if record.key in keep][-400:]  # 只保留被引用的,且不超过 400 条
        self.session.tool_results = {record.key: record.output for record in self.session.tool_records}

    def latest_user_index(self, messages: list[Json]) -> int | None:
        for index in range(len(messages) - 1, -1, -1):  # 从后往前找最近一条
            if (
                messages[index].get("role") == "user"
                and not messages[index].get(SESSION_EVENT_KEY)
                and not self.is_compaction_summary(messages[index])
                and not ImageInputs.is_tool_observation(messages[index])
            ):
                return index
        return None

    def is_compaction_summary(self, message: Json) -> bool:
        return message.get("role") == "user" and str(message.get("content") or "").startswith(COMPACTION_SUMMARY_TITLE)  # 按标题前缀识别

    def bound_output(self, text: str, key: str = "", *, stable_marker: bool = False) -> str:
        estimated = self.estimated_text_tokens(text)
        if estimated <= MAX_TOOL_OUTPUT_TOKENS:
            return text  # 未超限:原样返回
        limit = MAX_TOOL_OUTPUT_TOKENS * 4  # 字符上限 ≈ token 上限 × 4(1 token ≈ 4 字节)
        head_limit = max(1, limit * 2 // 5)  # 头尾按 2:3 分配
        tail_limit = max(1, limit - head_limit)
        head = self.head_excerpt(text, head_limit)
        tail = self.tail_excerpt(text, tail_limit)
        omitted_tokens = max(0, estimated - self.estimated_text_tokens(head) - self.estimated_text_tokens(tail))  # 被省略部分 ≈ 总估算 − 头 − 尾
        note = f'<bounded_output omitted="middle" max_tokens="{MAX_TOOL_OUTPUT_TOKENS}"'
        if not stable_marker:
            note += f' estimated_tokens="{estimated}" omitted_tokens="{omitted_tokens}"'  # 非稳定标记附带估算值,便于诊断
        note += f' recall="{key}"' if key else ""  # 带 recall 键便于检索定位
        note += "/>"
        return "\n".join(part for part in (head.rstrip(), note, tail.lstrip()) if part)  # 空段跳过

    @staticmethod
    def head_excerpt(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rsplit("\n", 1)[0] or text[:limit]  # 在换行处截断避免切开一行;无换行时硬截断

    @staticmethod
    def tail_excerpt(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[-limit:].split("\n", 1)[-1] or text[-limit:]  # 尾部同样在换行处起截

    def estimated_tokens(self, messages: list[Json]) -> int:
        # 归一化的 assistant 字段已包含可见文本与工具调用,provider 回显字段会重复计数。
        # 只保留额外可读的 reasoning;密文与签名是传输状态,其字节长度不能作为 prompt token 估算。
        def readable_provider_context(message: Json) -> list[str]:
            readable: list[str] = []
            responses = message.get(RESPONSES_OUTPUT_KEY)
            if isinstance(responses, list):
                for item in responses:
                    if not isinstance(item, dict) or item.get("type") != "reasoning":
                        continue  # 只取 reasoning 条目
                    readable.extend(str(item[key]) for key in ("content", "summary") if item.get(key))  # content/summary 是可读文本
            anthropic = message.get(ANTHROPIC_CONTENT_KEY)
            if isinstance(anthropic, list):
                for block in anthropic:
                    if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking") and block.get("thinking"):
                        readable.append(str(block["thinking"]))  # thinking 块的可读文本
            return readable

        payload: list[Json] = []
        for message in messages:
            estimated = {
                key: value for key, value in message.items() if key not in (*PROVIDER_ECHO_KEYS, IMAGE_REFS_KEY, TOOL_IMAGE_OBSERVATION_KEY, SESSION_EVENT_KEY)
            }  # 剔除内部字段后序列化
            if readable := readable_provider_context(message):
                estimated["_provider_context"] = readable  # 附加可读 reasoning 占位字段
            payload.append(estimated)
        chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))  # 字节数计量
        images = ImageInputs.estimated_tokens(messages) if self.session.images.support() is not False else 0  # 图片单独估算;不支持图片按 0
        return (chars + 3) // 4 + images  # 1 token ≈ 4 字节,向上取整

    @staticmethod
    def estimated_text_tokens(text: str) -> int:
        return (len(text) + 3) // 4  # 纯文本近似:4 字符 ≈ 1 token
