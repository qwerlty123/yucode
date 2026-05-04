"""yucode 的 prompt-toolkit 应用与交互式视图状态。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, ClassVar, TypeVar

from prompt_toolkit import search as pt_search
from prompt_toolkit.application import Application, create_app_session, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions, is_done
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, HighlightIncrementalSearchProcessor, Processor, Transformation
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import SearchToolbar

from yucode.base import (
    SELECTION_BACK,
    SELECTION_FREE_TEXT,
    LogBlock,
    LogEdge,
    YucodeError,
)
from yucode.image import IMAGE_MARKER, ImageInputs, ImageRef, UserInput
from yucode.render import UiPrinter

TUI_MODAL_PENDING = object()  # 模态按键处理器未消费完成时的哨兵:继续等待下一次按键
ViewLine = TypeVar("ViewLine")


@dataclass
class TuiModal:
    """一个正在显示的模态框:内容/按键处理器 + 线程间同步状态。"""

    fragments_fn: Callable[[], StyleAndTextTuples]
    key_fn: Callable[[str, str], Any]
    exclusive: bool = False  # 独占模态(如 /diff 查看器)会切到 alternate screen 全屏显示
    done: threading.Event = field(default_factory=threading.Event)  # TUI 线程置位,唤醒阻塞的 worker 线程
    result: Any = None  # worker 线程阻塞等待的返回值,由 close_modal 写入


@dataclass(frozen=True)
class _EditDelta:
    """两个字符串之间最小的一次编辑差异(纯数据)。"""

    prefix: int  # 未变化公共前缀的长度
    removed: str  # 旧文本中被删除的片段
    inserted: str  # 新文本中新增的片段


def _edit_delta(old: str, new: str) -> _EditDelta:
    """用公共前缀/后缀框定法计算把 ``old`` 变成 ``new`` 的最小编辑。"""
    prefix = 0
    limit = min(len(old), len(new))  # 公共前缀不可能长过较短的文本
    while prefix < limit and old[prefix] == new[prefix]:  # 从头逐字符比对
        prefix += 1
    suffix = 0
    # 从尾部比对公共后缀,但不得越过已算出的前缀区域
    while suffix < len(old) - prefix and suffix < len(new) - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    return _EditDelta(
        prefix=prefix,
        removed=old[prefix : len(old) - suffix],  # 前缀与后缀之间即被替换的区间
        inserted=new[prefix : len(new) - suffix],
    )


class CallbackPlaceholder(Processor):
    """在空输入缓冲区的末尾行追加提示文本(如排队提示);已有内容时不做任何事。"""

    def __init__(self, text_fn: Callable[[], str]):
        self.text_fn = text_fn

    def apply_transformation(self, transformation_input) -> Transformation:
        ti = transformation_input
        text = self.text_fn()
        buffer = ti.buffer_control.buffer
        # 只在"提示非空、缓冲区为空、且是最后一行"时渲染,避免与真实输入或换行冲突
        if not text or buffer is None or buffer.text or ti.lineno != ti.document.line_count - 1:
            return Transformation(ti.fragments)
        return Transformation([*ti.fragments, ("class:queue.hint", text)])  # 提示以独立样式片段追加


class ImageLabelProcessor(Processor):
    """把每个单格图片标记渲染成原子、可读的行内标签。"""

    def __init__(self, images_fn: Callable[[], tuple[ImageRef, ...]]):
        self.images_fn = images_fn

    def apply_transformation(self, transformation_input) -> Transformation:
        ti = transformation_input
        images = self.images_fn()
        before = sum(line.count(IMAGE_MARKER) for line in ti.document.lines[: ti.lineno])  # 本行之前的标记数 = 跨行连续编号的起点
        source = "".join(fragment[1] for fragment in ti.fragments)
        labels: dict[int, str] = {}
        ordinal = before
        for index, char in enumerate(source):
            # 标记可能多于实际图片(如输入清空后残留),越界则不再编号
            if char == IMAGE_MARKER and ordinal < len(images):
                ordinal += 1
                labels[index] = f"[Image #{ordinal} \u00b7 {images[ordinal - 1].name}]"  # 以源文本字符序号为键,之后逐位替换
        if not labels:
            return Transformation(ti.fragments)  # 本行没有标记,原样返回
        fragments: StyleAndTextTuples = []
        source_index = 0
        for fragment in ti.fragments:
            style, text = fragment[0], fragment[1]
            for char in text:
                label = labels.get(source_index)
                # 被替换成标签的字符改用附件样式,普通字符保留原样式
                fragments.append(("class:image.attachment" if label else style, label or char))
                source_index += 1

        def source_to_display(index: int) -> int:
            # 光标映射:源位置 → 展开后的显示位置(每个标签比单格标记多出 len(label)-1 列)
            return index + sum(len(label) - 1 for position, label in labels.items() if position < index)

        def display_to_source(index: int) -> int:
            # 反向映射:逐位置累计显示宽度,返回第一个达到目标的位置;供光标回写
            display = 0
            for position in range(len(source) + 1):
                if display >= index:
                    return position
                value = labels[position] if position in labels else (source[position] if position < len(source) else "")
                display += len(value)
            return len(source)  # 越界目标落到末尾,防越界

        return Transformation(fragments, source_to_display=source_to_display, display_to_source=display_to_source)


class TuiApp:
    """运行在主屏幕上的单一应用:负责实时活动、输入、选择器与状态。

    agent 线程持有主线程;prompt-toolkit 持有 TUI 线程。`request_input` 在两条线程间桥接
    阻塞式审批;已完成的输出则打印在应用上方,进入终端 scrollback。
    """

    MODAL_KEYS: ClassVar[tuple[str, ...]] = tuple(
        "j k h l g G up down left right tab enter escape q r pagedown pageup c-d c-u c-o backspace c-h /".split()  # noqa: SIM905 - 紧凑按键表
    )
    # 运行中分割线的帧预算。移动高亮每帧前进约一格时动画才平滑,
    # 因此 `CommandLoop.QUEUE_SWEEP_CELLS_PER_SEC` 以这个帧率为准。
    ANIMATION_INTERVAL: ClassVar[float] = 1 / 30
    # 空闲刷新:空闲屏幕没有动画,只有 0.2s 的 index 与 MCP 转圈指示器。
    IDLE_REFRESH_INTERVAL: ClassVar[float] = 0.2

    def __init__(
        self,
        *,
        on_chat_submit: Callable[[UserInput], None] | None = None,
        on_running_submit: Callable[[UserInput], None] | None = None,
        on_exit_request: Callable[[], None] | None = None,
        on_force_exit: Callable[[], None] | None = None,
        on_interrupt: Callable[[], None] | None = None,
        on_retry: Callable[[], None] | None = None,
        on_recall: Callable[[], str | UserInput] | None = None,
        on_expand_output: Callable[[], None] | None = None,
        status_fragments_fn: Callable[[], StyleAndTextTuples] | None = None,
        activity_fragments_fn: Callable[[], StyleAndTextTuples] | None = None,
        input_hint_fn: Callable[[], str] | None = None,
        quick_hints_fn: Callable[[], tuple[str, ...]] | None = None,
        editor_context_fn: Callable[[], str] | None = None,
        images: ImageInputs | None = None,
        image_cwd: str = "",
        history: FileHistory | None = None,
        completer: Completer | None = None,
    ) -> None:
        # 未注入的回调一律落为空操作,调用处无需判空
        self.on_chat_submit = on_chat_submit or (lambda _: None)
        self.on_running_submit = on_running_submit or (lambda _: None)
        self.on_exit_request = on_exit_request or (lambda: None)
        self.on_force_exit = on_force_exit or (lambda: None)
        self.on_interrupt = on_interrupt or (lambda: None)
        self.on_retry = on_retry or (lambda: None)
        self.on_recall = on_recall or (lambda: "")
        self.on_expand_output = on_expand_output or (lambda: None)
        self.status_fragments_fn: Callable[[], StyleAndTextTuples] = status_fragments_fn or list
        self.activity_fragments_fn: Callable[[], StyleAndTextTuples] = activity_fragments_fn or list
        self.input_hint_fn = input_hint_fn or (lambda: "")
        self.quick_hints_fn: Callable[[], tuple[str, ...]] = quick_hints_fn or (lambda: ())
        self.editor_context_fn = editor_context_fn or (lambda: "")
        self.images = images if images is not None else ImageInputs(cwd=image_cwd)
        self.input_images: tuple[ImageRef, ...] = ()
        self._last_input_text = ""
        self._changing_input = False
        self.input_error = ""
        self.history = history
        self.input_buffer = Buffer(
            history=history,
            completer=completer,
            complete_while_typing=False,
            enable_history_search=True,
            multiline=True,
            accept_handler=self._accept,
        )
        self.input_buffer.on_text_changed += self._on_input_text_changed  # 文本每次变化都同步图片标记与识别状态
        self.search_toolbar = SearchToolbar()
        self.app: Application | None = None
        self.ready = threading.Event()  # 应用启动就绪事件:TUI 线程置位,agent 线程可等待
        self.input_mode = "chat"  # 输入模式:chat | dispatch | running | approval
        self.quick_hint_focus = -1  # -1 = 焦点在输入框;0..n-1 = 聚焦第 n 个快捷输入 chip
        self.input_prompt = UiPrinter.PROMPT_PREFIX
        self._input_pending: threading.Event | None = None
        self._input_result: str = ""
        self.status_label: str = ""
        self.modal: TuiModal | None = None
        self.modal_lock = threading.Lock()
        self.input_window: Window | None = None
        self.activity_window: Window | None = None
        self.modal_window: Window | None = None
        self.exclusive_modal_window: Window | None = None
        self.status_window: Window | None = None

    def request_input(self, prompt: str) -> str:
        """从 agent 线程调用,内联获取一行用户输入(审批提示、Ask 工具等)。

        阻塞直到 TUI 线程的控件提交;若提交前应用已退出,返回 "" 以便调用方干净收尾。"""
        # 工具审批不能覆盖已经可见的选择器:等该选择器关闭后再复用共享输入行。
        # 借锁的获取/释放形成同步点:持锁期间模态不会开启,锁外继续即"模态已结束"
        with self.modal_lock:
            pass
        event = threading.Event()
        previous_mode, previous_prompt = self.input_mode, self.input_prompt  # 保存现场,结束时恢复
        previous_document: Document | None = None
        previous_images = self.input_images
        self._input_pending = event
        self._input_result = ""

        def switch(document: Document, mode: str, prompt_text: str, done: threading.Event) -> None:
            nonlocal previous_document
            if previous_document is None:
                previous_document = self.input_buffer.document  # 只捕获第一次切换前的文档,嵌套恢复时不互相覆盖
            images = previous_images if mode == previous_mode else ()  # 切回原模式才恢复图片附件,其他模式清空
            self._reset_input(UserInput(document.text, images), cursor_position=document.cursor_position)
            self._set_mode(mode, prompt_text)
            done.set()

        switched = threading.Event()
        # 跨线程调度:所有 TUI 状态变更都必须回到事件循环线程执行
        self._schedule(switch, Document(""), "approval", prompt, switched)
        switched.wait()  # 等 TUI 线程完成切换再阻塞等待结果,避免竞态
        try:
            event.wait()  # 阻塞直到用户在审批输入框提交/取消
        finally:
            self._input_pending = None
            restored = threading.Event()
            # 无论结果如何都恢复原输入状态(模式、提示符、草稿)
            self._schedule(switch, previous_document or Document(""), previous_mode, previous_prompt, restored)
            restored.wait()
        return self._input_result  # 应用退出时事件不会置位,由 run() 兜底置位并回空串

    def set_running(self, label: str) -> None:
        self.status_label = label
        self._set_mode("running", "+> ")  # 运行模式:输入行只读提示,活动区开始渲染

    def set_dispatching(self, prompt: str = "") -> None:
        self._set_mode("dispatch", prompt)

    def set_idle(self) -> None:
        self.status_label = ""
        self._set_mode("chat", UiPrinter.PROMPT_PREFIX)

    def _set_mode(self, mode: str, prompt: str) -> None:
        self.input_mode = mode
        self.input_prompt = prompt
        self.quick_hint_focus = -1  # 切模式时重置快捷输入焦点,避免悬空索引
        if mode not in {"chat", "running"}:  # 输入错误只在可编辑模式下展示,不留脏信息
            self.input_error = ""
        self.invalidate()  # 请求重绘,让新模式立即生效

    def invalidate(self) -> None:
        if self.app is not None:  # 应用未启动(如运行前)时跳过,避免对 None 调用
            self.app.invalidate()

    def invalidate_frame(self) -> None:
        """从远超视觉需要的频率触发源请求重绘。

        模型输出逐 token 到达。运行区域在屏幕上时,动画计时器已按帧率重绘,逐 token 再重绘
        只会让节奏随模型速度摆动;其他区域没有计时器,因此正常重绘。"""
        if self.input_mode != "running":  # running 模式下动画计时器负责帧节奏,这里不重复触发
            self.invalidate()

    def write_to_scrollback(self, callback: Callable[[], None]) -> None:
        """打印在活动应用上方,并等待终端确实接受后再返回。

        `run_in_terminal` 负责擦除/写入/重绘序列,`create_app_session` 把嵌套的
        prompt-toolkit 打印器路由到这个应用的输出。等待是有意的:调用方必须等提升后的
        回复真正进入 scrollback,才能开始输出工具结果。"""
        app = self.app
        if app is None or not app.is_running:
            callback()  # 应用未启动/已退出:直接同步打印,无需跨线程调度
            return
        done = threading.Event()
        errors: list[Exception] = []  # 终端侧错误跨线程收集,最后在 agent 线程重新抛出

        async def write() -> None:
            try:

                def render() -> None:
                    with create_app_session(output=app.output):  # 打印路由到本应用输出,统一渲染通道
                        callback()

                await run_in_terminal(render)  # 挂起应用完成一次完整终端序列,防止输出交错
            except Exception as error:  # noqa: BLE001 - 把终端侧失败带回 agent 线程
                errors.append(error)
            finally:
                done.set()  # 无论成败都解除等待

        def schedule_write() -> None:
            app.create_background_task(write())  # 协程调度到事件循环,保证线程安全

        self._schedule(schedule_write)
        done.wait()
        if errors:
            raise errors[0]  # 失败原样抛出,让调用方按自己的错误路径处理

    def _schedule(self, callback: Callable[..., None], *args: Any) -> None:
        app = self.app
        if app is not None and app.is_running:
            # 应用运行时必须回到事件循环线程,保证 TUI 状态访问线程安全
            loop = app.loop
            assert loop is not None  # 运行中的应用必然持有 loop;断言捕捉不一致状态
            loop.call_soon_threadsafe(callback, *args)  # 可在任意线程调用,是跨线程调度的唯一入口
        else:
            callback(*args)  # 应用未运行:退化为同步执行

    def exit(self) -> None:
        app = self.app
        if app is None:
            return  # 从未启动过则无事可做

        def close() -> None:
            with contextlib.suppress(Exception):  # 已退出/无循环时 exit 可能抛异常,静默即可
                app.exit(result=None)

        self._schedule(close)  # 必须在事件循环线程内退出应用

    def _accept(self, buffer: Buffer) -> bool:
        # 回车处理入口:先处理"空输入 + 焦点在快捷 chip"的快捷提交,再按模式分派
        if self.input_mode == "chat" and not buffer.text.strip() and 0 <= self.quick_hint_focus < len(self.quick_hints()):
            self._reset_input(self.quick_hints()[self.quick_hint_focus])  # 直接提交该 chip 的内容
            self.quick_hint_focus = -1  # 焦点回到输入框
        text = buffer.text
        if self.input_mode == "approval" and self._input_pending is not None:
            # 审批模式:把文本交还给阻塞在 request_input 的 agent 线程
            self._input_result = text
            self._input_pending.set()
            return False  # False = 不真正"接受":缓冲区保持原样,由阻塞线程取走文本
        if self.input_mode == "running":
            if text.strip():  # 空输入不提交;running 下回车也允许排队下一条消息
                value = self._submitted_input()
                if value is None:
                    return True  # 图片准备失败:错误已显示,保留输入让用户修改
                self._append_history(value)
                self._reset_input("")
                self.on_running_submit(value)
                return True
            return False  # 空输入吞掉回车,不产生空消息
        if self.input_mode == "chat":
            if not text.strip():
                return False  # 空白输入忽略
            value = self._submitted_input()
            if value is None:
                return True
            self._append_history(value)
            self._reset_input("")
            self.set_dispatching()  # 提交后立即切 dispatch,防重复回车
            self.on_chat_submit(value)
            return True
        return False

    def _submitted_input(self) -> UserInput | None:
        value = self._recognize_input()
        try:
            value = self.images.prepare(value)  # 图片落盘/校验,失败以 YucodeError 呈现而非崩溃
        except YucodeError as error:
            self.input_error = str(error)  # 错误写入输入行,用户可见并可修改后重试
            self.invalidate()
            return None  # None 信号让调用方放弃提交、保留输入
        self.input_error = ""
        return value

    def _append_history(self, value: UserInput) -> None:
        if self.history is not None:
            self.history.append_string(value.original_text())  # 只记原文(含图片标记),便于回放

    def _recognize_input(self) -> UserInput:
        value = self.images.recognize(self.input_buffer.text, self.input_images)
        # 识别结果与当前输入不一致(标记被规范化等)时,回写规范化文本
        if str(value) != self.input_buffer.text or value.images != self.input_images:
            self._reset_input(value, cursor_position=len(value))  # 文本长度变化时光标放末尾,防越界
        return value

    def _reset_input(self, value: str | UserInput, *, cursor_position: int | None = None) -> None:
        user_input = value if isinstance(value, UserInput) else UserInput(value)
        self._changing_input = True  # 抑制 on_text_changed 级联:reset 触发的事件不应再触发识别
        try:
            self.input_images = user_input.images
            self._last_input_text = str(user_input)
            position = len(user_input) if cursor_position is None else cursor_position  # 未指定光标时默认到末尾
            self.input_buffer.reset(Document(str(user_input), cursor_position=position))
        finally:
            self._changing_input = False  # 无论 reset 是否抛异常都要恢复标志

    def quick_hints(self) -> tuple[str, ...]:
        return self.quick_hints_fn()

    def quick_hint_fragments(self) -> StyleAndTextTuples:
        hints = self.quick_hints()
        if not hints:
            return []  # 无快捷输入时不渲染任何东西
        parts: StyleAndTextTuples = []
        for index, hint in enumerate(hints):
            if index:
                parts.append(("class:quickhint.sep", " │ "))  # 分隔符只在 chip 之间,不在行首
            style = "class:quickhint.focused" if index == self.quick_hint_focus else "class:quickhint"  # 焦点 chip 高亮
            parts.append((style, f" {hint} "))
        return parts

    def cycle_quick_hint_focus(self, reverse: bool = False) -> None:
        count = len(self.quick_hints())
        if not count:
            return  # 没有快捷输入时 Tab 无效果
        focus = self.quick_hint_focus + (-1 if reverse else 1)
        if focus >= count or focus < -1:
            focus = count - 1 if reverse else -1  # 循环折返:-1(输入框)也参与循环
        self.quick_hint_focus = focus
        self.invalidate()

    def tab_or_complete(self, buffer: Buffer, *, reverse: bool) -> None:
        # 空闲 chat 模式下的空输入框:Tab/Shift-Tab 循环切换快捷输入;其他情况都走补全
        if self.input_mode == "chat" and not buffer.text and buffer.complete_state is None and self.quick_hints():
            self.cycle_quick_hint_focus(reverse=reverse)
            return
        self.complete_input(buffer, reverse=reverse)

    def placeholder_text(self) -> str:
        if self.input_mode == "chat" and self.quick_hints():
            # 焦点在 chip 上时隐藏占位符,避免与选中态抢视觉
            return "" if self.quick_hint_focus >= 0 else "Tab cycles suggestions \u00b7 Enter submits"
        return self.input_hint_fn()  # 其他模式用外部注入的提示(如运行中队列提示)

    def _on_input_text_changed(self, buffer: Buffer) -> None:
        """编辑后统一同步所有跟踪输入文本的状态(图片标记、识别)。"""
        text = buffer.text
        if self._changing_input:
            # 程序自身在 reset(如回写识别结果)时只记录文本、跳过派生逻辑,防递归
            self._last_input_text = text
            return
        old = self._last_input_text
        if old == text:
            return  # 无实际变化(纯光标移动)提前返回
        self.input_error = ""  # 输入一有改动就清除上次的错误提示
        delta = _edit_delta(old, text)  # 只按插入/删除区间同步图片,避免全量重算
        self._sync_input_images(old, delta)
        self._last_input_text = text
        # 插入以空白结尾(粘贴图片或按空格)时才重新识别:识别较耗时,避免每键都跑
        if delta.inserted and delta.inserted[-1].isspace() and self.input_mode in {"chat", "running"}:
            self._recognize_input()

    def _sync_input_images(self, old: str, delta: _EditDelta) -> None:
        """删除编辑中被移除的输入文本所对应的图片附件。"""
        removed = delta.removed.count(IMAGE_MARKER)  # 删除区间里出现几个标记,就摘掉几张图片
        if not removed:
            return  # 删的只是普通文本,图片列表不动
        first = old[: delta.prefix].count(IMAGE_MARKER)  # 删除区之前已有标记数 = 附件列表起始下标
        self.input_images = self.input_images[:first] + self.input_images[first + removed :]  # 保持顺序移除,剩余序号稳定

    def show_modal(
        self,
        fragments_fn: Callable[[], StyleAndTextTuples],
        key_fn: Callable[[str, str], Any],
        *,
        exclusive: bool = False,
    ) -> Any:
        """在此 Application 内显示模态框,并阻塞调用方 worker 直到它关闭。"""
        with self.modal_lock:  # 互斥:同一时刻只允许一个模态,worker 线程在此排队
            app = self.app
            if app is None or not app.is_running or self.modal_window is None:
                return None  # 应用不可用(未启动/已退出)时静默返回 None,调用方按"未显示"处理
            modal = TuiModal(fragments_fn, key_fn, exclusive=exclusive)

            def activate() -> None:
                self.modal = modal
                target = self.exclusive_modal_window if exclusive else self.modal_window  # 独占模态用独立容器,便于全屏切换
                assert target is not None  # 布局构建后两窗口必然存在;断言防布局改动引入回归
                app.layout.focus(target)  # 焦点移入模态,按键才路由到 key_fn
                if exclusive:
                    self._use_alternate_screen(True)  # 独占模态切 alternate screen,保护主屏幕内容
                app.invalidate()

            self._schedule(activate)
            modal.done.wait()  # 阻塞 worker 线程直到 close_modal 置位
            return modal.result

    def close_modal(self, result: Any = None) -> None:
        modal = self.modal
        if modal is None:
            return  # 无活动模态(可能已关闭过)
        modal.result = result  # 先写结果再唤醒,保证等待方读到完整值
        self.modal = None
        if self.app is not None and self.input_window is not None:
            self.app.layout.focus(self.input_window)  # 焦点还给输入框;应用可能已退出,需判空
        if modal.exclusive:
            self._use_alternate_screen(False)  # 退出 alternate screen,恢复主屏幕
        self.invalidate()
        modal.done.set()  # 最后唤醒阻塞线程:置位时状态已一致

    def _use_alternate_screen(self, enabled: bool) -> None:
        """让常驻应用在主屏幕与 alternate screen 之间切换。

        独占模态(如 /diff 查看器)占满整个窗格。画在主屏幕上会把上方 transcript 挤出
        屏幕顶部进入 scrollback,关闭模态只会把应用区域缩回去 —— transcript 永远回不来。
        给它们 alternate screen,终端就会像 `less` 那样在退出时恢复 transcript。"""
        app = self.app
        if app is None or app.renderer.full_screen == enabled:
            return  # 状态已一致(含应用未启动)时无需切换
        # 擦除离开屏幕时本应用占用的区域,不留过时 footer(返回时也顺带退出 alternate screen)
        app.renderer.erase()
        app.renderer.full_screen = enabled
        app._request_absolute_cursor_position()  # 请求绝对光标位置,新屏幕坐标基准才正确

    @staticmethod
    def alternate_screen_available() -> bool:
        """该终端里独占模态能否保住主屏幕(即 alternate screen 是否可用)。"""
        if not os.environ.get("TMUX"):
            return True  # 非 tmux 终端几乎都支持 alternate screen,直接放行
        # alternate-screen 是 window 选项:show-options 只在窗口显式覆盖时才有输出,
        # 常见的全局 `set -wg` 形式保持静默。改用 display-message 格式化解析后的值,
        # 两种形式都能回答:1(启用)或 0(禁用)。
        command = ["tmux", "display-message", "-p"]
        if pane := os.environ.get("TMUX_PANE"):
            command.extend(["-t", pane])  # 指定 pane 查询:多窗格布局下各 pane 可单独设置
        command.append("#{alternate-screen}")  # 让 tmux 输出解析后的 1/0 值
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=1, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return True  # 探测失败按可用处理:宁可保守降级也不误判阻止模态
        # tmux 报错或值非 0 都视为可用;只有明确 0(禁用)才返回 False
        return result.returncode != 0 or result.stdout.strip() != "0"

    def modal_fragments(self) -> StyleAndTextTuples:
        return self.modal.fragments_fn() if self.modal is not None else []  # 无模态时返回空,布局中窗口渲染为空白

    def dispatch_modal_key(self, key: str, data: str = "") -> None:
        if self.modal is None:
            return  # 无模态时按键无处可去(过滤器保证不会发生,防御性检查)
        result = self.modal.key_fn(key, data)
        if result is not TUI_MODAL_PENDING:
            self.close_modal(result)  # 返回哨兵以外的值 = 模态想关闭,该值即结果
        else:
            self.invalidate()  # 仍在等待按键:重绘以反映状态变化(如选择移动)

    def status_fragments(self) -> StyleAndTextTuples:
        if self.input_mode == "dispatch" and self.input_prompt:
            return [("ansibrightblack", self.input_prompt)]  # dispatch 模式:顶部显示静默说明提示
        if self.input_mode == "approval" and self.input_prompt:
            frame = "|/-\\"[int(time.monotonic() / 0.2) % 4]  # 0.2s 一帧的旋转指示符
            connector = LogBlock.prefix(2, LogEdge.CONTINUE)
            prompt = (
                [("ansibrightblack", connector), ("class:approval", self.input_prompt[len(connector) :])]
                if self.input_prompt.startswith(connector)
                else [("class:approval", self.input_prompt)]  # 提示符自带树线前缀时保留它,视觉上延续日志树
            )
            return [*prompt, ("class:approval.wait", frame + " ")]  # 旋转符追加在提示文本末尾
        return [("class:prompt", self.input_prompt)]

    def input_error_fragments(self) -> StyleAndTextTuples:
        error = self.input_error
        # 用户带了图片但 provider 不支持时给出解释;无图片或非可编辑模式则不加
        if not error and self.input_images and self.input_mode in {"chat", "running"} and self.images.support() is False:
            error = "Image input is disabled for the active provider/model"
        return [("class:input.error", f"Error: {error}")] if error else []  # 无错误渲染空,条件容器自动隐藏整行

    @staticmethod
    def complete_input(buffer: Buffer, *, reverse: bool = False) -> None:
        if buffer.complete_state is not None:
            buffer.complete_previous() if reverse else buffer.complete_next()  # 菜单已打开:在候选项间切换
            return
        if buffer.completer is None:
            return  # 未配置 completer 时 Tab 无效果
        event = CompleteEvent(completion_requested=True)  # 标记为显式触发的补全,completer 可据此调整行为
        completions = list(buffer.completer.get_completions(buffer.document, event))
        if len(completions) == 1:
            buffer.apply_completion(completions[0])  # 唯一候选直接应用,不弹菜单
        elif completions:
            # 多候选才打开菜单;reverse 时从最后一项开始选
            if reverse:
                buffer.start_completion(select_last=True)
            else:
                buffer.start_completion(select_first=False)

    def _status_bar_window(self, *, dont_extend_height: bool) -> Window:
        return Window(
            FormattedTextControl(self.status_fragments_fn, style="class:bottom-toolbar.text"),
            style="class:bottom-toolbar",
            height=1,
            dont_extend_height=dont_extend_height,
        )

    def build_layout(self) -> Layout:
        # 输入区处理器按顺序叠加:搜索高亮、图片标签、状态提示、队列占位
        input_processors: list[Processor] = [
            HighlightIncrementalSearchProcessor(),
            ImageLabelProcessor(lambda: self.input_images),
            BeforeInput(self.status_fragments),
            CallbackPlaceholder(self.placeholder_text),
        ]
        self.input_window = Window(
            BufferControl(
                buffer=self.input_buffer,
                input_processors=input_processors,
                search_buffer_control=self.search_toolbar.control,
                preview_search=True,
            ),
            height=Dimension(min=1),
            dont_extend_height=True,
            wrap_lines=True,
            style=UiPrinter.user_log_style(),
        )
        # 补全菜单最多 12 行,只在有候选且输入未完成时出现
        completion_space = ConditionalContainer(Window(height=12, dont_extend_height=True), filter=has_completions & ~is_done)
        input_error = ConditionalContainer(
            Window(FormattedTextControl(self.input_error_fragments), dont_extend_height=True, wrap_lines=True),
            filter=Condition(lambda: bool(self.input_error_fragments())),
        )
        self.activity_window = Window(FormattedTextControl(self.activity_fragments_fn), dont_extend_height=True, wrap_lines=True)
        running = Condition(lambda: self.input_mode == "running")
        activity = ConditionalContainer(
            self.activity_window,
            filter=running,
        )
        running_gap_above = ConditionalContainer(
            Window(height=1, dont_extend_height=True),
            filter=running,
        )
        running_gap_below = ConditionalContainer(
            Window(height=1, dont_extend_height=True),
            filter=running,
        )
        self.modal_window = Window(FormattedTextControl(self.modal_fragments, focusable=True), wrap_lines=False, dont_extend_height=True)
        modal_active = Condition(lambda: self.modal is not None)  # 普通模态占内容区
        exclusive_active = Condition(lambda: self.modal is not None and self.modal.exclusive)  # 独占模态占整个屏幕
        idle = Condition(lambda: self.input_mode == "chat")
        has_quick_hints = idle & Condition(lambda: bool(self.quick_hints()))
        quick_hints_gap = ConditionalContainer(Window(height=1, dont_extend_height=True), filter=has_quick_hints)
        quick_hints_row = ConditionalContainer(
            Window(FormattedTextControl(self.quick_hint_fragments), wrap_lines=True, dont_extend_height=True),
            filter=has_quick_hints,
        )
        normal_region = ConditionalContainer(
            HSplit(
                [
                    running_gap_above,
                    activity,
                    running_gap_below,
                    input_error,
                    self.input_window,
                    quick_hints_gap,
                    quick_hints_row,
                    completion_space,
                    self.search_toolbar,
                    Window(height=1, dont_extend_height=True),
                ]
            ),
            filter=~modal_active,
        )
        modal_region = ConditionalContainer(
            HSplit([self.modal_window, Window(height=1, dont_extend_height=True)]),
            filter=modal_active & ~exclusive_active,
        )
        self.status_window = self._status_bar_window(dont_extend_height=True)
        # 空闲提示符上方保留与先前输出的一行间距,但临时的 running/approval 区域从第 0 行
        # 开始。否则 patch_stdout 挂起并重绘应用时,可能把行首空行夹在工具审批标题与
        # 最终结果之间,一并提交进 scrollback。
        content = HSplit(
            [
                ConditionalContainer(Window(height=1, dont_extend_height=True), filter=idle),
                modal_region,
                normal_region,
                self.status_window,
            ]
        )
        self.exclusive_modal_window = Window(FormattedTextControl(self.modal_fragments, focusable=True), wrap_lines=False)
        exclusive_status = self._status_bar_window(dont_extend_height=False)
        root = FloatContainer(
            HSplit(
                [
                    ConditionalContainer(content, filter=~exclusive_active),
                    ConditionalContainer(HSplit([self.exclusive_modal_window, exclusive_status]), filter=exclusive_active),
                ]
            ),
            # 补全浮动菜单锚定输入窗口的光标位置,跟随光标移动
            [Float(CompletionsMenu(max_height=12, scroll_offset=1), xcursor=True, ycursor=True, attach_to_window=self.input_window, transparent=True)],
        )
        return Layout(root, focused_element=self.input_window)

    def make_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        modal = Condition(lambda: self.modal is not None)  # 模态激活时,常规按键都改由模态消费
        running = Condition(lambda: self.input_mode == "running" and self.modal is None)

        # 模态键表内的键在模态激活时 eager 接管;lambda 用默认参数绑定 key,避免闭包晚绑定
        for key in self.MODAL_KEYS:
            bindings.add(key, filter=modal, eager=True)(lambda event, key=key: self.dispatch_modal_key(key, event.data))
        for number in range(1, 10):
            # 数字键同样 eager 接管,供选择器的序号跳转
            bindings.add(str(number), filter=modal, eager=True)(lambda event, number=number: self.dispatch_modal_key(str(number), event.data))
        # 未列出的键也转发给模态(字母/符号输入),通过 event.data 传原字符
        bindings.add(Keys.Any, filter=modal)(lambda event: self.dispatch_modal_key("any", event.data))

        bindings.add("enter", filter=~modal, eager=True)(lambda event: event.current_buffer.validate_and_handle())  # 回车统一走 accept_handler
        bindings.add("escape", "enter", filter=~modal, eager=True)(lambda event: event.current_buffer.insert_text("\n"))  # Esc+Enter 插入字面换行(多行输入)
        for key, reverse in (("tab", False), ("s-tab", True)):
            bindings.add(key, filter=~modal)(lambda event, reverse=reverse: self.tab_or_complete(event.current_buffer, reverse=reverse))

        def paste(event):
            event.current_buffer.insert_text(event.data.replace("\r\n", "\n").replace("\r", "\n"))  # 粘贴的 CRLF/CR 统一转 LF,防布局错乱
            if self.input_mode in {"chat", "running"}:
                self._recognize_input()  # 粘贴可能带图片标记,重新识别

        bindings.add(Keys.BracketedPaste, filter=~modal)(paste)

        def history_search(event):
            direction = pt_search.SearchDirection.BACKWARD  # Ctrl-R 是 readline 的反向历史搜索
            if event.app.layout.current_control is self.search_toolbar.control:
                pt_search.do_incremental_search(direction, count=event.arg)  # 已在搜索框:继续增量搜索
            else:
                pt_search.start_search(direction=direction)  # 否则新开一次搜索

        bindings.add("c-r", filter=~modal, eager=True)(history_search)
        bindings.add("c-o", filter=~modal, eager=True)(lambda _: self.on_expand_output())  # Ctrl-O:展开上次输出

        # Ctrl-P 与 Up 在这里等价:readline 视二者为同义词;turn 运行期间两者都召回
        # 最新的排队追问(有草稿时上移光标,无草稿时走历史导航)。
        def recall(event):
            if self.input_buffer.text:
                self.input_buffer.cursor_up()  # 已有草稿:先移动光标,不清空用户内容
                return
            text = self.on_recall()  # 从队列取最新追问
            if text:
                self._reset_input(text, cursor_position=len(text))
            else:
                event.current_buffer.auto_up(count=event.arg)  # 队列为空:回退标准历史导航

        bindings.add("c-p", filter=running, eager=True)(recall)
        bindings.add("up", filter=running, eager=True)(recall)

        # Ctrl-X Ctrl-E(readline 的 `edit-and-execute-command`)与 Ctrl-G 把当前输入交给
        # $VISUAL/$EDITOR(缺省 vim)编辑,与 Claude Code 的编辑器绑定保持一致。`c-x c-e`
        # 是组合键:单独的 Ctrl-X 会等待第二个键,而不是立即触发。
        # 运行中重发没有专属按键;它对应 running 输入框里输入的 `/resend` 命令。
        edits_input = Condition(lambda: self.input_mode in {"chat", "running", "approval"})

        def edit_in_editor(_):  # pragma: no cover — interactive path
            self.edit_input_in_editor()

        bindings.add("c-g", filter=~modal & edits_input)(edit_in_editor)
        bindings.add("c-x", "c-e", filter=~modal & edits_input)(edit_in_editor)

        def ctrl_c(event):  # pragma: no cover — interactive path
            # Ctrl-C 永不退出应用。具体行为:
            #   * approval 模式 → 取消当前这次提问(给 agent 回空答复)。
            #   * 空闲 chat → 静默清空当前输入。
            #   * agent 运行中 → 有草稿时丢弃草稿;输入为空时中断本轮。
            # 退出仍保留给:空 chat 输入下的 Ctrl-D,或 /exit 斜杠命令。
            if self.modal is not None:
                # 模态优先:把 Ctrl-C 交给模态自己的处理器(通常是"取消/关闭")
                result = self.modal.key_fn("c-c", event.data)
                self.close_modal(None if result is TUI_MODAL_PENDING else result)
                return
            if self.input_mode == "approval" and self._input_pending is not None:
                self._input_result = ""  # 取消审批:回空串,让 agent 线程继续
                self._input_pending.set()
                return
            if self.input_mode == "chat":
                if self.input_buffer.text:
                    self.input_buffer.reset(Document(""))  # 空闲:清空当前输入
                return
            if self.input_mode in {"dispatch", "running"}:
                # 草稿会吸收第一次按键,与空闲提示符处的行为一致。队列提示只在缓冲区为空时
                # 渲染,所以"Ctrl-C 中断"提示恰好在下一次按键真正中断时显示。
                if self.input_buffer.text:
                    self.input_buffer.reset(Document(""))
                    return
                self.on_interrupt()

        bindings.add("<sigint>", eager=True)(ctrl_c)
        bindings.add("c-c", eager=True)(ctrl_c)

        def clear_input(_):  # pragma: no cover - 交互路径
            # readline 里丢弃整行的惯例,也是这里各编辑器语义一致的按键。Ctrl-C 也会清空,
            # 但 agent 运行时它会消耗一次本可用于中断的按键;这个键永远不会与停止轮次竞争。
            self.input_buffer.reset(Document(""))

        bindings.add("c-u", filter=~modal & edits_input, eager=True)(clear_input)

        def ctrl_d(event):  # pragma: no cover - 交互路径
            if self.input_mode == "approval" and self._input_pending is not None:
                self._input_result = self.input_buffer.text  # 审批模式:当前文本当作答案提交
                self._input_pending.set()
            elif self.input_buffer.text and self.input_mode in {"chat", "running"}:
                self.input_buffer.delete()  # 有文本:删一个字符,避免误退
            elif self.input_mode == "chat":
                self.on_exit_request()  # 空输入 + chat:这才是退出条件
                event.app.exit()

        bindings.add("c-d", filter=~modal, eager=True)(ctrl_d)

        def force_exit(event):  # pragma: no cover - 交互紧急路径
            # 紧急退出:无视一切过滤与状态直接退出应用(备用逃生门)
            self.on_force_exit()
            event.app.exit()

        bindings.add(Keys.ControlBackslash, eager=True)(force_exit)

        return bindings

    @staticmethod
    def editor_command() -> list[str]:
        """Ctrl-X Ctrl-E / Ctrl-G 启动的编辑器:$VISUAL,其次 $EDITOR,最后 vim。"""
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vim"
        return shlex.split(editor)  # shlex 拆分以支持带参数的编辑器命令(如 "code -w")

    # 剪刀线标记(git 的 "scissors" 惯例):分隔可编辑草稿与 Ctrl-X Ctrl-E 附加在下方
    # 的只读参考上下文。从这一行往下的内容在消息发送前都会被剥离。
    EDITOR_CONTEXT_MARKER = "# ------------------------ >8 ------------------------"

    @classmethod
    def _compose_editor_text(cls, draft: str, context: str) -> tuple[str, str]:
        """交给外部编辑器的文本:先草稿,上下文可用时再附剪刀线与 agent 最近回复作参考
        (全屏编辑器会遮住回复打印所在的 scrollback)。返回组合文本与分隔草稿和参考
        上下文的唯一标记(未附加上下文时为空),这样后续剥离只删除本次调用添加的
        上下文,绝不会误删用户自己输入的剪刀线。"""
        context = context.strip()
        if not context:
            return draft, ""  # 空白上下文视为无参考材料,不附加剪刀线
        marker = f"{cls.EDITOR_CONTEXT_MARKER} ({uuid.uuid4().hex[:12]})"  # 唯一标记:同一编辑会话多次进入编辑器也不混淆
        composed = (
            draft
            + "\n\n"
            + marker
            + "\n"
            + "# Reference only: everything below the scissors line is stripped before your\n"
            + "# message is sent. The agent's most recent reply follows for reference.\n"
            + "\n"
            + context
        )
        return composed, marker

    @classmethod
    def _strip_editor_context(cls, text: str, marker: str) -> str:
        """删除本次组合添加的参考上下文(唯一的剪刀线及其下方全部内容)。未附加标记时
        无可剥离内容,用户自己输入的剪刀线会原样保留。"""
        if marker:
            text = text.split(marker, 1)[0]  # 只切到标记第一次出现处
        return text.rstrip("\n")  # 去尾部空行,避免多余换行进入输入框

    def _edit_text_in_editor(self, text: str) -> str | None:
        """通过临时文件在编辑器中编辑 `text` 并返回编辑后内容;编辑器无法启动或非零退出
        时返回 None。在事件循环之外、run_in_terminal 内部执行。"""
        fd, path = tempfile.mkstemp(prefix="yucode-input-", suffix=".md")  # .md 后缀让编辑器启用 markdown 高亮
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            try:
                completed = subprocess.run([*self.editor_command(), path], check=False)
            except OSError:
                return None  # 启动失败(命令不存在等)按"放弃编辑"处理
            if completed.returncode != 0:
                return None  # 编辑器报错退出:丢弃本次编辑,不覆盖原输入
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        finally:
            try:
                os.unlink(path)  # 无论成败都清理临时文件
            except OSError:
                pass  # 删除失败(如编辑器占用)可忽略

    async def _run_input_editor(self) -> None:
        # `run_in_terminal` 会挂起应用并在结束后恢复(prompt_toolkit 自己的编辑器支持
        # 也用它),全屏编辑器因此获得干净的终端。非零退出或启动失败时输入保持原样。
        # 编辑器还会收到剪刀线下方附带的 agent 最近回复作参考(全屏编辑器遮住了
        # scrollback);返回时这段上下文会被剥离。
        original = UserInput(self.input_buffer.text, self.input_images).original_text()  # 含图片标记的原文进出编辑器
        composed, marker = self._compose_editor_text(original, self.editor_context_fn())
        edited = await run_in_terminal(lambda: self._edit_text_in_editor(composed), in_executor=True)  # in_executor:子进程放线程池,不卡事件循环
        if edited is None:
            return  # 编辑失败/取消:原输入原样保留
        edited = self._strip_editor_context(edited, marker)
        if edited != original:  # 内容没变时避免无谓的 reset 与识别重跑
            self._reset_input(edited, cursor_position=len(edited))
            if self.input_mode in {"chat", "running"}:
                self._recognize_input()  # 编辑回写同样需要重新识别图片
            self.invalidate()

    def edit_input_in_editor(self) -> None:
        """Ctrl-X Ctrl-E / Ctrl-G:用外部编辑器编辑当前输入,再把结果加载回来。"""
        if self.app is not None:
            self.app.create_background_task(self._run_input_editor())  # 应用未启动时无事件循环可调度,直接跳过

    async def animate(self) -> None:
        """运行区域在屏幕上时,按动画帧率触发重绘。

        prompt-toolkit 在启动自身刷新任务时就固定了 `refresh_interval`,无法只对运行中的
        轮次提高帧率。这个第二计时器专管动画模式,分割线一消失就停止请求帧,
        让空闲屏幕保持低刷新。"""
        while True:  # 应用退出时本任务随后台任务一并取消,无需显式退出条件
            await asyncio.sleep(self.ANIMATION_INTERVAL)
            if self.input_mode == "running":
                self.invalidate()

    def run(self, style: Style | None = None) -> None:  # pragma: no cover — interactive
        app = Application(
            layout=self.build_layout(),
            key_bindings=self.make_bindings(),
            full_screen=False,
            mouse_support=False,
            refresh_interval=self.IDLE_REFRESH_INTERVAL,
            style=style,
            erase_when_done=True,
        )
        # 常驻主屏幕的渲染器在终端尺寸变化后需要 CPR(光标位置报告);否则过期的光标坐标
        # 可能把临时 footer 留在 tmux 的 scrollback 里。对不响应探测的终端保留静默降级
        # 的旧行为。
        app.renderer.cpr_not_supported_callback = lambda: None
        self.app = app
        self.ready.clear()

        def start() -> None:
            # pre_run 已在应用循环内执行;它启动的任务会随应用退出时其余后台任务一起取消
            app.create_background_task(self.animate())
            self.ready.set()

        try:
            with patch_stdout():  # 把 print 重定向进 TUI 输出通道,防并发打印破坏渲染
                app.run(pre_run=start)
        finally:
            self.ready.set()
            self.app = None
            # 若 agent 线程仍阻塞在 request_input,退出时置位解锁,让它的栈帧正常收尾
            # 而不是泄漏一个线程。
            if self._input_pending is not None:
                self._input_result = ""
                self._input_pending.set()
            if self.modal is not None:
                self.close_modal(None)  # 模态未关闭也一并清理,防 TUI 线程悬挂在 done.wait()


@dataclass
class TabbedViewState:
    """标签页视图的纯状态:当前标签、滚动位置与可视窗口计算。"""

    titles: tuple[str, ...]
    tab: int = 0
    scroll: int = 0

    def switch(self, delta: int) -> None:
        self.tab = (self.tab + delta) % len(self.titles)  # 模运算实现标签循环切换
        self.scroll = 0  # 切标签回到顶部,防残留上一标签的滚动

    def scroll_by(self, delta: int) -> None:
        self.scroll = max(0, self.scroll + delta)  # 不允许滚出顶部

    def visible(self, lines: list[ViewLine], height: int) -> list[ViewLine]:
        # 内容刷新后行数变化,把滚动钳制到有效范围;height 超过行数时为 0
        self.scroll = min(self.scroll, max(0, len(lines) - height))
        return lines[self.scroll : self.scroll + height]


@dataclass
class DiffViewState:
    """`/diff` 查看器的按键处理与视图状态;`handle_key` 返回关闭结果、REFRESH 哨兵或
    TUI_MODAL_PENDING(继续等待按键)。"""

    REFRESH: ClassVar[object] = object()  # 请求"重新渲染"的哨兵(r 键触发)

    class Mode(Enum):
        LIST = auto()
        FILE = auto()

    view: TabbedViewState
    mode: Mode = Mode.LIST
    file: int = 0

    def reset(self) -> None:
        self.mode = self.Mode.LIST
        self.file = 0
        self.view.scroll = 0

    def switch_tab(self, delta: int) -> None:
        self.view.switch(delta)
        self.reset()  # 换标签时回到列表视图与顶部

    def move_file(self, delta: int, count: int) -> None:
        if count:  # count 为 0(无文件)时保持原样
            self.file = (self.file + delta) % count  # 循环移动

    def clamp_file(self, count: int) -> None:
        self.file = self.file % count if count else 0  # 文件数变化后钳制;无文件归零

    def open_file(self, count: int) -> None:
        if self.mode is self.Mode.LIST and count:
            self.mode = self.Mode.FILE  # 只有列表模式且有文件才进入文件视图
            self.view.scroll = 0

    def close_file(self) -> None:
        if self.mode is self.Mode.FILE:
            self.mode = self.Mode.LIST  # 文件视图退回列表
            self.view.scroll = 0

    def handle_key(self, key: str, file_count: int, viewport: int) -> Any:
        if key in {"q", "c-c"}:
            return None  # q / Ctrl-C:关闭查看器,None = 关闭且无返回值
        if key == "escape":
            if self.mode is self.Mode.LIST:
                return None  # 列表视图按 Esc:整个关闭
            self.close_file()  # 文件视图按 Esc:先退回列表
        elif key in {"down", "j", "up", "k"}:
            delta = 1 if key in {"down", "j"} else -1
            if self.mode is self.Mode.LIST and file_count:
                self.move_file(delta, file_count)  # 列表模式:在文件间移动
            elif self.mode is self.Mode.FILE:
                self.view.scroll_by(delta)  # 文件模式:滚动内容
        elif key in {"h", "l", "tab"}:
            self.switch_tab(1 if key in {"l", "tab"} else -1)  # h/l/Tab:标签间切换
        elif key == "right" and self.mode is self.Mode.LIST:
            self.switch_tab(1)
        elif key == "left":
            if self.mode is self.Mode.FILE:
                self.close_file()  # 文件模式 left 先退回列表
            else:
                self.switch_tab(-1)
        elif key == "enter" and self.mode is self.Mode.LIST and file_count:
            self.open_file(file_count)  # 有文件时 Enter 打开当前文件
        elif self.mode is self.Mode.FILE and key in {"pagedown", "pageup", "c-d", "c-u"}:
            # 翻页按整页滚动,半页滚动减半;至少 1 行防死循环
            distance = max(1, viewport if key in {"pagedown", "pageup"} else viewport // 2)
            self.view.scroll_by(distance if key in {"pagedown", "c-d"} else -distance)
        elif key in {"g", "G"}:  # less 风格:g 到顶部,G 到底部
            if self.mode is self.Mode.LIST and file_count:
                self.file = 0 if key == "g" else file_count - 1
            elif self.mode is self.Mode.FILE:
                self.view.scroll = 0 if key == "g" else 10**9  # 底部用极大值占位,渲染时钳制到最后一页(见 visible)
        elif key == "r":
            self.reset()
            return self.REFRESH  # 请求重新渲染(重新生成 diff 内容)
        return TUI_MODAL_PENDING  # 未关闭:模态继续等待后续按键


@dataclass
class ChoiceViewState:
    """选择器(provider/MCP 等)的视图状态:搜索过滤、选中项与按键处理。"""

    FREE_TEXT: ClassVar[str] = "\x00free_text"  # 特殊选项:选中它表示让用户自由输入(0x00 不会出现在真实选项里)

    choices: tuple[str, ...]
    labels: dict[str, str]
    disabled: set[str]
    query: str = ""
    selected: int = 0
    searching: bool = False

    def visible(self) -> tuple[str, ...]:
        if not self.query:
            return self.choices  # 未搜索时直接显示全部,零开销
        needle = self.query.lower()  # 大小写不敏感的过滤
        visible: list[str] = []
        header = ""
        section: list[str] = []
        for choice in self.choices:
            if choice in self.disabled:
                # 禁用项变成分组标题:其后匹配的可用项归入该组
                if section:
                    visible.extend(([header] if header else []) + section)
                header, section = choice, []
            elif needle in (choice + " " + self.labels.get(choice, choice)).lower():
                # 同时匹配选项名与显示标签(如 "web_search 网页搜索")
                section.append(choice)
        if section:  # 末尾残留的组也要输出
            visible.extend(([header] if header else []) + section)
        return tuple(visible)

    def enabled(self) -> tuple[str, ...]:
        return tuple(choice for choice in self.visible() if choice not in self.disabled)

    def clamp(self, options: tuple[str, ...] | None = None) -> tuple[str, ...]:
        options = options if options is not None else self.enabled()
        # 把选中下标钳制到 [0, len-1];无选项时归零
        self.selected = min(max(self.selected, 0), len(options) - 1) if options else 0
        return options

    def move(self, delta: int) -> None:
        options = self.enabled()
        if options:  # 空列表时不动,避免下标运算出错
            self.selected = min(max(self.selected + delta, 0), len(options) - 1)

    def set_query(self, query: str) -> None:
        self.query = query
        self.selected = 0  # 过滤条件一变就回到第一项,符合搜索直觉

    def selected_choice(self) -> str | None:
        options = self.clamp()
        return options[self.selected] if options else None

    def fragments(self, title: str, preview_fn: Callable[[str], str] | None = None) -> StyleAndTextTuples:
        visible = self.visible()
        options = self.clamp()
        suffix = (" /" + self.query) if self.query else ""  # 标题行尾追加当前搜索词
        if self.query and not self.searching:
            suffix += " (filtered)"  # 搜索已提交但未在输入中,标注"已过滤"
        parts: StyleAndTextTuples = [
            ("class:choice.title", title + suffix + "\n"),
            ("class:choice.disabled", "  j/k move, / search, Esc/q back/cancel\n"),
        ]
        if self.query and not options:
            return [*parts, ("class:choice.disabled", "  no matches\n")]  # 过滤后无匹配:提示而非空白列表
        number = 0
        for choice in visible:
            label = self.labels.get(choice, choice)
            if choice in self.disabled:
                parts.append(("class:choice.disabled", "  " + label + "\n"))  # 禁用项仅作分组标题
                continue
            number += 1
            selected = number - 1 == self.selected  # 显示序号(从 1 起)与下标(从 0 起)的换算
            if selected:
                parts.append(("[SetCursorPosition]", ""))  # 光标移到选中行,供依赖光标的功能使用
            style = "class:choice.selected" if selected else ""
            prefix = ("> " if selected else "  ") + f"{number:2d}. "
            if match := UiPrinter.MCP_STATUS_RE.search(label):
                # 标签带 MCP 状态点(● connected 等)时给圆点单独配色
                parts.append((style, prefix + label[: match.start()]))
                marker_style = (style + " class:choice.status." + match.group(1)).strip()
                parts.append((marker_style, "●"))
                parts.append((style, label[match.start() + 1 :] + "\n"))
            else:
                parts.append((style, prefix + label + "\n"))
        if preview_fn and options:
            preview = preview_fn(options[self.selected]).replace("\\n", "\n")  # 预览中的字面 \n 转真实换行
            if preview:
                parts.append(("class:choice.disabled", "  ──────────────────────────────────\n"))
                parts.extend(("class:choice.preview", "  │ " + line + "\n") for line in preview.splitlines())
        if self.searching:
            parts.append(("", "/" + self.query))  # 搜索模式下底部回显查询串
        return parts

    def handle_key(self, key: str, data: str = "") -> Any:
        # 搜索模式下除控制键外全部进查询串,选择导航暂停
        if self.searching and key not in {"enter", "escape", "backspace", "c-h"}:
            text = data if key == "any" else key
            if len(text) == 1 and text not in "\r\n":  # 只接受单字符,换行/回车不进查询
                self.set_query(self.query + text)
        elif key in {"j", "down"} and not self.searching:
            self.move(1)
        elif key in {"k", "up"} and not self.searching:
            self.move(-1)
        elif key in {"g", "G"} and not self.searching:  # less 风格:g 跳到第一项,G 跳到最后一项
            self.move(-len(self.enabled()) if key == "g" else len(self.enabled()))
        elif key == "/":
            self.searching = True  # / 进入搜索模式
            self.set_query("")
        elif key in {"backspace", "c-h"} and self.searching:
            self.set_query(self.query[:-1])  # 退格逐字删除查询
        elif key == "escape":
            # Esc 逐级退出:先退搜索,再清查询,最后关闭选择器
            if self.searching:
                self.searching = False
            elif self.query:
                self.set_query("")
            else:
                return SELECTION_BACK
        elif key == "q" and not self.searching:
            return SELECTION_BACK  # 搜索模式下 q 是普通查询字符,不关闭
        elif key == "enter":
            if self.searching:
                self.searching = False  # 回车结束搜索,不清除查询词
            elif (choice := self.selected_choice()) is not None:
                return SELECTION_FREE_TEXT if choice == self.FREE_TEXT else choice  # 自由文本选项返回哨兵
        elif key == "c-c":
            return KeyboardInterrupt()  # 异常上抛,由调用方决定取消语义
        elif key.isdigit() and not self.searching:
            number = int(key)
            options = self.enabled()
            if 1 <= number <= len(options):
                self.selected = number - 1  # 数字键直接跳转序号(显示序号从 1 开始)
        return TUI_MODAL_PENDING  # 未关闭:继续等待按键
