"""yucode 命令循环与交互式会话运行时。"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, ClassVar

from prompt_toolkit import print_formatted_text
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from yucode.base import (
    DISMISSED,
    HTTP_USER_AGENT,
    IMAGE_INPUT_CHOICES,
    PROVIDER_API_CHOICES,
    REASONING_CHOICES,
    SELECTION_BACK,
    SELECTION_FREE_TEXT,
    ConfigError,
    Json,
    LogBlock,
    LogEdge,
    LogLine,
    LogRole,
    MalformedToolCallError,
    ProviderConfig,
    Text,
    ToolCall,
    ToolError,
    TurnBox,
    YucodeError,
    __version__,
)
from yucode.engine import Agent
from yucode.hints import Context as HintContext
from yucode.hints import HintPicker
from yucode.image import ImageInputs, UserInput
from yucode.memory import MemoryConsolidationOutcome
from yucode.model import ModelClient
from yucode.prompts import LIVE_FOLLOWUP_PREFIX, PREVIOUS_CONTEXT_TRIMMED, SYSTEM_PROMPT
from yucode.provider_compat import builtin_tools_issue
from yucode.render import BashLivePreview, StatusBar, Theme, UiPrinter, markdown_table, search_sources_footer
from yucode.runner import ToolDisplay
from yucode.session import QueuedInput, SessionEntry, SessionSnapshotCodec, SessionSnapshotStore, ToolResultRecord
from yucode.tools import TOOL_REGISTRY, AskSpec, CodeIndex
from yucode.tui import TUI_MODAL_PENDING, ChoiceViewState, DiffViewState, TabbedViewState, TuiApp
from yucode.update import UpdateChecker

SetHandler = tuple[str, str, Callable[[str], int | float | None] | None]
# fmt: off
# /set 处理器表:键 -> (目标对象, 属性名, 值转换函数)。转换函数同时负责边界约束
# (如 max_steps 至少为 1);转换抛 ConfigError/ValueError 时视为非法值。
SET_HANDLERS: dict[str, SetHandler] = {
    "provider.temperature": ("provider", "temperature", lambda v: None if v == "off" else float(v)),
    "provider.max_tokens": ("provider", "max_tokens", lambda v: max(0, int(v))),
    "provider.timeout": ("provider", "timeout", lambda v: max(1, int(v))),
    "provider.response_timeout": ("provider", "response_timeout", lambda v: max(0, int(v))),
    "provider.stream": ("provider", "stream", lambda v: v == "on"),
    "provider.image_input": ("provider", "image_input", None),
    "runtime.max_agent_steps": ("settings", "max_steps", lambda v: max(1, int(v))),
    "runtime.max_context_tokens": ("settings", "max_context_tokens", lambda v: max(1, int(v))),
    "runtime.max_parallel_tools": ("settings", "max_parallel_tools", lambda v: max(1, int(v))),
    "runtime.shell_timeout": ("settings", "shell_timeout", lambda v: max(1, int(v))),
    "runtime.bash_wait_timeout": ("settings", "bash_wait_timeout", lambda v: max(0, int(v))),
}
SET_KEYS = tuple(SET_HANDLERS)
# 值为封闭集合的键:/set 遇到未知值会拒绝,并整体作为补全候选项提供。
SET_CHOICES: dict[str, tuple[str, ...]] = {
    "provider.stream": ("on", "off"),
    "provider.image_input": IMAGE_INPUT_CHOICES,
}
SET_VALUES: dict[str, tuple[str, ...]] = {
    "provider.temperature": ("off",),
    **SET_CHOICES,
}
# fmt: on


class CommandCompleter(Completer):
    MCP_MENTION_RE: ClassVar[re.Pattern] = re.compile(r"@([A-Za-z0-9_.-]*)$")  # 行尾 @server[.tool] 提及
    SKILL_MENTION_RE: ClassVar[re.Pattern] = re.compile(r"(?<![A-Za-z0-9_])\$([A-Za-z0-9_-]*)$")  # 行尾 $skill 提及(前面不能是标识符字符)

    def __init__(
        self,
        providers: Callable[[], tuple[str, ...]] = tuple,
        models: Callable[[], tuple[str, ...]] = tuple,
        mcp_servers: Callable[[], tuple[str, ...]] = tuple,
        mcp_connected_servers: Callable[[], tuple[str, ...]] = tuple,
        mcp_tools: Callable[[str], tuple[str, ...]] = lambda _server: (),
        skills: Callable[[], tuple[str, ...]] = tuple,
    ):
        self.providers = providers
        self.models = models
        self.mcp_servers = mcp_servers
        self.mcp_connected_servers = mcp_connected_servers
        self.mcp_tools = mcp_tools
        self.skills = skills

    def get_completions(self, document, complete_event):
        del complete_event
        text = document.text_before_cursor
        if text.startswith("/set "):
            tail = text[len("/set ") :]
            if " " not in tail:
                yield from self.matches(SET_KEYS, tail)  # 还没输值:补全键名
                return
            key, _, value = tail.partition(" ")
            yield from self.matches(SET_VALUES.get(key, ()), value)  # 已输值:补全该键的合法取值
            return
        for command, values in (
            ("/model ", self.models),
            ("/provider ", self.providers),
            ("/reason ", lambda: REASONING_CHOICES),
            ("/effort ", lambda: REASONING_CHOICES),
            ("/api ", lambda: PROVIDER_API_CHOICES),
            ("/strict ", lambda: ("on", "off")),
        ):
            if text.startswith(command):
                yield from self.matches(values(), text[len(command) :])
                return
        if text.startswith("/mcp "):
            tail = text[len("/mcp ") :]
            if " " not in tail:
                yield from self.matches(("connect", "disconnect", "tools"), tail)
                return
            sub, _, value = tail.partition(" ")
            if sub == "connect":
                completed, _, prefix = value.rpartition(" ")  # 已选过的服务器不能再选:按空格拆分去重
                selected = set(completed.split())
                yield from self.matches((name for name in self.mcp_servers() if name not in selected), prefix)
                return
            if sub == "disconnect":
                yield from self.matches(self.mcp_servers(), value)
                return
            if sub == "tools":
                yield from self.matches(self.mcp_connected_servers(), value)
                return

        at_match = CommandCompleter.MCP_MENTION_RE.search(text)
        if at_match:
            server_part, dot, tool_part = at_match.group(1).partition(".")
            if dot:
                yield from self.matches(self.mcp_tools(server_part), tool_part)  # @server. 后补工具名
            else:
                yield from self.matches(self.mcp_servers(), server_part)  # 裸 @ 补服务器名
            return

        skill_match = CommandCompleter.SKILL_MENTION_RE.search(text)
        if skill_match:
            yield from self.matches(self.skills(), skill_match.group(1))
            return

        if text.startswith("/") and " " not in text:
            yield from self.matches(CommandLoop.COMMANDS, text)  # 斜杠命令补全

    @staticmethod
    def matches(values, prefix: str):
        return (Completion(value, start_position=-len(prefix)) for value in values if value.startswith(prefix))


class CommandLoop:
    """拥有会话行为:读取输入、分发命令、驱动回合、路由输出。

    斜杠命令在这里处理,永远不会到达模型。agent 在本线程运行,而 prompt-toolkit 在另一
    个线程,这正是输出有两个去向的原因:已完成的 user/assistant/工具输出进入原生
    scrollback,而草稿、预览、队列状态和选择器属于 TUI。终端留在 scrollback 里的任何
    临时内容都只是痕迹,不是历史——转录始终由语义记录重建。

    回合进行中输入会被排队,只有白名单里的只读命令允许在忙碌的 session 上运行;任何
    会改动配置的命令都会改变已在进行中的回合的含义。

    同一个对象也服务于非交互路径:那里没有 TUI,输入输出只是普通 callable——测试也是
    这样驱动它的。"""

    HUNK_HEADER_RE: ClassVar[re.Pattern] = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")  # diff hunk 头:捕获旧/新行数
    HELP_HEADING_RE: ClassVar[re.Pattern] = re.compile(r"^### (.+)$", re.MULTILINE)  # 帮助文本的 ### 标题
    HELP_ENTRY_RE: ClassVar[re.Pattern] = re.compile(r"^- (.+?) — ", re.MULTILINE)  # 帮助文本的命令条目
    QUEUE_EMPTY_HINT = "Enter queues follow-up · Ctrl-C interrupts"
    QUEUE_PENDING_HINT = "↑ recalls queued · Ctrl-C interrupts"
    TRANSCRIPT_DIFF_LINES: ClassVar[int] = 40  # 回放时每个 Edit diff 预览的最大行数
    EDITOR_CONTEXT_MAX_LINES: ClassVar[int] = 200  # 外部编辑器上下文的最大行数
    INPUT_HISTORY_BYTES: ClassVar[int] = 512 * 1024  # 输入历史文件大小上限(512KB)
    # fmt: off
    # 斜杠命令 -> 处理方法名;"/exit"、"/quit" 不在此表,由 command() 单独处理。
    COMMAND_HANDLERS: ClassVar[dict[str, str]] = {
        "/help": "help", "/status": "status", "/ps": "ps_command", "/diff": "diff_command",
        "/skills": "skills_command", "/config": "config",
        "/compact": "compact", "/index": "index", "/provider": "provider", "/model": "model",
        "/reason": "reason", "/effort": "reason", "/api": "api", "/set": "set_value", "/yolo": "yolo", "/strict": "strict", "/hints": "hints",
        "/mcp": "mcp_command", "/resend": "resend_command", "/name": "name_command", "/sessions": "sessions_command", "/resume": "sessions_command",
    }
    COMMANDS: ClassVar[tuple[str, ...]] = tuple(COMMAND_HANDLERS) + ("/exit", "/quit")
    # fmt: on

    # agent 工作时允许从跟进输入框运行的命令:只读视图外加 /yolo——
    # /yolo 只是翻转一个原子标志,agent 在下一次审批时读取即可,不会改变在飞回合。
    QUEUE_RUN_COMMANDS: ClassVar[frozenset[str]] = frozenset({"/help", "/status", "/skills", "/ps", "/mcp", "/diff", "/yolo", "/hints", "/resend"})
    MODEL_CONFIGURED_LABEL = "---- Configured models ----"
    MODEL_DISCOVERED_LABEL = "---- Discovered models ----"
    MODEL_LABELS = frozenset((MODEL_CONFIGURED_LABEL, MODEL_DISCOVERED_LABEL))
    # /mcp 子命令 -> (最少参数个数, 最多参数个数, 用法说明)。
    MCP_COMMANDS: ClassVar[dict[str, tuple[int, int, str]]] = {
        "connect": (1, sys.maxsize, "Usage: /mcp connect <server> [server ...]"),
        "disconnect": (1, 1, "Usage: /mcp disconnect <server>"),
        "tools": (0, 1, "Usage: /mcp tools [server]"),
    }
    MCP_HELP = "Try /mcp, /mcp connect <server> [server ...], /mcp disconnect <server>, or /mcp tools [server]"

    HELP = """### Commands

- `/help` — Show this help.
- `/status` — Show runtime status.
- `/ps` — Show active background jobs.
- `/diff` — Show latest edits and overall session diff.
- `/skills` — List installed skills (load with `Skill(name)` or reference inline with `$name`).
- `/config` — Show active config.
- `/compact` — Compact context now.
- `/name [TEXT]` — Name this session for later, or show the current name.
- `/sessions [all]` — Browse saved sessions and re-enter one (alias: `/resume`; `all` widens
  past this project).
- `/resend` — Resend the in-flight model request (type it while a turn is working).
- `/index [force]` — Sync or rebuild code symbol index.
- `/provider [NAME]` — Select or show the active provider.
- `/model [MODEL]` — Select or set the active model.
- `/reason [EFFORT]` — Select or set reasoning effort (alias: `/effort`).
- `/api [API]` — Select or set the request protocol used to reach the model.
- `/set KEY VALUE` — Set `provider.*` and `runtime.*`.
- `/yolo` — Toggle tool confirmations.
- `/hints` — Toggle next-step quick hints.
- `/strict` — Toggle strict tool-call schemas (OpenAI / DeepSeek).
- `/mcp` — Manage MCP server connections.
- `/exit`, `/quit` — Exit.

### Mentions

- `@server[.tool]` — Point the agent at an MCP server/tool in your message (tab-completes).
- `$skill` — Reference a skill in your message to load its instructions for that turn (tab-completes).

### CLI

- `-c`, `--last`, `--latest` — Resume the latest session in the current project.
- `--resume [UID]` — Resume a saved session by uid, name, or uid prefix; defaults to latest
  (`last` also works).

### Tools

Read, ViewImage, InspectCode, Search, Edit, Bash, Job, Recall, Note, Ask, MCP, Skill.

`Skill(name)` loads a skill's full instructions on demand (see the SKILLS section / `$skill`).
"""

    DIFF_MAX_BYTES: ClassVar[int] = 50_000
    DIFF_MAX_LINES: ClassVar[int] = 1_200

    @classmethod
    def bounded_diff(cls, text: str) -> tuple[str, bool]:
        # 大 diff 会撑爆 /diff 输出:按字节数和行数双重封顶,返回 (截断文本, 是否被截断)。
        if len(text.encode("utf-8")) <= cls.DIFF_MAX_BYTES and text.count("\n") <= cls.DIFF_MAX_LINES:
            return text, False
        clipped: list[str] = []
        length = 0
        for line in text.splitlines():
            line_bytes = len(line.encode("utf-8")) + 1  # +1 是换行符的字节
            if length + line_bytes > cls.DIFF_MAX_BYTES or len(clipped) >= cls.DIFF_MAX_LINES:
                break  # 任一上限达到即停:不切半行,保证截断处是完整行
            clipped.append(line)
            length += line_bytes
        return "\n".join(clipped), True

    @staticmethod
    def diff_counts(text: str) -> tuple[int, int]:
        # 统计 diff 里的增删行数:hunk 头给出各块的配额,只统计配额内的 +/- 行;
        # 上下文行(" ")会消耗配额,避免把相邻 hunk 的计数弄混。
        added = removed = 0
        old_remaining = new_remaining = 0
        for line in text.splitlines():
            if match := CommandLoop.HUNK_HEADER_RE.match(line):
                old_remaining = int(match.group(1) or 1)
                new_remaining = int(match.group(2) or 1)
            elif line.startswith("+") and new_remaining:
                added += 1
                new_remaining -= 1
            elif line.startswith("-") and old_remaining:
                removed += 1
                old_remaining -= 1
            elif line.startswith(" "):
                old_remaining = max(0, old_remaining - 1)  # 上下文行:双边配额都减
                new_remaining = max(0, new_remaining - 1)
        return added, removed

    def __init__(self, agent: Agent, input_fn=input, output_fn=print):
        self.agent = agent
        self.session = agent.session
        self._hint_picker = HintPicker()  # 空闲占位提示的挑选器;见 yucode/hints.py
        self.input_fn = input_fn
        self.ui = UiPrinter(output_fn)
        self.status_bar = StatusBar(self.session)
        self.live_preview = BashLivePreview()
        self.model_stream_lock = threading.Lock()
        self.model_stream_kind = ""
        self.model_stream_text = ""
        self.model_stream_promoted_text = ""
        self.live_status_paused = False
        # 本次运行结束时要移交的 session uid。`main` 在 run() 返回后读取它,
        # 并围绕该 session 构建下一个 CommandLoop。
        self.resume_request = ""
        self.background_output_lock = threading.Lock()
        self.background_output_open = True
        self.interactive_input = input_fn is input and sys.stdin.isatty()  # 交互终端才走全 TUI;注入输入(如测试)走简单 REPL
        # 全 TUI 外壳激活期间由 run_tui() 设置;tool_input 经由它路由,让审批提示落在
        # 用户正在输入的同一个输入控件里。
        self.tui: TuiApp | None = None
        if self.interactive_input:
            history_path = self.session.data_path("history.txt")
            os.makedirs(os.path.dirname(history_path), exist_ok=True)
            self.trim_input_history(history_path)  # 启动时先裁剪历史文件,防其无限增长
            self.input_history = FileHistory(history_path)  # prompt_toolkit 的历史后端
        else:
            self.input_history = None  # 非交互路径没有历史记录
        self.input_completer = CommandCompleter(
            providers=lambda: tuple(sorted(self.session.config.providers)),
            models=lambda: self.session.config.provider.available_models,
            mcp_servers=lambda: tuple(config.name for config in self.session.mcp.parse_configs()) if self.session.mcp else (),
            mcp_connected_servers=lambda: (
                tuple(config.name for config in self.session.mcp.parse_configs() if self.session.mcp.connected(config.name)) if self.session.mcp else ()
            ),
            mcp_tools=lambda server: tuple(tool.name for tool in self.session.mcp.tools.get(server, [])) if self.session.mcp else (),
            skills=lambda: tuple(skill.name for skill in self.session.skills.all()) if self.session.skills else (),
        )
        # —— 把 agent 的各路输出/输入回调接到本循环的渲染路径上 ——
        self.agent.output_fn = self.agent_output  # 最终答案:去重后经 agent_output 输出
        self.agent.model.on_stream = self.model_stream_output  # 流式预览/提升
        self.agent.model.on_builtin_call = self.builtin_call_output  # provider 侧 builtin 调用日志
        self.agent.on_queue_flush = self.flush_queued_to_log  # 排队消息刷入回合时移进 scrollback
        self.agent.context.on_compaction = self.automatic_compaction_status  # 自动压缩阶段提示
        self.agent.tools.output_fn = self.tool_output  # 工具结果展示
        self.agent.tools.input_fn = self.tool_input  # 审批输入:全 TUI 下走 TuiApp 输入框
        self.agent.tools.live_start = self.tool_live_start  # Bash 实时预览启动
        self.agent.tools.live_output = self.tool_live_output  # Bash 实时输出
        self.agent.tools.question_fn = self.question_interaction  # Ask 提问

    def automatic_compaction_status(self, active: bool) -> None:
        """把自动上下文压缩(compaction)显示为当前回合的一个独立阶段。"""
        if self.tui is not None:
            self.tui.set_running("compacting context" if active else "working")  # 让 TUI 标题反映压缩阶段

    def organize_memory_after_turn(self) -> None:
        """Run due synchronous memory maintenance without changing the completed turn's result."""

        started = False

        def begin() -> None:
            nonlocal started
            started = True
            if self.tui is not None:
                self.tui.set_running("organizing memory")
            else:
                self.emit("Organizing project memory...")
                self.status_bar.start()

        try:
            outcome = self.agent.consolidate_memory(on_start=begin)
        except KeyboardInterrupt:
            outcome = MemoryConsolidationOutcome(started, error="cancelled")
        except Exception as error:  # noqa: BLE001 - maintenance must never replace an already completed answer
            outcome = MemoryConsolidationOutcome(started, error=Text.clean(str(error))[:500] or error.__class__.__name__)
        finally:
            if started:
                if self.tui is not None:
                    self.tui.set_dispatching()
                else:
                    self.status_bar.stop()
        if not outcome.attempted:
            return
        # The internal request contributes to provider usage. Persist it even when its JSON was
        # rejected, while the lock mtime remains unchanged so a later turn can retry.
        self.session.save_snapshot()
        if outcome.error == "cancelled":
            self.emit("Memory organization cancelled; the completed answer was kept.")
        elif outcome.error:
            self.emit("Memory organization failed; it will retry later: " + outcome.error)
        elif outcome.upserted or outcome.forgotten:
            self.emit(f"Memory organized: {outcome.upserted} updated, {outcome.forgotten} removed.")
        else:
            self.emit("Memory reviewed: no durable changes.")

    @classmethod
    def trim_input_history(cls, path: str) -> None:
        """限制输入历史文件的大小——prompt_toolkit 只会往里追加。

        保留能装进 `INPUT_HISTORY_BYTES` 的最新的条目,丢弃其余。裁剪总是落在条目头部
        而不是字节偏移上,所以幸存内容永远可加载:头部写成 "\n# <timestamp>\n",内容行
        以 "+" 开头,因此用户以 "#" 开头的行不会被误认成头部。替换是原子的,被打断的
        裁剪不会留下截断的历史文件;所有失败都被忽略——历史回忆只是便利功能,绝不能
        阻止会话启动。
        """
        try:
            if os.path.getsize(path) <= cls.INPUT_HISTORY_BYTES:
                return  # 未超限就不动文件
            with open(path, "rb") as file:
                file.seek(-cls.INPUT_HISTORY_BYTES, os.SEEK_END)  # 只读尾部预算内的字节
                tail = file.read()
            start = tail.find(b"\n# ")
            if start < 0:
                return  # 单条条目比预算还大:保留它,而不是从中间切开
            temp = path + ".tmp"
            with open(temp, "wb") as file:
                file.write(tail[start + 1 :])  # 从条目头部起写,确保开头是完整头部
            os.replace(temp, path)  # 原子替换:进程被杀也不会留下半截文件
        except OSError:
            return  # 历史裁剪失败不影响启动

    def flush_queued_to_log(self, texts: list[str]) -> None:
        # 把已刷入回合的排队消息从活动区移进终端 scrollback(常规日志输出)。
        texts = [text for text in texts if text.strip()]  # 纯空白消息无展示价值,过滤掉
        if not texts:
            return
        fragments: list[tuple[str, str]] = [("", "\n")]
        for index, text in enumerate(texts):
            if index:
                fragments.append(("", "\n"))
            fragments.extend([("class:prompt", UiPrinter.USER_LOG_PREFIX), (UiPrinter.user_log_style(), text), ("", "\n")])
        fragments.append(("", "\n"))
        print_formatted_text(FormattedText(fragments), style=self.style(), end="", flush=True)  # 一次输出整块,避免逐行闪烁

    # 模型请求在途时分隔线上显示的"呼吸"绿点。随着流事件到达,标签从 working 变为
    # thinking/responding;脉冲一直持续到请求完成。
    WAITING_PULSE_STYLES: ClassVar[tuple[str, ...]] = (
        "fg:#0a3d0a",
        "fg:#146114",
        "fg:#1f8a1f",
        "fg:#2dbf2d bold",
        "fg:#43e043 bold",
        "fg:#7bff7b bold",
    )
    WAITING_PULSE_PERIOD: ClassVar[float] = 1.6

    def waiting_pulse_fragments(self) -> StyleAndTextTuples:
        if self.session.state.current_model_call_started_at <= 0:
            return []  # 没有在途请求就不显示脉冲
        # 三角呼吸:0 → 1 → 0,周期为 WAITING_PULSE_PERIOD,映射到调色板。
        phase = (time.monotonic() % self.WAITING_PULSE_PERIOD) / self.WAITING_PULSE_PERIOD
        intensity = 1.0 - abs(2.0 * phase - 1.0)
        idx = min(len(self.WAITING_PULSE_STYLES) - 1, int(intensity * len(self.WAITING_PULSE_STYLES)))  # 亮度 -> 样式索引,封顶到最后一个
        return [(self.WAITING_PULSE_STYLES[idx], "● ")]

    # 每帧一格。若头部在两次重绘之间前进的距离超过它自己的光晕,就不再像运动,
    # 而像是虚线在零散位置闪烁。
    QUEUE_SWEEP_CELLS_PER_SEC: ClassVar[float] = 1.0 / TuiApp.ANIMATION_INTERVAL
    # 彗星效果:柔和的头部,尾巴按与头部的距离渐隐到暗色的分隔线里。渐变比每格一个
    # 色阶更细,所以头部停在两格之间时两格都会部分点亮,而不是跳到更近的那格。
    # 分隔线只在工作时绘制;空闲没有这种外观。
    GLOW_REACH: ClassVar[float] = 4.0
    GLOW_STEPS: ClassVar[int] = 12

    def sweep_divider_fragments(self, label: str, width: int | None = None, prefix: StyleAndTextTuples | None = None) -> StyleAndTextTuples:
        prefix = prefix or []
        prefix_len = sum(len(fragment[1]) for fragment in prefix)
        cols = shutil.get_terminal_size((80, 20)).columns
        width = width if width is not None else max(20, min(52, cols - 2))  # 宽度自适应:至少 20,至多 52
        body_len = prefix_len + len(label) + 2  # prefix + " label "
        lead = 3
        trail = max(3, width - lead - body_len)
        dash_count = lead + trail
        # 彗星头只在水平分隔线上往返弹跳。标签保持稳定可读,光晕看起来从两侧的
        # 虚线轨道上穿过。
        span = max(1, dash_count - 1)
        phase = time.monotonic() * self.QUEUE_SWEEP_CELLS_PER_SEC % (2 * span)
        head = phase if phase <= span else 2 * span - phase  # 三角波:往返运动

        def dashes(offset: int, count: int) -> StyleAndTextTuples:
            fragments: StyleAndTextTuples = []
            for i in range(count):
                step = int(abs(offset + i - head) / self.GLOW_REACH * self.GLOW_STEPS)
                # 距头部越远越暗;超出光晕范围就退化为普通虚线格。
                fragments.append((f"class:divider.glow{step}" if step < self.GLOW_STEPS else "class:queue.rule", "-"))
            return fragments

        return [
            *dashes(0, lead),
            ("class:queue.rule", " "),
            *prefix,
            ("class:divider.working", label),
            ("class:queue.rule", " "),
            *dashes(lead, trail),
        ]

    def queue_divider_fragments(self, queued: int = 0) -> StyleAndTextTuples:
        # 分隔线标签:working/thinking/responding 随流事件切换,并显示已运行时长。
        status = self.tui.status_label if self.tui is not None and self.tui.status_label else "working"
        if status == "working":
            retry_status = self.status_bar.retry_status()  # 重试/退避状态优先展示
            attempt_status = self.status_bar.model_attempt_status()
            with self.model_stream_lock:
                phase = self.model_stream_kind
            activity = retry_status or (
                ({"reasoning": "thinking", "output": "responding"}.get(phase, phase) or "working") + (" · " + attempt_status if attempt_status else "")
            )
            label = f"{activity} ({Text.elapsed_since(self.status_bar.started_at)})"
        else:
            label = status
        if queued:
            label = f"{label} [ {queued} queued ]"  # 有排队输入时挂上计数
        return self.sweep_divider_fragments(label, prefix=self.waiting_pulse_fragments())

    def followup_fragments(self) -> tuple[StyleAndTextTuples, StyleAndTextTuples]:
        with self.session._queue_lock:
            pending = list(self.session.pending_user_inputs)  # 锁内快照,避免与主线程竞态

        def render(items: list[QueuedInput], marker: str, marker_style: str) -> StyleAndTextTuples:
            # 多行输入:首行带标记,后续行缩进对齐。
            fragments: StyleAndTextTuples = []
            for item in items:
                for index, line in enumerate(item.text.splitlines()):
                    fragments.extend([("", "\n"), (marker_style, marker if index == 0 else "  "), (UiPrinter.user_log_style(), line)])
            return fragments

        sent = [item for item in pending if item.inflight]  # 已随请求发出
        queued = [item for item in pending if not item.inflight]  # 还在队列里
        transcript = render(sent, UiPrinter.USER_LOG_PREFIX, "class:prompt")
        # 分隔线是整个回合的常设边界。只有还没进入任何模型请求的消息留在它下方;
        # 已发送的消息渲染在它上方,直到请求把它们正式提交。
        waiting = self.queue_divider_fragments(len(queued))
        waiting.extend(render(queued, "+ ", UiPrinter.user_log_style()))
        return transcript, waiting

    def tui_activity_fragments(self) -> StyleAndTextTuples:
        # 活动区自上而下:已发送消息 → 流式预览 → Bash 实时输出 → 分隔线(排队输入)。
        sent, waiting = self.followup_fragments()
        fragments = sent
        if fragments:
            fragments.append(("", "\n"))
        stream = self.model_stream_fragments()
        fragments.extend(stream)
        if stream:
            fragments.append(("", "\n"))
        with self.live_preview.lock:
            lines = self.live_preview.frame_lines() if self.live_preview.active else []  # 活跃 Bash 的实时输出帧
        for line in lines:
            fragments.extend([("ansibrightblack", line), ("", "\n")])
        if lines:
            fragments.append(("", "\n"))
        fragments.extend(waiting)
        return fragments

    def model_stream_fragments(self) -> StyleAndTextTuples:
        with self.model_stream_lock:
            kind, text = self.model_stream_kind, self.model_stream_text  # 锁内读取流缓冲
        if not text:
            return []
        width = max(20, shutil.get_terminal_size((120, 20)).columns)
        label = "thinking" if kind == "reasoning" else "responding"
        # 只保留最后 6 行,并按终端宽度裁剪,防止预览撑满屏幕。
        rows = [Text.clip_width(line.expandtabs(4), max(1, width - 4)) for line in text.replace("\r", "\n").splitlines()[-6:]]
        lines = [f"├─ {label}", *(f"│  {row}" for row in rows)]
        fragments: StyleAndTextTuples = []
        for line in lines:
            fragments.extend([("ansibrightblack", line), ("", "\n")])
        return fragments

    def tui_input_hint(self) -> str:
        # 输入框提示:运行中提示可排队/回忆;聊天模式给出空闲占位小技巧。
        if self.tui is None:
            return ""
        if self.tui.input_mode == "running":
            with self.session._queue_lock:
                has_pending = any(not item.inflight for item in self.session.pending_user_inputs)
            return self.QUEUE_PENDING_HINT if has_pending else self.QUEUE_EMPTY_HINT
        if self.tui.input_mode == "chat":
            return self._hint_picker.pick(self._hint_context(), self.session.state.round_count)
        return ""

    def _hint_context(self) -> HintContext:
        """把 session 投影成提示机制挑选时依赖的小型"情境"。

        round_count 只在下一个回合开始时推进,因此空闲时它仍然指向刚结束的那个回合;
        这样一来,一旦后面某个回合没有任何编辑,edited_round 就会自行清空。"""
        session = self.session
        round_count = session.state.round_count
        # diff 归属本回合(按 round 或 turn)即视为"本回合做过编辑"。
        edited = any((diff.round or diff.turn) == round_count for diff in session.turn_diffs)
        return HintContext(
            early=not session.tool_records,
            edited_round=round_count if edited else None,
            skills_available=bool(session.skills and session.skills.skills),
            mcp_connected=bool(session.mcp and session.mcp.tools),
            jobs_running=any(job.status == "running" for job in session.jobs.values()),
        )

    def editor_context(self) -> str:
        """把 agent 最近的回复作为只读参考放进外部编辑器(Ctrl-X Ctrl-E / Ctrl-G):
        全屏编辑器会遮住回复打印所在的终端 scrollback。长回复只保留最近的行,
        让编辑器的临时文件保持小巧。"""
        for message in reversed(self.session.messages):
            if message.get("role") != "assistant":
                continue  # 从最新往回找,只看 assistant 消息
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                lines = content.strip().splitlines()
                if len(lines) > self.EDITOR_CONTEXT_MAX_LINES:
                    drop = len(lines) - self.EDITOR_CONTEXT_MAX_LINES
                    lines = ["# [... earlier lines of the reply omitted ...]"] + lines[drop:]  # 超长:丢掉开头,保留最近部分并注明
                return "\n".join(lines)
        return ""

    def run_queued_command(self, text: str) -> None:
        """agent 回合运行期间分发一个只读斜杠命令。"""
        name = text.partition(" ")[0]
        if name not in self.QUEUE_RUN_COMMANDS:
            # 白名单外:拒绝并提示,防止命令干扰在飞回合。
            self.emit(f"{name} is unavailable while the agent is working; press Ctrl-C to run it.")
            return
        if name == "/mcp":
            sub = text.partition(" ")[2].split()
            if sub and sub[0] != "tools":
                # /mcp 只有纯读的子命令(tools/状态)可以在运行中执行,connect 等会改状态。
                self.emit("Only read-only /mcp (status, tools) is available while the agent is working.")
                return
        self.command(text)

    def take_pending_inputs(self) -> list[UserInput]:
        """取出并返回"当前未被刷入回合"的排队输入(刷入中的输入留在队列里)。"""
        with self.session._queue_lock:
            # 只取未 inflight 的;已发出的条目保留,等请求完成后的确认流程消费。
            texts = [item.user_input() for item in self.session.pending_user_inputs if not item.inflight]
            self.session.pending_user_inputs = [item for item in self.session.pending_user_inputs if item.inflight]
        return texts

    def recall_pending_input(self, on_inflight: Callable[[], None]) -> str | UserInput:
        """把最新一条排队输入放回编辑器(↑ 键);如果它已被请求认领,先触发重试再取回。"""
        with self.session._queue_lock:
            item = next(reversed(self.session.pending_user_inputs), None)
            if item is None:
                return ""  # 队列为空:无可回忆
            self.session.pending_user_inputs.remove(item)
            was_inflight = item.inflight
            if was_inflight:
                # 该输入已随请求发出:取回它意味着那条消息作废,后续请求不再携带;
                # 其余排队输入也全部降级为未发送,避免顺序错乱。
                for pending_item in self.session.pending_user_inputs:
                    pending_item.inflight = False
        if was_inflight:
            on_inflight()  # 通知上层重发当前模型请求(带前缀的输入已经不在了)
        self.session.images.retain(item.images)
        self.session.save_snapshot()
        return item.user_input()

    def run(self) -> int:
        # 交互终端走全 TUI;注入输入/非 TTY 的调用方(含测试)走简单 REPL。
        if self.interactive_input:
            return self.run_tui()
        self.session.settings.quick_hints = False  # 简单 REPL 没有提示 UI:别让模型主动给出 hints
        self.start_session()
        while True:
            try:
                entered = self.take_pending_inputs()
                initial_input = UserInput(
                    "\n".join(str(item) for item in entered),
                    tuple(image for item in entered for image in item.images),
                )
                user_input = self.read_input(initial_text=initial_input)
            except EOFError:
                # Ctrl-D/EOF:保存会话并打印恢复提示,正常退出。
                self.emit(TurnBox.SEPARATOR)
                self.save_and_emit_resume()
                return 0
            except KeyboardInterrupt:
                continue  # 空行打断:清掉输入框重新来
            if not user_input.strip():
                continue  # 空白输入:忽略
            handled, exit_now = self.command(user_input.strip())
            if exit_now:
                return 0
            if handled:
                continue  # 命令已处理,继续下一条输入
            self.emit("")
            started = time.monotonic()
            malformed_tool_call = False
            turn_completed = False
            try:
                self.status_bar.start()
                try:
                    answer = self.agent.run(user_input)
                    turn_completed = True
                except KeyboardInterrupt:
                    self.emit("Cancelled")
                    continue
                except MalformedToolCallError as error:
                    answer = str(error)  # 文本化调用超限:把错误当答案展示
                    malformed_tool_call = True
                except YucodeError as error:
                    answer = f"Error: {error}"
            finally:
                CodeIndex(self.session).update_pending_async()  # 回合结束:落定待处理的索引更新
                self.status_bar.stop()
            if self.ui.color and answer.strip():
                self.emit()  # 颜色模式下答案前空一行
            self.ui.emit_answer(answer, rule=False)
            if footer := search_sources_footer(self.agent.turn_sources):
                self.ui.emit_answer(footer, rule=False)  # 回合内的 provider 搜索来源页脚
            if not malformed_tool_call:
                self.ui.emit_turn_end(started)
            self.session.save_snapshot()
            if turn_completed:
                self.organize_memory_after_turn()

    def start_session(self) -> None:
        """初始化两个命令循环前端共享的输出与后台服务。"""
        self.emit(f"yucode {__version__}. /help for commands.")
        UpdateChecker(self.session).start()  # 后台线程检查更新
        if self.session.update.newer_than(__version__):
            self.emit(f"update available: {__version__} -> {self.session.update.latest}. upgrade with `{' '.join(UpdateChecker.upgrade_command())}`.")
        self.clean_expired_sessions_async()  # 过期会话清理放在后台,不占启动路径
        self.render_resumed_session()  # 恢复会话时重建转录
        # 只发布"已存在"的索引状态,不在用户开始输入时扫描工作树。
        # 有界的索引新鲜度检查已经在每个回合结束后运行过了。
        CodeIndex(self.session).status()
        # 后台发现 auto_connect 服务器:不可达的服务器不会让提示符等 discovery 超时;
        # 连接成功后由工具索引自行发现它们。
        mcp = self.session.mcp
        if mcp is not None:
            threading.Thread(target=mcp.discover_auto, name="mcp-discover", daemon=True).start()

    def clean_expired_sessions_async(self) -> None:
        """把过期会话的清理移出启动路径。

        清理要统计每个项目目录里的每个会话文件。本地磁盘上这只是微秒级的事,但家目录
        挂在网络文件系统上时,每个文件都要付一次往返,可能拖成秒级——而这发生在提示符
        接受第一次按键之前。第一次按键不依赖保留策略是否已运行,所以它和旁边的代码索引、
        MCP 发现一样跑在 daemon 线程上,并通过后台通道汇报;一旦本循环不再拥有终端,
        该通道就保持安静。"""

        def sweep() -> None:
            with contextlib.suppress(Exception):  # 清理失败绝不影响启动
                removed = SessionSnapshotStore.clean_expired(self.session)
                if removed:
                    self.emit_background(self.expired_sessions_notice(removed))  # 只通过后台通道汇报

        threading.Thread(target=sweep, name="session-cleanup", daemon=True).start()

    def expired_sessions_notice(self, removed: int) -> str:
        """组织保留策略通知的措辞。

        保留策略删掉的是用户拿不回来的工作,所以要报告而不是静默执行;把设置项的名字写
        进通知,让这则通知成为用户唯一值得了解该旋钮的时刻。"""
        days = self.session.settings.session_retention_days
        sessions = "session" if removed == 1 else "sessions"
        return f"removed {removed} saved {sessions} inactive for over {days} {'day' if days == 1 else 'days'} (runtime.session_retention_days)"

    def run_tui(self) -> int:
        return TuiRuntime(self).run()

    def render_resumed_session(self) -> None:
        # 转录重建自己负责历史调用/结果匹配与顺序不变式,这里只做渲染。
        if not self.session.resumed:
            return
        self.session.resumed = False  # 一次性标志:只重建一次
        # context 百分比是推导值而不是持久值,所以恢复的会话带着完整历史却读数归零。
        # 现在重算,否则状态栏在第一个回合前一直显示 0%。
        self.agent.context.update_current_tokens(SYSTEM_PROMPT)
        # 只渲染非内部消息(去掉快照内部元数据与 tool 结果消息,tool 结果由记录重建)。
        messages = [message for message in self.session.messages if not SessionSnapshotCodec.is_internal_message(message) and message.get("role") != "tool"]
        self.emit(f"Restored session: {self.session.uid}")
        if not messages:
            return
        diffs = {diff.key: diff.diff for diff in self.session.turn_diffs if diff.key and diff.diff}  # key -> diff 文本,供 Edit 回放
        tool_record_index = 0
        for index, turn in enumerate(TurnBox.group(messages)):
            if index:
                self.emit("")  # 回合之间空一行
            for message in turn.messages:
                tool_record_index = self.render_transcript_message(message, tool_record_index, diffs)
        self.render_remaining_tool_records(tool_record_index, diffs)  # 历史里缺工具消息的调用:用剩余记录补齐

    def render_transcript_message(self, message: Json, tool_record_index: int = 0, diffs: dict[str, str] | None = None) -> int:
        role = str(message.get("role") or "")
        content = ImageInputs.label_text(message).strip()
        raw_calls = message.get("tool_calls")
        has_tool_calls = isinstance(raw_calls, list) and bool(raw_calls)
        if role == "assistant" and content:
            # 带工具调用的 assistant 消息:正文缩进一层,为下方的工具块腾出视觉空间。
            self.ui.emit_answer(content, role=role, rule=False, indent=TurnBox.CONTENT_LEVEL if has_tool_calls else TurnBox.ROOT_LEVEL)
        if role == "assistant":
            return self.render_transcript_tool_calls(message, tool_record_index, diffs or {})
        if role == "user" and content and not ImageInputs.is_tool_observation(message):
            # 跟进标记是面向模型的上下文,因为发出过所以属于历史的一部分。
            # scrollback 展示用户当时输入的样子——去掉前缀,原样呈现。
            self.ui.emit_answer(content.removeprefix(LIVE_FOLLOWUP_PREFIX.strip()).lstrip(), role=role, rule=False)
        return tool_record_index

    def render_transcript_tool_calls(self, message: Json, tool_record_index: int, diffs: dict[str, str]) -> int:
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            return tool_record_index  # 畸形/缺失的 tool_calls 不做回放
        for raw in raw_calls:
            call = self.transcript_tool_call(raw)
            if call is None:
                continue  # 无法解析的调用跳过,不回放
            record, tool_record_index = self.transcript_tool_record(call, tool_record_index)
            self.emit_transcript_tool(call, record.key if record else "", diffs)
        return tool_record_index

    def render_remaining_tool_records(self, tool_record_index: int, diffs: dict[str, str]) -> None:
        # 历史中没有对应工具消息的剩余记录(如被拒绝/跳过调用产生的结果)也一并渲染,
        # 保持"一调用一结果"的转录完整性。
        for record in self.session.tool_records[tool_record_index:]:
            call = ToolCall(id="", name=record.name, args=record.args)
            self.emit_transcript_tool(call, record.key, diffs)

    def emit_transcript_tool(self, call: ToolCall, key: str, diffs: dict[str, str]) -> None:
        """Edit 展示它当时做的 diff,和现场运行时的方式一致。现场预览来自审批块;
        这里存储的 diff 文本就是同一个字符串,所以回放无需任何重建。"""
        preview = diffs.get(key, "") if call.name == "Edit" else ""
        if not preview:
            self.emit(self.agent.tools.finish_display(call, key, "", failed=False))
            return
        # 预览块自带调用行,结果行就折叠成它下面的尾部标记——和现场审批块相同的嵌套结构。
        self.emit(self.transcript_edit_preview(call, preview))
        self.emit(self.agent.tools.finish_display(call, key, "", failed=False, d=ToolDisplay(nested_display=True)))

    def transcript_edit_preview(self, call: ToolCall, preview: str) -> LogBlock:
        tools = self.agent.tools
        lines = preview.rstrip().splitlines()
        # 长回放会把提示符埋进一堆 diff 里,所以每个 diff 都裁剪到可读窗口;
        # 完整文本仍在 /diff 里。
        hidden = max(0, len(lines) - self.TRANSCRIPT_DIFF_LINES)
        if hidden:
            lines = lines[: self.TRANSCRIPT_DIFF_LINES]
        children = [LogLine("preview", role=LogRole.META, edge=LogEdge.BRANCH)]
        children.extend(LogLine("", line, LogRole.DIFF, LogEdge.CONTINUE) for line in lines)
        if hidden:
            children.append(LogLine("", f"… {hidden} more lines, see /diff", LogRole.META, LogEdge.CONTINUE))
        return LogBlock.hierarchy(tools.log_root(tools.short_call(call), LogRole.AUTO, "", call), children)

    @staticmethod
    def transcript_tool_call(raw: object) -> ToolCall | None:
        if not isinstance(raw, dict):
            return None  # 历史快照里的畸形条目:跳过
        raw_function = raw.get("function")
        function = raw_function if isinstance(raw_function, dict) else {}
        name = str(function.get("name") or "")
        if not name:
            return None  # 没有名字的调用无法回放
        arguments = function.get("arguments")
        try:
            # strict=False 容忍参数串里的字面换行(例如多行 git commit message),
            # 否则它们会被当作非法 JSON 拒绝。
            payload = json.loads(arguments, strict=False) if isinstance(arguments, str) else (arguments or {})
        except json.JSONDecodeError:
            payload = {}  # 参数无法解析:退化为空 payload
        try:
            args = ModelClient.tool_payload(name, payload)
        except ToolError:
            # 历史中的畸形调用(如参数校验失败的 tool args)不能弄崩恢复流程:
            # 不带解析参数地渲染它。
            args = [payload] if payload else []
        return ToolCall(id=str(raw.get("id") or ""), name=name, args=args)

    def transcript_tool_record(self, call: ToolCall, tool_record_index: int) -> tuple[ToolResultRecord | None, int]:
        tool_class = TOOL_REGISTRY.get(call.name)
        if tool_class is not None and not tool_class.STORES_RESULT:
            return None, tool_record_index  # 不存储结果的工具没有对应记录
        records = self.session.tool_records
        while tool_record_index < len(records):
            record = records[tool_record_index]
            tool_record_index += 1
            if record.name == call.name:
                return record, tool_record_index  # 找到同名记录(按历史顺序消费)
        return None, tool_record_index

    def save_and_emit_resume(self) -> None:
        uid = self.session.save_snapshot()
        if uid:
            # 名字只出现在句子里,绝不放进命令:下面这行是拿来粘贴的,
            # 而只有 uid 能保证明天仍然指向这个会话。
            name = self.session.name
            self.emit(f"Resume {name!r} with:\nyucode --resume {uid}" if name else f"Resume with:\nyucode --resume {uid}")

    def style(self) -> Style:
        rule = Theme.style("divider.rule")
        return Style.from_dict(
            {
                "prompt": "ansicyan bold",
                # 彗星在它滑过的分隔线上渐隐,所以两者都取自调色板。
                "queue.rule": rule,
                **{f"divider.glow{step}": color for step, color in enumerate(Theme.ramp("divider.glow", "divider.rule", self.GLOW_STEPS))},
                "queue.hint": "ansibrightblack",
                "quickhint": "ansicyan",
                "quickhint.focused": "reverse",
                "quickhint.sep": "ansibrightblack",
                "image.attachment": "ansicyan bold",
                "input.error": "ansired",
                "divider.working": "ansimagenta bold",
                "approval": "ansiyellow",
                "approval.wait": "ansimagenta",
                "choice.title": "ansicyan bold",
                "choice.selected": "reverse",
                "choice.disabled": "ansibrightblack",
                "choice.preview": "ansigreen italic",
                "choice.status.connected": "ansigreen bold",
                "choice.status.connecting": "ansigreen bold",
                "choice.status.disconnected": "ansiyellow bold",
                "choice.status.disconnecting": "ansiyellow bold",
                "choice.status.error": "ansired bold",
                "choice.status.skipped": "ansibrightblack",
                "tab.active": "bold reverse ansicyan",
                "tab.inactive": "ansicyan",
                "completion-menu": "noreverse bg:default",
                "completion-menu.completion": "noreverse bg:default fg:default",
                "completion-menu.completion.current": "noreverse bg:default fg:ansicyan bold",
                "completion-menu.meta.completion": "noreverse bg:default fg:ansibrightblack",
                "completion-menu.meta.completion.current": "noreverse bg:default fg:ansicyan",
                "bottom-toolbar": "noreverse bg:default fg:default",
                "bottom-toolbar.text": "noreverse bg:default fg:default",
                "search-toolbar": "noreverse bg:default fg:default",
                "search-toolbar.prompt": "ansicyan",
                "search-toolbar.text": "fg:default",
            }
        )

    def read_input(
        self,
        prompt_text: str = UiPrinter.PROMPT_PREFIX,
        *,
        initial_text: str = "",
    ) -> str:
        """从注入输入/非 TTY 路径读取;交互终端使用 TuiApp。"""
        return initial_text or self.input_fn(prompt_text)

    def emit(self, text: str | LogBlock = "") -> None:
        self.ui.emit(text)

    def emit_background(self, text: str) -> None:
        """仅当本循环仍拥有终端输出时才从 daemon worker 发出文本。"""
        with self.background_output_lock:
            if self.background_output_open:
                self.emit(text)

    def close_background_output(self, final_output: Callable[[], None] | None = None) -> None:
        with self.background_output_lock:
            self.background_output_open = False  # 之后 emit_background 一律静默
            if final_output is not None:
                final_output()  # 关门前最后输出(如退出前的摘要)

    def with_status_paused(self, action):
        # 只暂停简单/非 TTY 路径使用的独立状态栏线程。全 TUI 把状态和输出一起渲染,
        # 永远不需要这种终端级协调。
        was_running = self.status_bar.is_running()
        if was_running:
            self.status_bar.stop()  # 输出期间停掉状态栏,避免刷屏互相覆盖
        try:
            return action()
        finally:
            if was_running:
                self.status_bar.start(reset=False)  # 恢复状态栏,但不重置开始时间

    def tool_output(self, text: str | LogBlock = "") -> None:
        def output() -> None:
            # 颜色模式下,单条工具输出前空一行保持视觉分隔。
            if self.ui.color and (isinstance(text, str) or (text.items and isinstance(text.items[0], LogLine))):
                self.emit()
            self.emit(text)

        self.with_status_paused(output)  # 输出期间暂停独立状态栏,避免互相覆盖

    def builtin_call_output(self, label: str, detail: str) -> None:
        """记录 provider 替自己执行的工具,让转录像展示其他调用一样展示它。

        provider 侧搜索不会留下可记录的本地工具调用,而且运行状态标签在回合结束的瞬间
        就消失了。没有这行的话,转录会把模型"自己去查过"的知识记成它本来就有的。"""
        self.tool_output(LogBlock([LogLine(label, Text.clip_width(detail, 120), LogRole.TOOL, LogEdge.BRANCH)]))

    @staticmethod
    def unpromoted_text(text: str, promoted: str) -> str:
        """早前"提升"(promotion)已经把 `promoted` 写进 scrollback 后,还剩下什么要发布。

        本地工具调用会结束响应,所以被提升的文本就是它的全部。而 provider 侧工具在响应
        内部运行,模型之后还会继续写,所以那里的提升只是前缀:整个重发会重复,整个跳过
        又会丢掉搜索之后模型写的所有内容。"""
        answer = text.strip()
        if promoted and answer.startswith(promoted):
            return answer[len(promoted) :].strip()  # 去掉已提升的前缀,只发剩余部分
        return answer  # 文本与提升内容不符(被更新过):全部重发

    def agent_output(self, text: str = "") -> None:
        # 早期提升只是展示层面的:Agent 在 ModelClient 返回后仍会发布同一段语义文本。
        # 这里消费掉一次性标记,避免打印两遍。
        with self.model_stream_lock:
            promoted = self.model_stream_promoted_text
            self.model_stream_promoted_text = ""  # 一次性消费
        if promoted:
            remaining = self.unpromoted_text(text, promoted)
            if not remaining:
                return  # 全部内容都已提升过:不再打印
            text = remaining
        self.with_status_paused(lambda: self.emit_agent_output(text))

    def model_stream_output(self, kind: str, text: str) -> None:
        """更新暗色预览,或把协议完整的响应永久"提升"到 scrollback。

        `output_done` 是内部事件,只在 ModelClient 同时看到完整文本与工具调用时发出。
        scrollback 写入是同步的,这样 prompt-toolkit 不会把它和紧随其后的 ToolRunner 输出
        批量渲染,从而让 `responding` 预览遮住它。
        """
        promote = ""
        tui = self.tui
        if kind == "output_done" and self.session.has_inflight_user_inputs():
            # 携带实时跟进消息的请求只在返回后把消息记入 scrollback,所以在这里提升会把
            # 响应放到它所回答的消息上方。让预览保持原样,由常规的请求后输出维持转录顺序。
            return
        with self.model_stream_lock:
            if kind == "output_done":
                promote = text.strip()
                self.model_stream_kind = self.model_stream_text = ""  # 清除预览缓冲
                if promote and tui is not None:
                    self.model_stream_promoted_text = promote  # 记下已提升内容,供 agent_output 去重
            elif not kind:
                self.model_stream_kind = self.model_stream_text = ""  # 流结束:清空预览
            elif not text:
                self.model_stream_kind, self.model_stream_text = kind, ""  # 只改阶段、不改文本
            elif text:
                if kind != self.model_stream_kind:
                    self.model_stream_kind, self.model_stream_text = kind, ""  # 阶段切换:丢弃旧文本
                self.model_stream_text = (self.model_stream_text + text)[-8000:]  # 缓冲封顶 8000 字符,防内存无限增长
        if tui is not None:
            tui.invalidate_frame()  # 通知 TUI 重绘活动区
            if promote:
                # 提升为正式输出:同步写入 scrollback,保证顺序。
                self.with_status_paused(lambda: tui.write_to_scrollback(lambda: self.emit_agent_output(promote)))

    def tool_input(self, prompt: str = "") -> str:
        # TUI 运行时,把 agent 的审批路由到 TuiApp 自己的输入控件,让用户就在常驻 shell
        # 里内联回答,而不是另开一个 prompt_toolkit Application(那会失败,因为 pt
        # 不能嵌套)。
        if self.tui is not None:
            return self.tui.request_input(prompt)

        return self.with_status_paused(lambda: self.input_fn(prompt))

    def emit_agent_output(self, text: str) -> None:
        if self.ui.color and text.strip():
            self.emit()  # 颜色模式下,答案前空一行
        self.ui.emit_answer(text, rule=False, indent=TurnBox.CONTENT_LEVEL)  # 内容缩进一级,与工具块对齐

    def _begin_cli_preview(self) -> None:
        """暂停(若在运行的)状态栏,并启动 CLI Bash 实时预览行。"""
        self.live_status_paused = self.status_bar.is_running()  # 记住是否需要恢复
        if self.live_status_paused:
            self.status_bar.stop()
        self.live_preview.start()

    def tool_live_start(self) -> None:
        if not self.ui.color:
            return  # 无颜色模式:没有实时预览
        if self.tui is not None:
            with self.live_preview.lock:
                self.live_preview.active = True
                self.live_preview.text = ""
                self.live_preview.started_at = time.monotonic()
            self.tui.invalidate()
            return
        self._begin_cli_preview()

    def tool_live_output(self, _stream: str, text: str) -> None:
        if not self.ui.color:
            return
        if self.tui is not None:
            with self.live_preview.lock:
                if text:
                    self.live_preview.active = True
                    self.live_preview.text = (self.live_preview.text + text)[-self.live_preview.MAX_CHARS :]  # 预览文本封顶,防内存无限增长
                else:
                    self.live_preview.active = False  # 空文本 = 流结束
                    self.live_preview.text = ""
            self.tui.invalidate()
            return
        if text:
            if not self.live_preview.active:
                self._begin_cli_preview()  # 首块输出才启动预览
            self.live_preview.update(text)
            return
        if self.live_preview.active:
            self.live_preview.finish()  # 流结束:定格最后一帧
        if self.live_status_paused:
            self.status_bar.start(reset=False)  # 恢复状态栏
            self.live_status_paused = False

    def command(self, text: str) -> tuple[bool, bool]:
        if text in {"/exit", "/quit", "exit", "quit"}:
            self.save_and_emit_resume()
            return True, True  # (已处理, 退出)
        if not text.startswith("/"):
            return False, False  # 不是命令:由调用方当普通输入跑回合
        name, _, args = text.partition(" ")
        method_name = self.COMMAND_HANDLERS.get(name)
        handler = getattr(self, method_name, None) if method_name else None
        output = handler(args.strip()) if handler else f"Unknown command: {name}"
        # None 结果表示 handler 已经渲染了自己的 UI(例如 /diff 的查看器)。
        if output is not None:
            if name == "/status":
                self.ui.emit_answer(output, rule=False)
            else:
                (self.ui.emit_answer if name in {"/help", "/ps", "/mcp", "/skills", "/diff"} else self.emit)(output)
        # 请求切换会话的 handler 会像 /exit 一样结束本次运行;`main` 会围绕它
        # 指定的 session 启动下一个。
        return True, bool(self.resume_request)

    def resend_command(self, _args: str) -> str | None:
        """重发在途的模型请求。只在运行中的排队输入区可用:回合进行中输入它,
        会重新请求当前的模型调用(与 on_retry 同一条路径)。"""
        if self.tui is None or self.tui.input_mode != "running":
            return "/resend re-requests the current model request — type it while a turn is working."  # 非运行态:提示用法
        if self.session.state.current_model_call_started_at <= 0:
            return "Nothing to resend right now; /resend works while the model is generating."  # 没有在途请求
        self.tui.on_retry()
        return None  # None = 不输出文本,由 TUI 自己处理

    def mcp_command(self, args: str) -> str | None:
        mcp = self.session.mcp
        if mcp is None:
            return "MCP not configured"

        parts = args.split()
        if not parts:
            # 无子命令:空闲 TUI 打开交互式选择器;否则输出文本状态。
            if self.tui is not None and self.tui.input_mode != "running":
                return self.mcp_manager()
            return mcp.render_server_status()

        sub = parts[0]
        rest = parts[1:]
        command = self.MCP_COMMANDS.get(sub)
        if command is None:
            return f"Unknown /mcp subcommand: {sub}. {self.MCP_HELP}"
        min_args, max_args, usage = command
        if not min_args <= len(rest) <= max_args:
            return usage  # 参数个数不合法:给出用法

        if sub == "connect":
            return mcp.connect_servers(rest, interactive=self.interactive_input, notify=self.emit)
        if sub == "disconnect":
            return mcp.disconnect_server(rest[0])
        if sub == "tools":
            return mcp.render_tool_listing(rest[0] if rest else None)
        raise AssertionError("unreachable MCP subcommand")  # 所有子命令都已处理,理论不可达

    def mcp_manager(self) -> None:
        # 交互式 MCP 服务器管理器:选择器里每行一个服务器,回车切换连接状态。
        mcp = self.session.mcp
        tui = self.tui
        if mcp is None or tui is None:
            return
        configs = tuple(mcp.parse_configs())
        if not configs:
            self.ui.emit_answer(mcp.render_server_status())
            return

        # transitions: 正在进行的切换(name -> 目标动作);errors: 上次切换的失败信息;
        # modal_open 跟踪模态窗是否还开着,决定切换结果走 UI 通道还是后台通道。
        state = ChoiceViewState(tuple(config.name for config in configs), {}, set())
        transitions: dict[str, str] = {}
        errors: dict[str, str] = {}
        state_lock = threading.Lock()  # 后台切换线程与渲染线程共享状态,需要加锁
        modal_open = threading.Event()
        modal_open.set()

        def server_labels() -> dict[str, str]:
            with state_lock:
                changing = dict(transitions)
                failed = dict(errors)
            server_rows = []
            for config in configs:
                # 状态优先级:切换中 > 出错 > 已知问题 > 已连接 > 未连接。
                if transition := changing.get(config.name):
                    status = mcp.STATUS_MARKER + " " + transition
                elif config.name in failed:
                    status = mcp.STATUS_MARKER + " error"
                elif issue := mcp.server_issue(config.name):
                    status = mcp.STATUS_MARKER + " " + issue[0]
                elif mcp.connected(config.name):
                    status = mcp.STATUS_MARKER + " connected"
                else:
                    status = mcp.STATUS_MARKER + " disconnected"
                mode = "auto" if config.auto_connect else "manual"
                count = len(mcp.tools.get(config.name, []))
                server_rows.append((config.name, status, mode, count))
            name_width = max(len(name) for name, *_rest in server_rows)
            status_width = max(len(mcp.STATUS_MARKER + " disconnecting"), *(len(status) for _name, status, _mode, _count in server_rows))
            # 按列宽对齐排版,保证选择器里各行整齐。
            return {name: f"{name:<{name_width}}  {status:<{status_width}}  {mode:<6}  {count:>3} tools" for name, status, mode, count in server_rows}

        def preview(name: str) -> str:
            # 预览:优先显示最近一次失败,其次显示服务器的已知问题说明。
            with state_lock:
                if message := errors.get(name):
                    return message
            if issue := mcp.server_issue(name):
                return issue[1]
            return ""

        def fragments() -> StyleAndTextTuples:
            state.labels = server_labels()  # 每帧重算标签:切换状态实时反映
            return state.fragments("MCP servers · Enter toggles connection", preview)

        def toggle(name: str, connect: bool) -> None:
            try:
                if connect:
                    result = mcp.connect_server(name, interactive=True, notify=self.emit)
                else:
                    result = mcp.disconnect_server(name)
            except Exception as error:  # noqa: BLE001 - 后台 MCP 失败也要能在选择器里看到
                result = f"MCP server error: {name}: {error}"

            succeeded = mcp.connected(name) == connect
            with state_lock:
                transitions.pop(name, None)  # 切换结束,移出"切换中"标记
                if succeeded:
                    errors.pop(name, None)  # 成功:清掉历史错误
                else:
                    errors[name] = result  # 失败:记录原因供 preview 显示
            if modal_open.is_set():
                tui.invalidate()  # 模态窗还开着:刷新选择器
            else:
                self.emit_background(result)  # 否则走后台通道(如失败发生在本循环退出后)

        def handle_key(key: str, data: str = "") -> Any:
            result = state.handle_key(key, data)
            if not isinstance(result, str):
                return result  # 导航类按键:交给选择器状态机
            with state_lock:
                if result in transitions:
                    return TUI_MODAL_PENDING  # 该服务器正在切换:忽略重复按键
                connect = not mcp.connected(result)
                errors.pop(result, None)
                transitions[result] = "connecting" if connect else "disconnecting"
            # 连接/断开放到后台线程,避免阻塞 UI 线程;返回 PENDING 保持模态窗。
            threading.Thread(target=toggle, args=(result, connect), name="mcp-toggle-" + result, daemon=True).start()
            return TUI_MODAL_PENDING

        try:
            tui.show_modal(fragments, handle_key)
        finally:
            modal_open.clear()  # 模态窗关闭后,后台切换结果改走 emit_background

    def select_choice(
        self,
        title: str,
        choices: tuple[str, ...],
        *,
        labels: dict[str, str] | None = None,
        current: str = "",
        disabled: set[str] | frozenset[str] = frozenset(),
    ) -> str | object | None:
        labels = labels or {}
        if not choices or not self.interactive_input:
            return None  # 无可选项或非交互路径:不做选择器,由调用方走默认
        enabled = tuple(choice for choice in choices if choice not in disabled)
        if len(enabled) == 1:
            return enabled[0]  # 只有一个可选:直接选中,跳过选择器
        try:
            return self.choice_application(title, choices, labels, current, set(disabled))
        except (EOFError, KeyboardInterrupt):
            self.emit("Cancelled")
            return None  # 用户中断选择:视同放弃

    def choice_application(
        self,
        title: str,
        choices: tuple[str, ...],
        labels: dict[str, str],
        current: str,
        disabled: set[str],
        *,
        preview_fn: Callable[[str], str] | None = None,
        free_text: bool = False,
    ) -> str | object | None:
        if free_text and self.interactive_input:
            choices = (*choices, ChoiceViewState.FREE_TEXT)
            labels = {**labels, ChoiceViewState.FREE_TEXT: "Type freely..."}  # 追加"自由输入"选项
        state = ChoiceViewState(choices, labels, disabled)
        options = state.enabled()
        state.selected = options.index(current) if current in options else 0  # 默认选中当前值(若存在)
        if self.tui is None:
            return None
        result = self.tui.show_modal(lambda: state.fragments(title, preview_fn), state.handle_key)
        if isinstance(result, KeyboardInterrupt):
            raise result  # 选择器内中断:上抛给 select_choice 统一处理
        return result

    def question_application(self, spec: AskSpec, position: str = "") -> str:
        """通过共享的选择器提问,支持动态预览和自由输入兜底。"""
        choices = spec.choices
        # 把位置前缀(如 "(1/3) ...")拼进问题文本,按普通 markdown 渲染——
        # 不需要单独的样式行,也就没有 ANSI 转义会被弄乱的问题。
        prompt = f"({position}) {spec.question}" if position else spec.question
        if not choices:
            # 无选项:直接输入框(先渲染问题文本)。
            return self.tui.request_input("\n" + prompt) if self.tui is not None else self.read_input("\n" + prompt)
        if not self.interactive_input:
            return self.read_input("\n" + prompt)  # 非交互:纯文本提问

        # 每个问题前空一行,多问题提示不会挤在一起。
        if self.ui.color:
            self.emit("")
            self.ui.emit_markdown(prompt)
        else:
            self.emit("\n" + prompt + "\n")

        # 可选的推荐项通过 current 预选中、通过 labels 标记出来,
        # 直接复用选择器已有的机制。
        labels, current = {}, ""
        if spec.recommended is not None and 0 <= spec.recommended < len(choices):
            current = choices[spec.recommended]
            labels = {current: current + " (recommended)"}
        previews = spec.previews
        preview_map = {c: previews[i] for i, c in enumerate(choices) if previews and i < len(previews) and previews[i]}  # 选项 -> 预览文本
        result = self.choice_application(
            "Select:",
            tuple(choices),
            labels,
            current,
            set(),
            preview_fn=lambda choice: preview_map.get(choice, ""),
            free_text=True,
        )
        if result is SELECTION_FREE_TEXT:
            # 问题已经渲染过了;切到自由输入时不要再重复一遍冗长的原始提示。
            self.emit("")
            return self.tui.request_input("> ") if self.tui is not None else self.read_input("> ")
        if isinstance(result, str):
            return result
        return DISMISSED  # SELECTION_BACK (Esc)——用户拒绝作答

    def question_interaction(self, spec: AskSpec, position: str = "") -> str:
        """Ask 工具的入口;返回的答案由最终的 tool 日志渲染。"""
        return self.question_application(spec, position)

    def select_reasoning(self) -> str | object | None:
        # 当前值带上 "(current)" 标签,让选择器一眼看出现状。
        current = self.session.config.provider.reasoning
        labels = {"off": "off - disable reasoning"}
        labels[current] = labels.get(current, current) + " (current)"
        return self.select_choice("Reasoning effort", REASONING_CHOICES, labels=labels, current=current)

    def select_api(self, model: str) -> str | object | None:
        # 一个列出多个模型家族的端点很少只用一种协议就能全部服务,/models 列表也不说明
        # 这一点。所以选择模型时顺带确认 wire 协议。
        provider = self.session.config.provider
        current = provider.api
        inferred = replace(provider, api="auto", model=model).resolve().api  # 预演 auto 解析结果,写进标签
        labels = {"auto": f"auto - infer from the endpoint URL and model ({inferred})"}
        labels[current] = labels.get(current, current) + " (current)"
        return self.select_choice("Request API", PROVIDER_API_CHOICES, labels=labels, current=current)

    def help(self, args: str) -> str:
        text = self.HELP.rstrip()
        if self.ui.color:
            return text  # 颜色模式:原样输出 markdown
        text = text.replace("`", "")  # 纯文本模式:去掉反引号
        text = self.HELP_HEADING_RE.sub(r"\1:", text)  # "### 命令" -> "命令:"
        return self.HELP_ENTRY_RE.sub(r"  \1  ", text)  # 条目重新缩进

    def status(self, args: str) -> str:
        def progress_bar(value: int, total: int, width: int = 14) -> str:
            ratio = min(1.0, max(0.0, value / total)) if total else 0.0  # 封顶 100%;total 为 0 时按 0 处理
            eighths = int(ratio * width * 8 + 0.5)  # 宽度×8 细分,支持半格字符
            full, partial = divmod(eighths, 8)
            partials = "▏▎▍▌▋▊▉"
            return "[" + "█" * full + (partials[partial - 1] if partial else "") + "░" * (width - full - bool(partial)) + "]"

        def token_count(value: int) -> str:
            if value >= 1_000_000:
                return f"{value / 1_000_000:.1f}M"
            if value >= 1_000:
                return f"{value / 1_000:.1f}K"
            return str(value)

        usage = self.session.usage
        provider = self.session.config.provider
        resolved = provider.resolve()
        context_tokens = self.agent.context.update_current_tokens(SYSTEM_PROMPT)  # 未发送过请求时的估算值
        context_budget = self.agent.context.request_token_budget()
        if usage.last_prompt_tokens and usage.last_prompt_budget:
            # 展示 provider 上报的 token 数与上一次请求的预算;估算值
            # (state.context_percent)在没有任何请求之前作为兜底。
            context_tokens = usage.last_prompt_tokens
            context_budget = usage.last_prompt_budget
            context_percent = min(100, context_tokens * 100 // context_budget)  # 整数百分比,封顶 100
        else:
            context_percent = self.session.state.context_percent
        index = CodeIndex(self.session)
        index_status, index_message = index.status(check=False)
        index.update_pending_async()
        if self.session.state.code_index_refreshing:
            index_status, index_message = self.session.state.code_index_notice or "syncing", ""
        elif self.session.state.code_index_error:
            index_status, index_message = "error", self.session.state.code_index_error  # 后台同步失败:显示错误
        if index_status in {"missing", "unavailable", "error"} and "run /index" not in index_message:
            index_message = (index_message + "; " if index_message else "") + "run /index"  # 不可用状态附上修复提示
        elif index_status == "stale" and "run /index" not in index_message:
            index_message = (index_message + "; " if index_message else "") + "run /index or wait for auto update"
        cache_ratio = (usage.cached_prompt_tokens * 100 / usage.prompt_tokens) if usage.prompt_tokens else 0  # prompt cache 命中率
        last_cache_ratio = (usage.last_cached_prompt_tokens * 100 / usage.last_prompt_tokens) if usage.last_prompt_tokens else 0
        connected_mcp = sum(self.session.mcp.connected(config.name) for config in self.session.mcp.parse_configs()) if self.session.mcp else 0
        activity: list[tuple[str, int | str]] = [
            ("history", len(self.session.messages)),
            ("turn", self.session.state.turn_messages),
            ("tools", len(self.session.tool_results)),
            ("mcp", connected_mcp),
            ("skills", len(self.session.skills.skills) if self.session.skills else 0),
            ("known", len(self.session.state.known)),
            ("compactions", self.session.state.compaction_count),
        ]
        running_jobs = len(self.session.running_jobs())
        if self.session.jobs:
            activity.append(("jobs", f"{running_jobs}/{len(self.session.jobs)}"))
        rows = [
            ("workspace", "`" + self.session.cwd + "`"),
            ("session", "`" + self.session.uid + "`"),
            (
                "model",
                f"`{self.session.config.active_provider}/{provider.model or '(empty)'}`; api `{resolved.api}`; reasoning `{provider.reasoning}`",
            ),
            (
                "context",
                f"`{progress_bar(context_tokens, context_budget)}` `~{token_count(context_tokens)} / {token_count(context_budget)}` (`{context_percent}%`)",
            ),
            (
                "cache",
                f"`{progress_bar(usage.last_cached_prompt_tokens, usage.last_prompt_tokens)}` last read `{token_count(usage.last_cached_prompt_tokens)} / {token_count(usage.last_prompt_tokens)} ({last_cache_ratio:.1f}%)`, write `{token_count(usage.last_cache_write_prompt_tokens)}`; "
                f"session read `{token_count(usage.cached_prompt_tokens)} / {token_count(usage.prompt_tokens)} ({cache_ratio:.1f}%)`, write `{token_count(usage.cache_write_prompt_tokens)}`"
                if usage.prompt_tokens
                else "(no requests yet)",
            ),
        ]
        if self.session.state.goal:
            rows.append(("goal", self.session.state.goal))
        visible_activity = [(name, value) for name, value in activity if value]
        if visible_activity:
            rows.append(("activity", "; ".join(f"{name} `{value}`" for name, value in visible_activity)))
        if usage.calls:
            rows.append(("usage", f"calls `{usage.calls}`; total `{token_count(usage.total_tokens)}`"))
        runtime = [
            f"yolo {'on' if self.session.settings.yolo else 'off'}",
            f"steps {self.session.settings.max_steps}",
            CodeIndex.status_line(index_status, index_message),
        ]
        update = UpdateChecker(self.session).status_line().removeprefix("update: ")
        if update not in {"current", "unknown"}:
            runtime.append("update " + update)
        rows.append(("runtime", "; ".join(f"`{value}`" for value in runtime)))
        return "\n".join(
            [
                "| status | value |",
                "| --- | --- |",
                *(f"| {name} | {Text.clean(str(value)).replace(chr(10), ' ').replace('|', chr(92) + '|')} |" for name, value in rows),
            ]
        )

    def skills_command(self, args: str) -> str:
        # 列出已安装 skills 及来源;SKILL.md 位于项目级或用户级 .yucode/skills/ 下。
        library = self.session.skills
        skills = library.all() if library else []
        if not skills:
            return "No skills installed. Add `<name>/SKILL.md` under `.yucode/skills/` (project) or `~/.yucode/skills/` (user)."
        table = markdown_table(
            ["skill", "source", "description"],
            [(f"`{skill.name}`", skill.source, skill.description or "(no description)") for skill in skills],
        )
        return "\n".join([f"### Skills · {len(skills)}", "", "Load with `Skill(name)` or reference inline with `$name`.", "", table])

    def ps_command(self, args: str) -> str:
        if args.strip():
            return "Usage: /ps"
        running = self.session.running_jobs()
        if not running:
            total = len(self.session.jobs)
            return f"No active jobs ({total} total)."  # 无活跃任务时顺带说明总数
        rows = [(job.id, job.status, f"{job.elapsed():.1f}s", job.command[:80]) for job in running]
        table = markdown_table(["id", "status", "elapsed", "command"], rows)
        return f"### Active jobs · {len(running)}\n\n{table}"

    def bash_output_viewer(self) -> None:
        """浏览最近完成的 Bash 输出预览,而不把它们复制进 scrollback。"""
        if self.tui is None:
            return
        records = []
        for record in reversed(self.session.tool_records):
            if record.name != "Bash":
                continue
            preview = self.agent.tools.bash_result_preview(record.output)
            if preview:
                records.append((record, preview))  # 只收有预览可看的记录
            if len(records) == 10:
                break  # 最多 10 条,避免列表过长
        if not records:
            return
        width = max(20, shutil.get_terminal_size((120, 20)).columns - 12)
        labels = {}
        calls = {}
        for index, (record, _preview) in enumerate(records):
            call = self.agent.tools.short_call(ToolCall("", "Bash", record.args))
            choice = str(index)
            calls[choice] = call
            labels[choice] = Text.clip_width(f"{record.key}  {call}", width)
        choices = tuple(labels)
        state = ChoiceViewState(choices, labels, set())
        opened: str | None = None

        def rule(label: str) -> StyleAndTextTuples:
            cols = shutil.get_terminal_size((80, 20)).columns
            rule_width = max(20, min(72, cols - 2))
            lead = "──── "
            trail = " " + "─" * max(3, rule_width - get_cwidth(lead + label) - 1)
            return [("", "\n"), ("class:choice.disabled", lead + label + trail + "\n")]

        def fragments() -> StyleAndTextTuples:
            if opened is None:
                list_fragments = state.fragments("")
                return [*rule(f"Bash outputs · latest {len(records)}"), *list_fragments[1:]]
            record, preview = records[int(opened)]
            detail_width = max(20, shutil.get_terminal_size((120, 20)).columns - 6)
            parts: StyleAndTextTuples = [*rule(f"Bash output · {record.key}"), ("ansibrightblack", f"  {Text.clip_width(calls[opened], detail_width)}\n\n")]
            parts.extend(("ansibrightblack", f"  {Text.clip_width(line, detail_width)}\n") for line in preview.splitlines())
            parts.append(("class:choice.disabled", "\n  Esc / ← back · Ctrl-O / q closes\n"))
            return parts

        def handle_key(key: str, data: str) -> Any:
            nonlocal opened
            if key in {"c-o", "q"}:
                return None  # 关闭查看器
            if opened is not None:
                if key in {"escape", "left", "h"}:
                    opened = None  # 从详情返回列表
                return TUI_MODAL_PENDING  # 详情态按键继续留在模态窗
            result = state.handle_key(key, data)
            if result is SELECTION_BACK:
                return None  # Esc:退出查看器
            if isinstance(result, str):
                opened = result  # 选中某条:打开详情
            return TUI_MODAL_PENDING

        self.tui.show_modal(fragments, handle_key)

    def diff_command(self, args: str) -> str | None:
        if args.strip():
            return "Usage: /diff"
        if self.interactive_input and self.ui.color and (self.tui is None or self.tui.alternate_screen_available()):
            self.diff_viewer()  # 交互终端:全屏交互查看器
            return None
        latest = self.agent.session.latest_round_diff_sections()
        session = self.agent.session.session_diff_sections()
        groups: list[tuple[str, list[tuple[str, str, str]]]] = []
        if latest is not None and latest[1]:
            round, sections = latest
            groups.append((f"Latest · Round {round}", sections))
        if session:
            groups.append(("Session", session))
        if not groups:
            return "No changes"
        lines: list[str] = []
        for title, sections in groups:
            lines.append("### " + title)
            for _status, path, diff in sections:
                lines.append(f"#### {path}")
                bounded, truncated = CommandLoop.bounded_diff(diff)  # 超大 diff 截断
                lines.append(f"```diff\n{bounded}\n```")
                if truncated:
                    lines.append("\n*Diff truncated. Full edit output is stored in the session.*")
        return "\n".join(lines)

    def diff_viewer(self) -> None:
        """交互式 diff 查看器。先显示文件列表;打开某个文件查看它的 diff。

        列表模式:↑/↓ 或 j/k 移动,h/l 或 ←/→ 切换标签,Enter 打开所选文件,
        r 刷新,q/Esc 关闭。
        diff 模式:↑/↓ 逐行滚动,Ctrl-U/Ctrl-D 半页,PgUp/PgDn 整页,
        Esc/← 返回列表,r 刷新,q 关闭。
        """
        state = DiffViewState(TabbedViewState(("Latest", "Session")))  # 两个标签:本轮 vs 整个会话

        def build_model() -> list[list[tuple[str, str, str]]]:
            latest = self.agent.session.latest_round_diff_sections()
            return [latest[1] if latest is not None else [], self.agent.session.session_diff_sections()]

        model = build_model()

        def viewport() -> int:
            return max(3, shutil.get_terminal_size().lines - 7)  # 减去头部/提示占用的行数

        def active_sections() -> list[tuple[str, str, str]]:
            return model[state.view.tab]

        def list_fragments(parts: StyleAndTextTuples, sections: list[tuple[str, str, str]]) -> None:
            parts.append(("", "\n"))
            counts = [CommandLoop.diff_counts(diff) for _status, _path, diff in sections]
            added_width = max(len(str(added)) for added, _removed in counts)  # 按列宽对齐
            removed_width = max(len(str(removed)) for _added, removed in counts)
            for index, ((_status, path, _diff), (added, removed)) in enumerate(zip(sections, counts)):
                selected = index == state.file
                marker = "> " if selected else "  "  # 选中行打标记
                style = "ansicyan" if selected else "class:choice.disabled"
                parts.extend(
                    [
                        (style, marker),
                        ("ansigreen", f"+{added:>{added_width}}"),  # 新增数绿色
                        ("", " "),
                        ("ansired", f"-{removed:>{removed_width}}"),  # 删除数红色
                        (style, f" {path}\n"),
                    ]
                )
            parts.append(("", "\n"))

        def file_fragments(parts: StyleAndTextTuples, sections: list[tuple[str, str, str]]) -> None:
            state.clamp_file(len(sections))
            status, path, diff = sections[state.file]
            parts.append(("", "\n"))
            parts.append(("ansicyan", f"  {status.title()} · {path}\n"))
            lines = self.ui.segment_lines(self.ui.diff_segments_live(diff))
            visible = state.view.visible(lines, viewport())
            for line in visible:
                parts.extend(line)
            if not visible or not visible[-1] or not visible[-1][-1][1].endswith("\n"):
                parts.append(("", "\n"))

        def fragments() -> StyleAndTextTuples:
            parts: StyleAndTextTuples = [("", "\n")]
            parts.extend(self.ui.tab_segments(state.view.titles, state.view.tab))
            parts.append(("", "\n"))

            sections = active_sections()
            if not sections:
                parts.append(("class:choice.disabled", "  No diffs\n"))
            elif state.mode is DiffViewState.Mode.LIST:
                list_fragments(parts, sections)
            else:
                file_fragments(parts, sections)
            mode_hint = "list" if state.mode is DiffViewState.Mode.LIST else "diff"
            if state.mode is DiffViewState.Mode.LIST:
                hint = "↑/↓ or j/k move · ←/→ or h/l tab · Enter open · r refresh · Esc/q close"
            else:
                hint = "↑/↓ scroll · Ctrl-U/D half-page · PgUp/PgDn page · Esc/← back · r refresh · q close"
            position = f"{state.file + 1 if sections else 0}/{len(sections)}"
            parts.append(("class:choice.disabled", f"\n  [{mode_hint}] {hint} [{position}]\n"))
            return parts

        if self.tui is None:
            return

        def modal_key(key: str, _data: str) -> Any:
            nonlocal model
            result = state.handle_key(key, len(active_sections()), viewport())
            if result is DiffViewState.REFRESH:
                model = build_model()  # r 键:重建模型数据
                return TUI_MODAL_PENDING
            return result

        self.tui.show_modal(fragments, modal_key, exclusive=True)

    def config(self, args: str) -> str:
        # 展示全部配置键值;builtin_tools 按"解析后的实际状态"呈现(可能因 wire 协议无效)。
        provider = self.session.config.provider
        resolved = provider.resolve()
        configured_builtin_tools = ", ".join(str(entry.get("type") or "?") for entry in provider.builtin_tools) or "(off)"
        builtin_issue = builtin_tools_issue(resolved, provider.builtin_tools)
        if not provider.builtin_tools:
            resolved_builtin_tools = "(off)"
        elif builtin_issue is None:
            resolved_builtin_tools = "active: " + configured_builtin_tools
        elif builtin_issue.reason == "wire":
            resolved_builtin_tools = f"inactive on {resolved.api}: {configured_builtin_tools}"  # 当前 wire 协议不支持
        else:
            resolved_builtin_tools = "invalid: " + ", ".join(builtin_issue.configured)  # 配置本身非法
        return "\n".join(
            [
                f"provider.active: {self.session.config.active_provider}",
                f"provider.available: {', '.join(sorted(self.session.config.providers))}",
                f"provider.url: {provider.url or '(empty)'}",
                f"provider.key: {'(set)' if provider.key else '(empty)'}",
                f"provider.model: {provider.model or '(empty)'}",
                f"provider.api: {provider.api}",
                f"provider.stream: {'on' if provider.stream else 'off'}",
                f"provider.image_input: {provider.image_input}",
                f"provider.resolved_api: {resolved.api}",
                f"provider.prompt_cache_key: {provider.prompt_cache_key}",
                f"provider.available_models: {', '.join(provider.available_models) or '(empty)'}",
                f"provider.reasoning: {provider.reasoning}",
                f"provider.resolved_reasoning_effort: {resolved.reasoning_effort or '(off)'}",
                f"provider.resolved_chat_reasoning: {resolved.chat_reasoning}",
                f"provider.chat_reasoning: {provider.chat_reasoning}",
                f"provider.temperature: {provider.temperature if provider.temperature is not None else '(off)'}",
                f"provider.max_tokens: {provider.max_tokens or '(server default)'}",
                f"provider.strict_tools: {provider.strict_tools} (active {resolved.strict_tools_active})",
                f"provider.extra_body: {json.dumps(provider.extra_body, ensure_ascii=False, sort_keys=True) if provider.extra_body else '(off)'}",
                f"provider.builtin_tools: {configured_builtin_tools}",
                f"provider.resolved_builtin_tools: {resolved_builtin_tools}",
                f"provider.timeout: {provider.timeout}",
                f"provider.response_timeout: {provider.response_timeout or '(off)'}",
                f"paths.data_dir: {self.session.data_path()}",
                f"runtime.shell_timeout: {self.session.settings.shell_timeout}",
                f"runtime.max_agent_steps: {self.session.settings.max_steps}",
                f"runtime.max_context_tokens: {self.session.settings.max_context_tokens}",
                f"runtime.max_parallel_tools: {self.session.settings.max_parallel_tools}",
                f"runtime.session_retention_days: {self.session.settings.session_retention_days}",
                f"runtime.yolo: {'on' if self.session.settings.yolo else 'off'}",
            ]
        )

    def sessions_command(self, args: str) -> str | None:
        """浏览已保存的会话并重新进入一个。`/sessions all` 扩大到本项目之外。"""
        argument = args.strip().lower()
        if argument not in {"", "all"}:
            return "Usage: /sessions [all]"
        entries = SessionSnapshotStore.list_sessions(self.session.config.data_dir, self.session.cwd, all_projects=argument == "all")
        if not entries:
            return "No saved sessions yet."
        labels = {entry.uid: self.session_label(entry, all_projects=argument == "all") for entry in entries}
        if self.tui is None or not self.interactive_input:
            return "\n".join(f"{entry.uid}  {labels[entry.uid]}" for entry in entries)  # 非交互:纯文本列表
        title = "Sessions" + (" · all projects" if argument == "all" else "")
        # 预览每帧都渲染,因此它读的是手里已有的列表,而不是存储。
        by_uid = {entry.uid: entry for entry in entries}
        chosen = self.choice_application(
            title, tuple(entry.uid for entry in entries), labels, self.session.uid, set(), preview_fn=lambda uid: self.session_preview(by_uid.get(uid))
        )
        if not isinstance(chosen, str) or chosen == self.session.uid:
            return None  # 没选或选了自己:不切换
        self.resume_request = chosen  # 设置移交目标:本次运行结束后 main 会接管
        self.save_and_emit_resume()
        return None

    def session_label(self, entry: SessionEntry, *, all_projects: bool = False) -> str:
        rounds = f"{entry.rounds} round" + ("s" if entry.rounds > 1 else "") if entry.rounds else "no turns"
        parts = [Text.age(time.time() - entry.updated_at), rounds]
        if all_projects and entry.cwd:
            parts.append(os.path.basename(entry.cwd.rstrip(os.sep)) or entry.cwd)  # 跨项目视图:附上项目目录名
        if entry.uid == self.session.uid:
            parts.append("current")  # 当前会话打标
        return f"{entry.label()}  ·  " + " · ".join(parts)

    def session_preview(self, entry: SessionEntry | None) -> str:
        if entry is None:
            return ""
        return "\n".join([f"uid   {entry.uid}", f"start {entry.opening or '(no message)'}", f"where {entry.cwd or '(unknown)'}"])

    def name_command(self, args: str) -> str:
        """显示或设置会话名称——之后 `--resume` 可以用它代替 uid。"""
        text = args.strip()
        if not text:
            current = self.session.name
            source = {"user": "set by you", "goal": "from the current goal", "input": "from the opening message"}
            described = source.get(self.session.state.name_source, "")
            return f"Session name: {current} ({described})" if current and described else f"Session name: {current or '(unnamed)'}"
        name = self.session.rename(text)
        self.session.save_snapshot()  # 改名立即落盘
        return f"Session named: {name}\nResume with: yucode --resume {shlex.quote(name)}"  # 名字加引号,防 shell 转义问题

    def compact(self, args: str) -> str:
        if args.strip():
            return "Usage: /compact"
        before = len(self.session.messages)
        compacted, keep = self.agent.context.compaction_parts()
        if not compacted:
            return "No prior conversation to compact"  # 没有可压缩的历史
        fallback = False
        self.status_bar.begin()
        if self.tui is not None:
            self.tui.set_running("compacting context")  # TUI 标题显示压缩阶段
        else:
            self.status_bar.start(reset=False)
        try:
            data = self.agent.model.compact(self.agent.context.compaction_input(compacted))
        except KeyboardInterrupt:
            return "Cancelled"
        except Exception:  # noqa: BLE001 - 手动压缩与自动压缩走同一条确定性兜底路径
            # 模型压缩失败:回退到确定性的裁剪压缩(与自动压缩同一兜底)。
            self.agent.context.apply_compaction(None, keep, fallback_note=PREVIOUS_CONTEXT_TRIMMED, compacted=compacted)
            fallback = True
            data = None
        finally:
            if self.tui is not None:
                self.tui.set_dispatching()
            else:
                self.status_bar.stop()
        if data is not None:
            self.agent.context.apply_compaction(data, keep, compacted=compacted)
        self.agent.context.update_current_tokens(SYSTEM_PROMPT)
        # 压缩就地改写历史。立刻落盘:若直接离开会话而不跑下一个回合,
        # 下次恢复会从压缩前的日志状态开始。
        self.session.save_snapshot()
        fallback_note = " (fallback)" if fallback else ""
        return (
            f"Compacted context: messages {before} -> {len(self.session.messages)}, "
            f"prior summary inserted, ctx {self.session.state.context_percent}%{fallback_note}"
        )

    def index(self, args: str) -> str:
        value = args.strip()
        if value not in {"", "force"}:
            return "Usage: /index [force]"
        try:
            self.status_bar.start()
            return CodeIndex(self.session).sync(force=value == "force")  # force 强制全量重建
        finally:
            self.status_bar.stop()

    def provider(self, args: str) -> str:
        parts = args.split()
        if len(parts) > 1:
            return "Usage: /provider [NAME]"
        if parts:
            return self.set_provider(parts[0])  # 带参数:直接切换
        choices = tuple(sorted(self.session.config.providers))
        summary = "provider: " + self.session.config.active_provider + "\nproviders: " + ", ".join(choices)
        current = self.session.config.active_provider
        choice = self.select_choice("Provider", choices, labels={current: current + " (current)"}, current=current)
        if not isinstance(choice, str):
            return "No change" if choice is SELECTION_BACK else summary  # Esc 显示"未改动";其他异常显示摘要
        provider_result = self.set_provider(choice)
        model_result = self.model("")  # 切换 provider 后顺手让用户重选模型
        return provider_result + ("\n" + model_result if model_result else "")

    def set_provider(self, name: str) -> str:
        if name not in self.session.config.providers:
            return "Unknown provider: " + name
        self.session.config.active_provider = name
        return "Set provider = " + name

    def model(self, args: str) -> str:
        parts = args.split()
        if len(parts) > 1:
            return "Usage: /model [MODEL]"
        if parts:
            result = self.set_model(parts[0])
            return "No change" if result is SELECTION_BACK else str(result)
        provider = self.session.config.provider
        configured = tuple(dict.fromkeys(provider.available_models))  # 去重保序
        tui = self.tui
        show_loading = tui is not None and bool(provider.url and provider.key)  # 有端点才值得拉远端列表
        if show_loading and tui is not None:
            tui.set_dispatching("Loading models...")  # 远端发现期间显示加载态
        try:
            remote = tuple(model for model in self.remote_models(provider) if model not in configured)
        finally:
            if show_loading and tui is not None:
                tui.set_dispatching()  # 恢复默认标题
        choices: list[str] = []
        if configured:
            choices.extend((self.MODEL_CONFIGURED_LABEL, *configured))
        if remote:
            choices.extend((self.MODEL_DISCOVERED_LABEL, *remote))
        choice_values = tuple(choices)
        if not choice_values:
            return "Current provider.model is " + (self.session.config.provider.model or "(empty)")
        while True:
            current = self.session.config.provider.model
            labels = {label: label for label in self.MODEL_LABELS if label in choice_values}
            labels.update({current: current + " (current)"} if current in choice_values else {})
            choice = self.select_choice("Model", choice_values, labels=labels, current=current, disabled=self.MODEL_LABELS)
            if choice is SELECTION_BACK:
                return "No change"
            if not isinstance(choice, str):
                return "Current provider.model is " + (self.session.config.provider.model or "(empty)")
            if choice in self.MODEL_LABELS:
                continue  # 选到分组标题:忽略,继续选
            result = self.set_model(choice, back_to_model=True)
            if result is SELECTION_BACK:
                continue  # 设置过程中 Esc:回到模型列表重选
            return str(result)

    def remote_models(self, provider: ProviderConfig) -> tuple[str, ...]:
        if not provider.url or not provider.key:
            return ()  # 没有端点与 key,就无可发现的模型
        try:
            # 惰性导入:/model 发现是这里唯一的 openai SDK 用法,让它留在启动路径之外
            from openai import OpenAI

            page = OpenAI(
                api_key=provider.key,
                base_url=provider.resolve().base_url,
                timeout=min(provider.timeout, 10),  # 发现请求最多等 10 秒,不拖住交互
                max_retries=0,  # 失败就失败,不重试:可选的发现功能
                default_headers={"User-Agent": HTTP_USER_AGENT},
            ).models.list()
        except Exception:  # noqa: BLE001 - 远端模型发现是可选的,provider SDK 的失败形态五花八门
            return ()  # 发现失败不阻塞 /model:返回空列表
        names = []
        for item in getattr(page, "data", page) or []:
            name = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)  # 兼容 dict 与对象两种分页形态
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(sorted(dict.fromkeys(names)))  # 去重排序

    def set_model(self, model: str, *, back_to_model: bool = False) -> str | object:
        # 依次确认 api 与 reasoning;任一环节 Esc 都可放弃(或返回模型列表)。
        while True:
            api = self.select_api(model)
            if api is SELECTION_BACK:
                return SELECTION_BACK if back_to_model else "No change"  # Esc:回模型列表或放弃
            reasoning = self.select_reasoning()
            if reasoning is not SELECTION_BACK:
                break  # reasoning 未取消:api 与 reasoning 都确定下来
        provider = self.session.config.provider
        provider.model = model
        lines = ["Set provider.model = " + model]
        if isinstance(api, str):
            lines.append(self.set_api(api))
        if isinstance(reasoning, str):
            provider.reasoning = reasoning
            lines.append("Set provider.reasoning = " + reasoning)
        return "\n".join(lines)

    def reason(self, args: str) -> str:
        value = args.strip()
        if value:
            if value not in REASONING_CHOICES:
                return "Usage: /reason " + "|".join(REASONING_CHOICES)  # 非法值:给出用法
            self.session.config.provider.reasoning = value
            return "Set provider.reasoning = " + value
        choice = self.select_reasoning()  # 无参数:走选择器交互
        if isinstance(choice, str):
            self.session.config.provider.reasoning = choice
            return "Set provider.reasoning = " + choice
        return "No change"

    def api(self, args: str) -> str:
        value = args.strip()
        provider = self.session.config.provider
        if value:
            if value not in PROVIDER_API_CHOICES:
                return "Usage: /api " + "|".join(PROVIDER_API_CHOICES)
            return self.set_api(value)
        choice = self.select_api(provider.model)  # 无参数:走选择器交互
        return self.set_api(choice) if isinstance(choice, str) else "No change"

    def set_api(self, value: str) -> str:
        provider = self.session.config.provider
        provider.api = value
        # "auto" 是常见选择,所以回报解析出的实际 wire 协议,而不是把设置原样回显。
        resolved = provider.resolve()
        result = f"Set provider.api = {value} (wire: {resolved.api})"
        issue = builtin_tools_issue(resolved, provider.builtin_tools)
        if issue is not None:
            if issue.reason == "wire":
                result += f"; builtin_tools inactive on {resolved.api}"  # 该 wire 协议不支持内置工具
            else:
                result += "; unsupported builtin_tools: " + ", ".join(issue.configured)
        return result

    def yolo(self, args: str) -> str:
        self.session.settings.yolo = not self.session.settings.yolo  # 翻转原子标志,agent 在下次审批时读取
        return "yolo: " + ("on" if self.session.settings.yolo else "off")

    def hints(self, args: str) -> str:
        self.session.settings.quick_hints = not self.session.settings.quick_hints  # 开关 quick hints 提示
        return "quick hints: " + ("on" if self.session.settings.quick_hints else "off")

    def strict(self, args: str) -> str:
        if args:
            return "Usage: /strict"
        provider = self.session.config.provider
        provider.strict_tools = not provider.strict_tools
        state = "on" if provider.strict_tools else "off"
        if provider.strict_tools:
            resolved = provider.resolve()
            if not resolved.strict_tools_active:
                # 打开了但当前 provider 不支持:明确告知,避免用户以为已生效。
                return f"strict_tools: {state} (inactive: {resolved.host or 'this provider'} does not support strict tool calling)"
        return f"strict_tools: {state}"

    def set_value(self, args: str) -> str:
        key, _, value = args.partition(" ")
        if not key or not value:
            return "Usage: /set KEY VALUE"
        handler = SET_HANDLERS.get(key)
        if handler is None:
            return "Unknown config key: " + key
        target_name, attr, coerce = handler
        choices = SET_CHOICES.get(key)
        if choices is not None and value not in choices:
            return "Invalid value for " + key  # 封闭取值集合:先校验再写
        obj = self.session.config.provider if target_name == "provider" else self.session.settings
        try:
            if coerce is not None:
                value = coerce(value)  # 类型转换兼边界约束(如 max(1, int(v)))
            setattr(obj, attr, value)
        except (ConfigError, ValueError):
            return "Invalid value for " + key  # 转换失败 = 非法值
        return "Set " + key


class TuiRuntime:
    """在 CommandLoop 拥有会话行为的同时,拥有交互会话的时间线。"""

    def __init__(self, command_loop: CommandLoop):
        self.loop = command_loop
        self.pending: queue.Queue[UserInput] = queue.Queue()  # TUI 线程 -> agent 线程的输入管道
        self.stop = threading.Event()  # 退出信号
        self.cancel_pending = threading.Event()  # 取消已请求(去重防重复)
        self.main_busy = threading.Event()  # agent 是否正在跑回合(决定 Ctrl-C 是否要 SIGINT)
        self.force_exit_timer: threading.Timer | None = None  # 强制退出兜底定时器
        self.error: BaseException | None = None  # TUI 线程的失败记到这里,在主线程抛出

    @property
    def tui(self) -> TuiApp:
        assert self.loop.tui is not None
        return self.loop.tui

    def _interrupt_active(self, cancel: Callable[[], None]) -> None:
        # 取消走独立线程,防止取消逻辑本身阻塞 UI 线程;agent 线程若在忙,
        # 还需要 SIGINT 打断它阻塞中的系统调用(如等待模型响应)。
        threading.Thread(target=cancel, daemon=True).start()
        if self.main_busy.is_set():
            os.kill(os.getpid(), signal.SIGINT)

    def interrupt(self) -> None:
        if self.cancel_pending.is_set():
            return  # 已在取消流程中:忽略重复请求
        self.cancel_pending.set()
        self.tui.set_running("cancelling")  # 输入框提示切换到取消状态
        self._interrupt_active(self.loop.agent.cancel)

    def _request_model_retry(self) -> None:
        state = self.loop.session.state
        if state.current_model_call_started_at <= 0 or state.manual_model_retry_requested:
            return  # 没有在途请求,或已在重试:忽略
        state.manual_model_retry_requested = True  # 防抖:一个回合只允许一次手动重试
        state.model_retry_count += 1
        self.tui.invalidate()
        self._interrupt_active(self.loop.agent.model.cancel)  # 中断当前请求,让模型层自动重发

    def submit_running(self, value: str | UserInput) -> None:
        value = value if isinstance(value, UserInput) else UserInput(value)
        text = str(value).strip()
        if not text:
            return  # 空白输入忽略
        if not value.images and "\n" not in text and text.startswith("/"):
            # 运行中的单行斜杠命令:走白名单的只读命令线程(不打断 agent)。
            threading.Thread(target=self.loop.run_queued_command, args=(text,), daemon=True).start()
        else:
            self.loop.session.enqueue_user_input(value)  # 普通输入:排队等下一个请求认领
            self.loop.session.save_snapshot()
        self.tui.invalidate()

    def recall(self) -> str | UserInput:
        # 回忆最近一条排队输入;若它已随请求发出,先触发模型重试。
        return self.loop.recall_pending_input(self._request_model_retry)

    def expand_output(self) -> None:
        # Ctrl-O:Bash 输出查看器放在后台线程,避免阻塞 TUI 渲染。
        threading.Thread(target=self.loop.bash_output_viewer, name="bash-output", daemon=True).start()

    def request_exit(self) -> None:
        self.stop.set()
        self.loop.save_and_emit_resume()  # 退出前保存会话并打印恢复命令

    def force_exit(self) -> None:
        self.stop.set()
        threading.Thread(target=self.loop.agent.cancel, daemon=True).start()
        # 兜底:1 秒后 SIGTERM;正常取消路径走完时定时器会在 run() 的 finally 里取消。
        self.force_exit_timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.force_exit_timer.daemon = True
        self.force_exit_timer.start()
        os.kill(os.getpid(), signal.SIGINT)  # 立刻 SIGINT:让阻塞中的 agent 线程退出

    def build_tui(self) -> TuiApp:
        # 把 TUI 的各种回调接到 TuiRuntime/CommandLoop 上:TuiApp 只负责渲染与按键。
        return TuiApp(
            on_chat_submit=self.pending.put,
            on_running_submit=self.submit_running,
            on_exit_request=self.request_exit,
            on_force_exit=self.force_exit,
            on_interrupt=self.interrupt,
            on_retry=self._request_model_retry,
            on_recall=self.recall,
            on_expand_output=self.expand_output,
            status_fragments_fn=lambda: self.loop.status_bar.display_fragments(active=self.tui.input_mode == "running"),
            activity_fragments_fn=self.loop.tui_activity_fragments,
            input_hint_fn=self.loop.tui_input_hint,
            quick_hints_fn=lambda: self.loop.session.quick_hints if self.loop.session.settings.quick_hints else (),
            editor_context_fn=self.loop.editor_context,
            images=self.loop.session.images,
            history=self.loop.input_history,
            completer=self.loop.input_completer,
        )

    def submit_next(self, entered: Sequence[str | UserInput]) -> None:
        if not entered:
            return
        first = entered[0] if isinstance(entered[0], UserInput) else UserInput(entered[0])
        self.pending.put(first)  # 第一条进 pending 队列:保持原有顺序
        for text in entered[1:]:
            self.loop.session.enqueue_user_input(text)  # 其余排队给 agent

    def reset_turn(self) -> None:
        self.loop.model_stream_output("", "")  # 清掉流预览
        # 请求可能在"永久提升之后、Agent 重新发布文本并消费标记之前"失败。
        # 绝不能让这个过期的标记抑制一条内容相同的后续响应。
        with self.loop.model_stream_lock:
            self.loop.model_stream_promoted_text = ""
        self.tui.set_idle()
        self.cancel_pending.clear()  # 回合重置:下次打断重新可用
        self.main_busy.clear()  # 主线程恢复空闲:之后 Ctrl-C 不再需要 SIGINT

    def dispatch(self, user_input: str | UserInput) -> bool:
        """分发一条输入。当它被完全当作命令处理时返回 true。"""
        user_input = user_input if isinstance(user_input, UserInput) else UserInput(user_input)
        self.loop.ui.emit_answer(user_input.display_text(), role="user", rule=False)  # 先回显用户输入
        try:
            handled, exit_now = self.loop.command(user_input.strip())
        except (KeyboardInterrupt, YucodeError) as error:
            self.loop.emit("Cancelled" if isinstance(error, KeyboardInterrupt) else f"Error: {error}")
            self.submit_next(self.loop.take_pending_inputs())  # 命令中断:排队输入不滞留
            self.reset_turn()
            return True
        if exit_now:
            self.stop.set()
            self.main_busy.clear()
            self.tui.exit()
            return True
        if handled:
            # 命令不能滞留排队的跟进输入:像 run_agent_turn 那样刷掉它们,让它们在命令
            # 完成后继续链式执行(例如 /compact 之后的排队输入)。
            # 在恢复空闲提示符之前提交——之后新输入会进入 `pending`。
            self.submit_next(self.loop.take_pending_inputs())
            self.reset_turn()
            return True
        return False  # 未处理:调用方去跑 agent 回合

    def run_agent_turn(self, user_input: str | UserInput) -> None:
        user_input = user_input if isinstance(user_input, UserInput) else UserInput(user_input)
        self.loop.emit("")
        self.loop.status_bar.begin()
        self.tui.set_running("working")
        started = time.monotonic()
        cancelled = False
        malformed_tool_call = False
        turn_completed = False
        promoted_answer = ""
        try:
            answer = self.loop.agent.run(user_input)
            turn_completed = True
        except KeyboardInterrupt:
            self.submit_next(self.loop.take_pending_inputs())  # 取消时也要把排队输入交回队列
            answer = ""
            cancelled = True
        except MalformedToolCallError as error:
            answer = str(error)
            malformed_tool_call = True
        except YucodeError as error:
            answer = f"Error: {error}"
        finally:
            # 在 reset_turn 清掉标记之前先快照流提升标记:终止性的 NextHints 批次会像任何
            # 工具批次一样把答案提升进 scrollback,但没有任何东西再经 agent_output 重新发布
            # 它——没有这一步,下面的最终 emit 会把它再打印一遍。
            with self.loop.model_stream_lock:
                promoted_answer = self.loop.model_stream_promoted_text
            self.reset_turn()
            self.loop.session.state.manual_model_retry_requested = False  # 回合结束:重置重试防抖
            CodeIndex(self.loop.session).update_pending_async()
        if cancelled:
            self.loop.emit("Cancelled")
            return
        if remaining := self.loop.unpromoted_text(answer, promoted_answer):
            if self.loop.ui.color:
                self.loop.emit()
            self.loop.ui.emit_answer(remaining, rule=False)
        # 在提升检查之外发出:被提升的答案已经在 scrollback 里却没有来源页脚,
        # 如果这里跳过页脚,恰恰会在"确实跑过搜索"的时候丢掉它。
        if footer := search_sources_footer(self.loop.agent.turn_sources):
            self.loop.ui.emit_answer(footer, rule=False)
        if not malformed_tool_call:
            self.loop.ui.emit_turn_end(started)
        self.loop.session.save_snapshot()
        if turn_completed:
            # reset_turn above made the completed answer render cleanly. Memory organization is
            # still synchronous main-thread work, so restore busy state while it runs: Ctrl-C
            # must cancel the organizer without being mistaken for an idle-input cancellation.
            self.main_busy.set()
            self.loop.organize_memory_after_turn()
        self.reset_turn()  # 整理期间的 Ctrl-C 只取消整理；清掉它，不能污染下一条排队输入。
        self.submit_next(self.loop.take_pending_inputs())  # 回合结束:让排队输入继续链式执行

    def run_agent_loop(self) -> None:
        while not self.stop.is_set():
            try:
                user_input = self.pending.get(timeout=0.1)  # 带超时轮询:同时响应 stop 事件
            except queue.Empty:
                continue
            self.main_busy.set()  # 进入忙碌态
            # 用户行动了:丢掉上一回合的提示(也覆盖跳过 Agent.run 的斜杠命令)。
            self.loop.session.clear_quick_hints()
            if self.cancel_pending.is_set():
                # 取消在排队时就已请求:不执行这条输入,直接重置回合。
                self.loop.emit("Cancelled")
                self.reset_turn()
                continue
            if not self.dispatch(user_input):
                self.run_agent_turn(user_input)

    def run_tui_app(self) -> None:
        try:
            self.tui.run(style=self.loop.style())
        except BaseException as error:  # noqa: BLE001 - 把 TUI 线程的每个失败都带到主线程抛出
            self.error = error
            self.stop.set()

    def run(self) -> int:
        """agent 跑在主线程,prompt-toolkit 跑在一个被 join 的 UI 线程上。"""
        self.loop.tui = self.build_tui()
        tui_thread = threading.Thread(target=self.run_tui_app, name="tui")
        tui_thread.start()
        try:
            self.tui.ready.wait()  # 等 TUI 就绪(如 patch_stdout 接管终端)
            if self.error is not None:
                raise self.error  # TUI 启动失败:立刻在主线程暴露
            # 只有 patch_stdout 拥有终端之后才输出启动与恢复转录行,
            # 让主屏应用把它们放进原生终端/tmux scrollback。
            self.loop.start_session()
            self.submit_next(self.loop.take_pending_inputs())
            self.run_agent_loop()
        finally:
            self.stop.set()
            if self.force_exit_timer is not None:
                self.force_exit_timer.cancel()  # 正常退出:取消 SIGTERM 兜底定时器
            self.tui.exit()
            # 不要让解释器终结流程与正在刷 stdout 的 TUI 线程赛跑。
            # 真正卡死的应用仍由紧急 force-exit 定时器负责终结。
            tui_thread.join()
            try:
                self.loop.close_background_output()
            finally:
                self.loop.tui = None
        if self.error is not None:
            raise self.error
        return 0
