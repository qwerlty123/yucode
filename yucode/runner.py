"""yucode 工具运行器:批量 Edit 规划、确认与工具执行。"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import ClassVar

from yucode.base import (
    ActiveResource,
    Json,
    LogBlock,
    LogEdge,
    LogLine,
    LogRole,
    ToolCall,
    ToolError,
    builtin_tool_label,
)
from yucode.context import ContextManager
from yucode.session import Session, TurnDiff
from yucode.tools import (
    TOOL_REGISTRY,
    AskSpec,
    AskTool,
    BashTool,
    CodeIndex,
    Edit,
    EditTool,
    JobTool,
    ReadTool,
    Tool,
)


class EditBatchPlan:
    """在写盘之前,先基于内存中的文件模型解析整批 Edit 调用。

    每个 anchor 都指向"模型读到的那一行",但批处理里第二个 Edit 落地的文件已经被第一个
    改动了。因此每行都携带它原来的行号,插入把某行推下去之后,`12:hash` 依然能解析:

        read as        after edit 1
        11 ...         11 ...
        12 target      12 <inserted>
                       13 target      <- origin 12,仍然是 anchor 指向的那一行

    先规划整批,还让确认(confirmation)能展示最终结果,而不是只展示第一步。

    规划阶段完全不碰文件。每个被规划的编辑都会记录它期望的文件内容,并在写入时再次校验:
    如果底层文件在规划后被改掉了,就拒绝写入而不是覆盖。无法规划的调用把错误记在该调用
    id 上而不是抛出异常,从而保持"每个调用恰好一个结果"的约定。
    """

    @dataclass
    class Line:
        text: str
        origin: int | None

    @dataclass
    class FileState:
        path: str
        lines: list[EditBatchPlan.Line]
        original: list[str]
        exists: bool

        def text(self) -> str:
            return "".join(line.text for line in self.lines)

        def current_origin(self, origin: int) -> int | None:
            for index, line in enumerate(self.lines):
                if line.origin == origin:
                    return index
            return None

    @dataclass
    class ApplyResult:
        lines: list[EditBatchPlan.Line]
        changes: list[tuple[int, int, int, int]]
        replacements: list[tuple[int, int, list[str]]]
        replace_all: bool = False

    @dataclass
    class PlannedEdit:
        path: str
        before: str
        after: str
        created: bool
        changes: list[tuple[int, int, int, int]]

        def preview(self, tool: EditTool) -> str:
            return tool.diff(self.path, self.before, self.after) or f"Edit({self.path})"

        def call(self, tool: EditTool) -> str:
            # 写入前再次校验文件状态:规划与执行之间文件可能被外部改动,发现即拒绝(防覆盖)。
            if os.path.isdir(self.path):
                raise ToolError("planned edit is stale; path is a directory")
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as file:
                    current = file.read()
            elif self.created and not self.before:
                current = ""  # 新建文件的空初始状态与 before=="" 等价
            else:
                raise ToolError("planned edit is stale; file changed")
            if current != self.before:
                raise ToolError("planned edit is stale; file changed")  # 内容不符 = 文件被改动过
            if self.created:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)  # 新建文件时补建父目录
            with open(self.path, "w", encoding="utf-8") as file:
                file.write(self.after)
            tool.last_path = tool.session.relpath(self.path)
            tool.last_diff = tool.diff(self.path, self.before, self.after)
            tool.last_before = self.before
            tool.last_after = self.after  # 记录 before/after,供后续 diff 展示与回放
            return "\n".join(
                [
                    f"<Edit path={json.dumps(tool.last_path)}>",
                    tool.file_stat(self.path),
                    tool.last_diff.rstrip(),
                    tool.edit_context(self.after, self.changes),
                    "</Edit>",
                ]
            )

    def __init__(self, session: Session):
        self.session = session
        self.files: dict[str, EditBatchPlan.FileState] = {}  # path -> 内存文件模型(批内共享)
        self.planned: dict[str, EditBatchPlan.PlannedEdit] = {}  # call.id -> 规划好的编辑
        self.errors: dict[str, str] = {}  # call.id -> 规划失败的错误信息

    def build(self, calls: list[ToolCall]) -> EditBatchPlan:
        # 只规划 Edit;其他工具不进批处理模型。失败的调用记到 errors,不打断其余调用。
        for call in calls:
            if call.name != "Edit":
                continue
            try:
                self.plan_call(call, EditTool(self.session, call.args))
            except ToolError as error:
                self.errors[call.id] = str(error)
        return self

    def plan_call(self, call: ToolCall, tool: EditTool) -> None:
        path, edits = tool.parse()
        state = self.file_state(tool, path, edits[0].op == "create")  # 取(或创建)该文件的内存状态
        before, created = state.text(), not state.exists
        before_lines = [line.text for line in state.lines]  # 供"无变化"报错时给出上下文
        result = self.apply(tool, state, edits)
        after = "".join(line.text for line in result.lines)
        if after == before and not created:
            raise ToolError(EditTool.no_changes_error_from_lines(before_lines, result.replacements, result.replace_all))  # 编辑前后完全一样 = 无效编辑
        self.planned[call.id] = self.PlannedEdit(path, before, after, created, result.changes)
        state.lines, state.exists = result.lines, True  # 内存模型推进:后续 Edit 基于新内容解析 anchor

    def file_state(self, tool: EditTool, path: str, creating: bool) -> FileState:
        if path in self.files:
            # 同一批次里已见过该文件:直接用批内状态,但校验 create 语义不能自相矛盾。
            state = self.files[path]
            if not state.exists and not creating:
                raise ToolError("file does not exist; use op=create to create it")
            if state.exists and creating:
                raise ToolError("file already exists")
            return state
        if tool._validate_target(path, creating):
            with open(path, encoding="utf-8") as file:
                original = file.readlines()
            # 每行记录其原始行号(origin),后续编辑即使插行也能继续定位 anchor。
            state = self.FileState(path, [self.Line(line, index) for index, line in enumerate(original)], original, True)
        else:
            state = self.FileState(path, [], [], False)  # 目标不存在(或要新建):空状态
        self.files[path] = state
        return state

    def apply(self, tool: EditTool, state: FileState, edits: list[Edit]) -> ApplyResult:
        result = tool.apply(state.text(), edits, lambda anchor: self.resolve_anchor(state, anchor))
        # 新建文件或 replace_all:内容整体替换,所有行失去 origin(不再对应旧行)。
        if edits[0].op == "create" or result.replace_all:
            return self.ApplyResult(self.new_lines(ReadTool.split_lines(result.content)), result.changes, result.replacements, result.replace_all)
        lines = list(state.lines)
        # 按位置从后往前应用替换,避免前面的替换改动后续区间的坐标。
        for start, end, replacement in sorted(result.replacements, reverse=True):
            lines[start:end] = self.new_lines(replacement)
        return self.ApplyResult(lines, result.changes, result.replacements)

    @staticmethod
    def new_lines(lines: list[str]) -> list[Line]:
        return [EditBatchPlan.Line(line, None) for line in lines]

    def resolve_anchor(self, state: FileState, anchor: str) -> int:
        index, expected = ReadTool.require_anchor(anchor)
        # 首选:该行在"当前批内状态"的原位置仍然匹配(最常见的批内稳定情况)。
        if index < len(state.lines) and ReadTool.anchor_matches(state.lines[index].text, expected):
            return index
        # 其次:原始文件该行匹配,但批内编辑移动了它——沿 origin 追踪到新位置。
        if index < len(state.original) and ReadTool.anchor_matches(state.original[index], expected):
            current = state.current_origin(index)
            if current is not None:
                return current
            raise ToolError(f"stale anchor {anchor}; original line was changed in this batch")  # 原始行在批内被改过,anchor 彻底失效
        relocated = ReadTool.relocated_anchor([line.text for line in state.lines], index, expected)  # 最后尝试:在新内容里模糊定位(附近唯一匹配)
        if relocated is not None:
            return relocated
        current_line = ReadTool.anchor_line(index, state.lines[index].text) if index < len(state.lines) else "out of range"
        raise ToolError(f"stale anchor {anchor}; current is {current_line}")  # 全部失败:报错并附当前行内容,便于模型修正


@dataclass
class ToolDisplay:
    """单个工具调用的渲染信息:批次计数后缀、短调用行、是否以嵌套树打印,
    以及是被自动批准还是用户批准。由 run_one 传给 finish/reject。"""

    batch_suffix: str = ""
    display: str | None = None
    nested_display: bool = False
    approved: bool = False
    auto: bool = False


class ToolRunner:
    """执行一批工具调用,对模型发出的每个调用恰好返回一个结果。

    这个数量是回放(replay)的前提:被拒绝、失败、跳过、畸形、被打断的调用,每个都仍然
    产生一条对应的 tool 消息——因为存在无应答调用的历史在任何 provider 上都是非法的。

    批次是分段(segment)的而不是扁平的:互相独立的只读调用并发执行;变更型与交互型的
    调用保持有序,同一段里的 Edit 一起规划,让它们的 anchor 能对着"前面编辑之后"的
    文件来解析。

    并发只覆盖 `call()`。所有副作用——展示、session 记账、返回的消息——都在本线程上
    按模型原始顺序应用。拒绝确认会短路本批次剩余调用,观察(observation)跟在所有结果
    之后,保证批次可回放。
    """

    BASH_TRANSCRIPT_PREVIEW_LINES: ClassVar[int] = 3  # 转录日志里展示的 Bash 输出行数
    BASH_PREVIEW_LINES: ClassVar[int] = 24  # 常规结果预览行数
    BASH_PREVIEW_LINE_LIMIT: ClassVar[int] = 220  # 单行预览的最大字符数
    EDIT_PATH_RE: ClassVar[re.Pattern] = re.compile(r'<Edit\s+path=(".*?")')  # 从 Edit 输出里提取被改动的路径
    MCP_CALL_RE: ClassVar[re.Pattern] = re.compile(r"(?s)<MCPCall\b[^>]*>\n?(.*?)\n?</MCPCall>\s*$")  # 提取 MCPCall 的结果体

    def __init__(self, session: Session, context: ContextManager, input_fn=input, output_fn=print):
        self.session = session
        self.context = context
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.live_output: Callable[[str, str], None] | None = None
        self.live_start: Callable[[], None] | None = None
        self.question_fn: Callable[[AskSpec, str], str] | None = None
        self._active_bash: ActiveResource[BashTool] = ActiveResource()  # 跟踪当前活跃的 Bash,取消时用

    def cancel(self) -> None:
        # 只取消当前正在运行的 Bash 工具;其余工具要么瞬时完成,要么已由各自的调用方处理。
        self._active_bash.apply(lambda tool: tool.cancel())

    def call_tool(self, tool: Tool, planned_edit: EditBatchPlan.PlannedEdit | None = None) -> str:
        if not isinstance(tool, BashTool):
            # Edit 走规划好的写入路径(带校验);其他工具直接执行。
            return planned_edit.call(tool) if planned_edit and isinstance(tool, EditTool) else tool.call()
        with self._active_bash.track(tool):  # 登记为活跃 Bash,便于跨线程取消
            return tool.call()

    def run(self, calls: list[ToolCall], batch_suffix: str = "") -> list[Json]:
        messages: list[Json] = []
        observations: list[Json] = []
        # 跨分段共享、会被改动的状态:`first` 决定哪个展示携带 batch_suffix;
        # `refused` 一旦有确认被拒绝就短路本批次剩余调用。
        state = {"first": True, "refused": False}
        echoed = self.session.config.provider.builtin_function_names()  # provider 自己执行的 builtin 函数名
        index = 0
        while index < len(calls):
            if state["refused"]:
                # 前面已拒绝:剩余调用全部"跳过",但仍为每个调用产出消息(保证一对一)。
                messages.append(self.skip_message(calls[index]))
                index += 1
                continue
            if calls[index].name in echoed:
                # provider 的 builtin 是握手协议:原样回显参数即可,不经过确认与注册表。
                messages.append(self.builtin_echo_message(calls[index]))
                index += 1
                continue
            # 优先切"并发安全"的连续段;满足并发条件就整段并行执行。
            end = self.parallel_segment_end(calls, index)
            if end - index >= 2 and self.session.settings.max_parallel_tools > 1:
                messages.extend(self.run_parallel(calls[index:end], batch_suffix, state))
                index = end
                continue
            # 否则切串行段:以"变更型/交互型工具"为界,段内 Edit 一起规划。
            end = index + 1 if self.edit_barrier(calls[index]) else self.edit_segment_end(calls, index)
            messages.extend(self.run_serial(calls[index:end], batch_suffix, state, observations))
            index = end
        return [*messages, *observations]  # 消息在前、观察在后:保证可回放

    def builtin_echo_message(self, call: ToolCall) -> Json:
        """应答 provider 自己的 builtin 函数:把它的参数原样返回。

        provider 会自己执行该工具;它发出的调用只是一次握手,协议要求的客户端行为就是把
        参数直接送回。因此结果跳过确认、跳过注册表、也不套用通常的 `tool ... output:`
        格式——这里多加的任何东西都会作为 provider 自身协议的一部分回传给 provider。
        仍然按工具调用记日志,让转录(transcript)显示这次工作确实发生过。依据:
        https://platform.kimi.ai/docs/guide/use-web-search
        """
        # 未识别的名字会被解析成单个原始 payload,而这正是需要原样回显的东西。
        payload = call.args[0] if len(call.args) == 1 else call.args
        content = json.dumps(payload, ensure_ascii=False)
        label = builtin_tool_label(call.name)
        self.output_fn(LogBlock([LogLine(label, self.oneline(content, 120), LogRole.TOOL, LogEdge.BRANCH)]))  # 像普通工具一样展示一行
        return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}

    def skip_message(self, call: ToolCall) -> Json:
        # 为被拒绝批次"跳过"的调用生成消息:说明原因,且保持每个调用一条结果的约定。
        content = self.tool_message(call, "", "Skipped: previous tool call was refused", failed=True)
        return {"role": "tool", "tool_call_id": call.id, "content": content}

    def run_serial(self, segment: list[ToolCall], batch_suffix: str, state: dict[str, bool], observations: list[Json]) -> list[Json]:
        messages: list[Json] = []
        # 段内只要有 Edit 就建批处理规划,让多个 Edit 的 anchor 互相可见。
        plan = EditBatchPlan(self.session).build(segment) if any(call.name == "Edit" for call in segment) else EditBatchPlan(self.session)
        for call in segment:
            suffix = batch_suffix if state["first"] else ""  # 批次后缀只挂在第一个展示上
            state["first"] = False
            status, content, observation = self.run_one(
                call, batch_suffix=suffix, planned_edit=plan.planned.get(call.id), plan_error=plan.errors.get(call.id, "")
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if observation is not None:
                observations.append(observation)  # 观察消息排在所有结果之后
            if status == "refused":
                state["refused"] = True  # 一旦拒绝,标记短路,剩余调用走 skip
        return messages

    def run_parallel(self, segment: list[ToolCall], batch_suffix: str, state: dict[str, bool]) -> list[Json]:
        # 纯 tool.call() 的工作并发执行,但所有副作用(展示、session 记账、tool 消息)都在本
        # 线程按请求顺序应用,这样输出和交回给模型的结果与模型发出调用的顺序一致。
        cap = max(1, self.session.settings.max_parallel_tools)  # 并发上限,至少为 1
        outcomes: list[tuple[str, str, str | None, float] | None] = [None] * len(segment)  # 按位置收集结果,与输入顺序对齐
        with ThreadPoolExecutor(max_workers=min(len(segment), cap), thread_name_prefix="tool") as executor:
            futures = {executor.submit(self.execute_readonly, call): position for position, call in enumerate(segment)}
            for future in as_completed(futures):
                outcomes[futures[future]] = future.result()  # as_completed 乱序到达,靠位置字典还原顺序
        messages: list[Json] = []
        for call, outcome in zip(segment, outcomes):
            suffix = batch_suffix if state["first"] else ""
            state["first"] = False
            assert outcome is not None  # 每个 future 都已完成,不可能为 None
            content = self.finalize_outcome(call, outcome, batch_suffix=suffix)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        return messages

    def parallel_segment_end(self, calls: list[ToolCall], start: int) -> int:
        # 从 start 起数出连续一段"可以并发"的调用;遇到第一个不安全调用即停。
        end = start
        while end < len(calls) and self.parallel_safe(calls[end]):
            end += 1
        return end

    def parallel_safe(self, call: ToolCall) -> bool:
        # 只有"既不改状态、也不阻塞等交互输入"的调用才能并发:只读、自动批准、非交互的
        # 工具(Read/Search/Recall/InspectCode、只读 MCP)。Edit 由 EditBatchPlan 串行协调;
        # Bash 会流式输出并改动状态;Ask 会阻塞等待用户输入。
        tool_class = TOOL_REGISTRY.get(call.name)
        if tool_class is None or call.name in ("Edit", "NextHints") or tool_class in (BashTool, JobTool, AskTool) or tool_class.PRODUCES_MODEL_OBSERVATION:
            return False  # 未知/需串行/产生观察的工具一律不并发
        try:
            return not tool_class(self.session, call.args).needs_confirmation()  # 需要确认 = 不能并发跑
        except Exception:  # noqa: BLE001 - 畸形的第三方工具实现永远不被视为可并发
            return False  # 保守降级:解析失败的工具实现按"不可并发"处理

    def execute_readonly(self, call: ToolCall) -> tuple[str, str, str | None, float]:
        # 并行 worker 里的纯执行:返回 (kind, output, display, elapsed),不做任何展示或 session
        # 写入(那些在主线 finalize_outcome 里做)。分支与 run_one 镜像,只是省去确认
        # (parallel_safe 已保证不需要)。
        started = time.monotonic()
        tool_class = TOOL_REGISTRY.get(call.name)
        if tool_class is None:
            return "reject", f"ToolError: unknown tool {call.name}", None, 0.0
        tool = tool_class(self.session, call.args)
        display = None
        try:
            display = self.short_call(call, tool.short_args())
            if call.error:
                raise ToolError(call.error)  # 解析阶段记录的调用错误,在执行前抛出
            output = tool.call()
        except ToolError as error:
            return "reject", f"ToolError: {error}", display, time.monotonic() - started  # 参数/使用类错误:拒绝
        except Exception as error:  # noqa: BLE001 - 工具失败统一序列化回模型
            return "error", f"ToolError: {error}", display, time.monotonic() - started  # 任意异常都变成失败结果
        return "ok", output, display, time.monotonic() - started

    def finalize_outcome(self, call: ToolCall, outcome: tuple[str, str, str | None, float], batch_suffix: str = "") -> str:
        # 在主线程把 worker 的裸结果转成最终渲染与消息:"ok" 正常收尾,"reject" 拒绝,
        # 其余(错误)按失败收尾。execute_readonly 只产生这三种 kind。
        kind, output, display, elapsed = outcome
        d = ToolDisplay(batch_suffix=batch_suffix, display=display)
        if kind == "ok":
            return self.finish(call, output, elapsed=elapsed, d=d)
        if kind == "reject":
            return self.reject(call, output, d=d)
        return self.finish(call, output, failed=True, elapsed=elapsed, d=d)

    def edit_segment_end(self, calls: list[ToolCall], start: int) -> int:
        # 数出一段连续的"非屏障"调用;屏障(变更型/交互型工具)之后的调用属于下一段。
        end = start
        while end < len(calls) and not self.edit_barrier(calls[end]):
            end += 1
        return end

    def edit_barrier(self, call: ToolCall) -> bool:
        # 屏障 = 会改动状态或产生观察的非 Edit 工具:它们之前不能与 Edit 混在同一规划段里。
        tool_class = TOOL_REGISTRY.get(call.name)
        return call.name != "Edit" and (tool_class is None or tool_class.MUTATES or tool_class.PRODUCES_MODEL_OBSERVATION)

    def run_one(
        self,
        call: ToolCall,
        batch_suffix: str = "",
        planned_edit: EditBatchPlan.PlannedEdit | None = None,
        plan_error: str = "",
    ) -> tuple[str, str, Json | None]:
        """执行单个工具调用,返回 (status, tool 消息, 可选观察)。

        任何出口都会产生一条消息——未知工具、参数畸形、被拒绝、异常——因为批次欠模型
        每个调用一个结果。status 是调用方据此行动的依据:"refused" 会短路本批次剩余
        调用,"failed" 不会。

        顺序是有意义的:展示行在确认之前就构建好,这样被拒绝的调用也显示它请求了什么;
        Bash 的实时预览只在批准之后才开始,未获用户同意的调用不会流出任何内容。"""
        tool_class = TOOL_REGISTRY.get(call.name)
        if tool_class is None:
            return (
                "failed",
                self.reject(call, f"ToolError: unknown tool {call.name}", d=ToolDisplay(batch_suffix=batch_suffix)),
                None,
            )  # 未知工具:仍产出结果消息
        if call.error:
            return "failed", self.reject(call, f"ToolError: {call.error}", d=ToolDisplay(batch_suffix=batch_suffix)), None  # 解析期错误(如参数畸形)
        tool = tool_class(self.session, call.args)
        if isinstance(tool, BashTool):
            tool.live_output = self.live_output  # 把实时输出回调接给 Bash,边跑边渲染
        started = time.monotonic()
        d = ToolDisplay(batch_suffix=batch_suffix)
        if isinstance(tool, AskTool):
            tool.question_fn = self.question_fn
        try:
            d.display = self.short_call(call, tool.short_args())  # 展示行在确认前构建:拒绝时也能看到请求内容
            if plan_error:
                raise ToolError(plan_error)  # 规划阶段的错误(如 stale anchor)在这里统一走拒绝路径
            needs_confirmation = tool.needs_confirmation()
            if needs_confirmation and self.session.settings.yolo:
                d.auto = True
                pre = self.approval_display(call, tool, "auto", batch_suffix=batch_suffix, planned_edit=planned_edit)
                # "auto ..." 头部会与结果行重复;只有当它带有结果行不会重复的预览(例如
                # Edit 的 diff)时才真正展示。自动批准本身由下面结果行上的 [auto] 标记记录。
                if pre.has_children:
                    self.output_fn(pre)
                    d.nested_display = True
            elif needs_confirmation:
                d.nested_display = True
                confirmed, reason = self.confirm(call, tool, batch_suffix=batch_suffix, planned_edit=planned_edit)
                if not confirmed:
                    output = "Cancelled: user refused tool call" + ((": " + reason) if reason else "")
                    return "refused", self.finish(call, output, failed=True, elapsed=time.monotonic() - started, d=d), None  # 拒绝:短路本批次
                d.approved = True
            if isinstance(tool, BashTool) and self.live_start is not None:
                if not d.nested_display:
                    self.output_fn(LogBlock.hierarchy(self.log_root(d.display or self.short_call(call), batch_suffix=batch_suffix, call=call), []))
                    d.nested_display = True  # 批准后才挂出嵌套树,实时输出将渲染在其中
                self.live_start()  # 启动实时预览(暂停状态栏)
            output = self.call_tool(tool, planned_edit)
            observation = tool.model_observation()  # 工具可能附带模型观察(如代码索引扫描结果)
        except ToolError as error:
            return "failed", self.reject(call, f"ToolError: {error}", d=d), None
        except Exception as error:  # noqa: BLE001 - 工具失败统一序列化回模型
            return "failed", self.finish(call, f"ToolError: {error}", failed=True, elapsed=time.monotonic() - started, d=d), None
        return "ok", self.finish(call, output, elapsed=time.monotonic() - started, turn_diff=tool.turn_diff(), d=d), observation

    def reject(
        self,
        call: ToolCall,
        output: str,
        *,
        d: ToolDisplay | None = None,
    ) -> str:
        d = d or ToolDisplay()
        # 拒绝 = 调用从未执行,不产生任何存储结果,key 记 "-";同时记入错误日志。
        self.session.record_tool_error("-", call.name, call.args, output)
        self.output_fn(
            # 已嵌套展示时(有预览树)只追加一行错误;否则走简短的拒绝展示。
            LogBlock.hierarchy(None, [LogLine("error", self.oneline(output.removeprefix("ToolError:").strip(), 220), LogRole.ERROR, LogEdge.END)])
            if d.nested_display
            else self.reject_display(call, output, d=d)
        )
        return self.tool_message(call, "", output, failed=True, display=d.display)

    def reject_display(self, call: ToolCall, output: str, *, d: ToolDisplay) -> LogBlock:
        # 参数/用法类拒绝通常在重试时被模型自行纠正,所以展示一行低调的提示(UiPrinter 会
        # 渲染成暗色)而不是整块红色失败块。模型仍然收到完整错误信息,以便修正调用。
        reason = self.oneline(output.removeprefix("ToolError:").strip(), 60)
        return LogBlock.hierarchy(self.log_root((d.display or self.short_call(call)) + " · rejected: " + reason, LogRole.MUTED, d.batch_suffix, call), [])

    def finish(
        self,
        call: ToolCall,
        output: str,
        *,
        failed: bool = False,
        elapsed: float | None = None,
        store: bool = True,
        turn_diff: TurnDiff | None = None,
        d: ToolDisplay | None = None,
    ) -> str:
        d = d or ToolDisplay()
        tool_class = TOOL_REGISTRY.get(call.name)
        # 成功且工具声明 STORES_RESULT(或未知)才存结果并拿到 key;失败或未声明一律不存。
        key = self.session.store_tool_result(call.name, call.args, output) if not failed and store and (tool_class is None or tool_class.STORES_RESULT) else ""
        if failed:
            self.session.record_tool_error(key or "-", call.name, call.args, output)  # 失败也记账,便于统计与回放
        elif key:
            self.update_code_index(call, output)  # 成功编辑后更新符号索引
            if turn_diff and turn_diff.path and turn_diff.diff:
                # 保存回合内 diff:挂在当前 turn_step/round 上,供 /diff 展示。
                self.session.store_turn_diff(
                    key,
                    self.session.state.turn_step,
                    turn_diff.path,
                    turn_diff.diff,
                    before=turn_diff.before,
                    after=turn_diff.after,
                    round=self.session.state.round_count,
                )
        # SILENT 工具(如 Note)成功时不展示,但失败必须展示,否则用户看不到出错。
        if not (tool_class is not None and tool_class.SILENT) or failed:
            self.output_fn(self.finish_display(call, key, output, failed=failed, elapsed=elapsed, d=d))
        return self.tool_message(call, key, output, failed=failed, display=d.display)

    def tool_message(self, call: ToolCall, key: str, output: str, *, failed: bool = False, display: str | None = None) -> str:
        # 组装送回模型的 tool 消息:成功带存储 key,失败用 "-" 占位并标注 status: failed。
        head = "tool " + ((key + " ") if key else ("- " if failed else "")) + (display or self.short_call(call))
        rows = [head]
        if failed:
            rows.append("status: failed")
        rows.extend(["output:", self.context.bound_output(output, key).rstrip()])  # bound_output 保证输出有界(防超长)
        return "\n".join(rows).strip()

    def update_code_index(self, call: ToolCall, output: str) -> None:
        if call.name != "Edit":
            return  # 只有 Edit 会改动文件,才需要同步索引
        paths = [str(call.args[0])] if call.args and isinstance(call.args[0], str) else []  # 主目标路径来自参数
        for match in self.EDIT_PATH_RE.finditer(output):
            # 输出里 <Edit path=...> 引用的路径一并索引(如一次替换写入的多个文件)。
            with contextlib.suppress(json.JSONDecodeError):
                paths.append(str(json.loads(match.group(1))))
        CodeIndex(self.session).update(list(dict.fromkeys(paths)))  # 去重后更新

    def confirm(self, call: ToolCall, tool: Tool, batch_suffix: str = "", planned_edit: EditBatchPlan.PlannedEdit | None = None) -> tuple[bool, str]:
        # 确认对话框:空回车/yes 批准;no 拒绝;其他输入视为"拒绝并给出理由"(理由会传给模型)。
        self.output_fn(self.approval_display(call, tool, "confirm", batch_suffix=batch_suffix, planned_edit=planned_edit))
        answer = self.input_fn(LogBlock.prefix(2, LogEdge.CONTINUE) + "[Y/n or reason] ").strip()
        lower = answer.lower()
        if lower in {"", "y", "yes"}:
            return True, ""
        return False, "" if lower in {"n", "no"} else answer

    def approval_display(
        self,
        call: ToolCall,
        tool: Tool,
        status: str,
        batch_suffix: str = "",
        planned_edit: EditBatchPlan.PlannedEdit | None = None,
    ) -> LogBlock:
        role = LogRole.TOOL if status == "confirm" else LogRole.AUTO
        root = self.log_root(self.short_call(call), role, batch_suffix, call)
        children = []
        if tool.NAME != "Edit":
            return LogBlock.hierarchy(root, children)  # 只有 Edit 需要 diff 预览
        # 有规划用规划后的预览(展示最终结果),否则用工具自己的预览。
        preview = planned_edit.preview(tool) if planned_edit and isinstance(tool, EditTool) else tool.preview()
        preview_lines = preview.rstrip().splitlines()
        if preview_lines:
            children.append(LogLine("preview", role=LogRole.META, edge=LogEdge.BRANCH))
            children.extend(LogLine("", line, LogRole.DIFF, LogEdge.CONTINUE) for line in preview_lines)
        return LogBlock.hierarchy(root, children)

    def finish_display(
        self,
        call: ToolCall,
        key: str,
        output: str,
        *,
        failed: bool,
        elapsed: float | None = None,
        d: ToolDisplay | None = None,
    ) -> str | LogBlock:
        d = d or ToolDisplay()
        if call.name == "Note" and not failed and d.display:
            # Note 只输出一句话,不套完整结构:去掉 "Note " 前缀直接展示。
            return self.with_batch_suffix(d.display.removeprefix("Note ").strip(), d.batch_suffix)
        # 尾部标签:拒绝/失败/批准/自动,一眼看出调用结局。
        tag = " [refused]" if failed and "user refused" in output else " [failed]" if failed else " [approved]" if d.approved else " [auto]" if d.auto else ""
        tree = d.nested_display or call.name == "Bash"  # Bash 天然是树形结构
        root = self.log_root(d.display or self.short_call(call), LogRole.ERROR if failed else LogRole.TOOL, d.batch_suffix, call)
        children = []
        if failed:
            label = "refused" if "user refused" in output else "error"
            children.append(LogLine(label, self.oneline(output, 220), LogRole.ERROR, LogEdge.END))
        elif call.name == "MCP":
            summary = self.mcp_result_summary(call, output, elapsed)
            if summary:
                children.append(LogLine("", summary, LogRole.META, LogEdge.END))
        elif call.name == "Bash":
            # Bash 只回显几行预览,提示 Ctrl-O 查看完整输出。
            preview = self.bash_result_preview(output, self.BASH_TRANSCRIPT_PREVIEW_LINES)
            if preview:
                duration = f" · {elapsed:.1f}s" if elapsed is not None else ""
                children.append(LogLine("output" + duration, "Ctrl-O for more", LogRole.META, LogEdge.BRANCH))
                children.extend(LogLine("", line, LogRole.OUTPUT, LogEdge.CONTINUE) for line in preview.splitlines())
        elif call.name == "Ask":
            children.append(LogLine("answer", self.oneline(output, 220), LogRole.META, LogEdge.END))
        if tree and not failed:
            children.append(LogLine("stored" if key else "done", key + tag if key else tag.strip(), LogRole.META, LogEdge.END))
        elif not tree:
            # 非树形:key 和标签并进 meta 尾巴,保持单行可读。
            tail = ((" → " + key) if key else "") + tag
            root = LogLine(root.label, root.text, root.role, meta=root.meta + tail, syntax=root.syntax)
        return LogBlock.hierarchy(None if d.nested_display else root, children)

    def log_root(self, display: str, role: LogRole = LogRole.TOOL, batch_suffix: str = "", call: ToolCall | None = None) -> LogLine:
        name, _, args = display.partition(" ")
        tool_class = TOOL_REGISTRY.get(name)
        syntax = ""
        if tool_class is not None:
            syntax = tool_class.log_lexer(call.args) if call is not None else tool_class.LOG_LEXER
        if role is LogRole.MUTED:
            syntax = ""  # 静默/拒绝展示不做语法高亮
        # 批次计数器放进 `meta`(渲染为灰色)而不是 `args`(语法高亮),这样它读起来是同一行
        # 上一个低调的标签,而不是又一个被高亮的 token。
        meta = ("  " + batch_suffix) if batch_suffix else ""
        return LogLine(name, args, role, meta=meta, syntax=syntax)

    def bash_result_preview(self, output: str, line_limit: int | None = None) -> str:
        # 把 Bash 结果拆成 stdout/stderr 两节,各自截断后按节展示。
        sections = []
        for name in ("stdout", "stderr"):
            text = self.tagged_output(output, name).strip()
            if text:
                sections.extend([name + ":", *("  " + line for line in self.preview_lines(text, line_limit))])
        return "\n".join(sections)

    @staticmethod
    def tagged_output(output: str, name: str) -> str:
        # 提取 <stdout>...</stdout> / <stderr>...</stderr> 之间的文本;标签缺失返回空串。
        start_tag = f"<{name}>"
        end_tag = f"</{name}>"
        start = output.find(start_tag)
        if start < 0:
            return ""
        start += len(start_tag)
        if output.startswith("\n", start):
            start += 1  # 跳过紧跟开始标签的换行,避免预览首行是空行
        # stdout 节在下一个 <stderr> 处截断,stderr 节在 </BashToolResult> 处截断,
        # 防止嵌套/同名标签(如 stdout 内容里含 </stdout> 文本)把范围切错。
        next_section = output.find("\n<stderr>\n", start) if name == "stdout" else output.find("\n</BashToolResult>", start)
        end = output.rfind(end_tag, start, next_section if next_section >= 0 else len(output))
        if end < 0:
            return ""
        text = output[start:end]
        return text.removesuffix("\n")

    def preview_lines(self, text: str, line_limit: int | None = None) -> list[str]:
        line_limit = self.BASH_PREVIEW_LINES if line_limit is None else line_limit
        lines = [self.clip_preview_line(line) for line in text.splitlines()]
        if len(lines) <= line_limit:
            return lines
        # 超限时取头尾各一半,中间用省略行代替:两头通常是用户最关心的部分。
        head = line_limit // 2
        tail = line_limit - head
        omitted = len(lines) - line_limit
        noun = "line" if omitted == 1 else "lines"
        return [*lines[:head], f"... {omitted} {noun} omitted ...", *lines[-tail:]]

    def clip_preview_line(self, line: str) -> str:
        line = line.rstrip()  # 行尾空白会破坏多行渲染,先去掉
        return line if len(line) <= self.BASH_PREVIEW_LINE_LIMIT else line[: self.BASH_PREVIEW_LINE_LIMIT - 3].rstrip() + "..."  # 超长行截断并加省略号

    def mcp_result_summary(self, call: ToolCall, output: str, elapsed: float | None) -> str:
        # 只有 "call" 动作才值得摘要;列表/初始化等动作跳过。
        if str((call.args[0] if call.args and isinstance(call.args[0], dict) else {}).get("action")) != "call":
            return ""
        inner = output
        match = self.MCP_CALL_RE.match(output)
        if match:
            inner = match.group(1).strip()  # 剥掉 <MCPCall> 外壳,只留结果体
        if not inner:
            shape = "empty"
        else:
            try:
                data = json.loads(inner)  # 结果常是 JSON:按结构给出形状描述
            except (json.JSONDecodeError, ValueError):
                data = None  # 非 JSON 文本退化为按行数描述
            if isinstance(data, list):
                shape = f"{len(data)} items"
            elif isinstance(data, dict):
                shape = f"{len(data)} fields"
            else:
                shape = f"{inner.count(chr(10)) + 1} lines"
        parts = [f"{shape}, {self.human_size(len(inner))}"]
        if elapsed is not None:
            parts.append(f"{elapsed:.1f}s")
        return "→ " + " · ".join(parts)

    @staticmethod
    def human_size(num_bytes: int) -> str:
        if num_bytes < 1024:
            return f"{num_bytes}B"
        if num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024:.1f}KB"
        return f"{num_bytes / (1024 * 1024):.1f}MB"

    @staticmethod
    def with_batch_suffix(text: str, suffix: str) -> str:
        return text + (("  " + suffix) if suffix else "")

    def short_call(self, call: ToolCall, args: list[str] | None = None) -> str:
        tool_class = TOOL_REGISTRY.get(call.name)
        if args is None:
            try:
                # 工具自带 short_args 用它(如 Bash 只显示命令首段),否则压缩参数为单行。
                args = tool_class(self.session, call.args).short_args() if tool_class is not None else [Tool.compact(arg) for arg in call.args]
            except Exception:  # noqa: BLE001 - 展示格式化必须对畸形工具参数做兜底
                args = [Tool.compact(arg) for arg in call.args]  # 参数畸形时展示兜底到压缩形式
        text = " ".join([call.name, *args]).strip()
        return text if "\n" in text else self.oneline(text, 200)  # 多行参数保留原样,单行则截断到 200 字符

    @staticmethod
    def oneline(text: str, limit: int) -> str:
        text = " ".join(str(text).split())  # 折叠空白:换行/连续空格变单空格
        return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."  # 超限截断,末尾留省略号
