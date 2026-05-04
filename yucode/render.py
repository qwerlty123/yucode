"""yucode 的终端渲染、实时输出与状态显示。"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, ClassVar

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI, FormattedText, StyleAndTextTuples
from prompt_toolkit.output import create_output
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.text import Text as RichText

from yucode.base import (
    MODEL_REQUEST_RETRIES,
    Json,
    LogBlock,
    LogEdge,
    LogLine,
    LogRole,
    Text,
    __version__,
)
from yucode.session import Session
from yucode.tools import CodeIndex

if TYPE_CHECKING:
    from pygments.style import Style as PygmentsStyle

try:
    import pygments
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
    from pygments.styles import get_style_by_name
    from pygments.token import Token
except ImportError:  # pragma: no cover - 可选的高亮依赖,缺失时全部降级为 None
    pygments = Token = None
    get_lexer_by_name = get_lexer_for_filename = get_style_by_name = None


def markdown_table(headers: list[str], rows: list[tuple]) -> str:
    """把表头与行拼成 GitHub 风格的 markdown 表格字符串。"""

    def cell(value: object) -> str:
        # 单元格内:换行压成空格、竖线转义,避免破坏表格结构
        return Text.clean(str(value)).replace("\n", " ").replace("|", "\\|")

    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(cell(value) for value in row) + " |" for row in rows),
        ]
    )


MAX_RENDERED_SOURCES = 10  # 来源列表最多渲染 10 条,防止长搜索轮次刷屏


def search_sources_footer(sources: list[Json]) -> str:
    """本轮 provider 端搜索来源的 markdown 列表;无来源时返回 ""。

    这纯属展示层。来源始终留在承载它们的消息上,因此进入历史记录的答案就是模型
    写下的原文,下一轮也不会向 provider 重放任何新内容。"""
    # dict.fromkeys 去重并保持首次出现顺序;跳过无 url 的条目
    seen = dict.fromkeys(url for source in sources if isinstance(source, dict) and (url := str(source.get("url") or "")))
    if not seen:
        return ""  # 没有来源就不输出任何东西
    shown = list(seen)[:MAX_RENDERED_SOURCES]  # 超长列表截断
    # 去掉协议前缀与尾部斜杠,压缩成单行展示。
    lines = [f"{index}. {url.split('://', 1)[-1].rstrip('/')}" for index, url in enumerate(shown, start=1)]
    if len(seen) > len(shown):
        lines.append(f"…and {len(seen) - len(shown)} more")  # 被截断时追加"还有 N 条"
    return "\n".join(["", "**Sources**", "", *lines])


class Theme:
    DARK: ClassVar[dict[str, str]] = {
        "diff.added.bg": "bg:#003b00",
        "diff.added.fg": "fg:default",
        "diff.removed.bg": "bg:#520000",
        "diff.removed.fg": "fg:default",
        "syntax.assign": "fg:#79c0ff",
        "syntax.string": "fg:#a5d6ff",
        "syntax.number": "fg:#d2a8ff",
        "syntax.ident": "fg:#a5d6ff",
        "syntax.builtin": "fg:#79c0ff",
        "syntax.default_hex": "e6edf3",
        # 状态行位于对话下方,应当读作安静的 footer 而不是与内容争抢注意,
        # 因此它的朴素色调保持在纯白之下。
        "status.base": "#cbd5e1",
        "status.sep": "#4b5563",
        "status.provider": "#cbd5e1",
        "status.sweep.start": "#4f9fc4",
        "status.sweep.end": "#9b82c9",
        "status.sweep.crest": "#cfe6f2",
        "status.reason": "#a5b4fc",
        "status.mcp": "#93c5fd",
        "status.ctx": "#facc15",
        "status.update": "#fb923c",
        "status.index": "#94a3b8",
        "status.warn": "#fb7185",
        "divider.glow": "#67e8f9",
        "divider.rule": "#4b5563",
        "user.log": "#e0a96d",
        "pygments": "github-dark",
    }

    LIGHT: ClassVar[dict[str, str]] = {
        "diff.added.bg": "bg:#d1f0d1",
        "diff.added.fg": "fg:#003b00",
        "diff.removed.bg": "bg:#f5c8c8",
        "diff.removed.fg": "fg:#520000",
        "syntax.assign": "fg:#005cc5",
        "syntax.string": "fg:#032f62",
        "syntax.number": "fg:#6f42c1",
        "syntax.ident": "fg:#032f62",
        "syntax.builtin": "fg:#005cc5",
        "syntax.default_hex": "24292e",
        "status.base": "#4b5563",
        "status.sep": "#9ca3af",
        "status.provider": "#4b5563",
        # 浅色终端上,波峰是画面中最深的点:让流动光带读作高亮靠的是对比度而非亮度。
        "status.sweep.start": "#3b7ea3",
        "status.sweep.end": "#6b52a3",
        "status.sweep.crest": "#1f2937",
        "status.reason": "#5b21b6",
        "status.mcp": "#1e40af",
        "status.ctx": "#a16207",
        "status.update": "#9a3412",
        "status.index": "#475569",
        "status.warn": "#b91c1c",
        "divider.glow": "#0e7490",
        "divider.rule": "#9ca3af",
        "user.log": "#9a5b2e",
        "pygments": "default",
    }

    _mode: ClassVar[str] = "dark"  # 未检测前的默认主题;detect() 按环境覆盖
    _pygments_cache: ClassVar[dict[str, type[PygmentsStyle] | None]] = {}  # 样式对象只解析一次并缓存

    @classmethod
    def set_mode(cls, mode: str) -> None:
        cls._mode = "light" if mode == "light" else "dark"  # 只认 light/dark 两个有效值,其余一律 dark

    @classmethod
    def style(cls, key: str) -> str:
        return (cls.LIGHT if cls._mode == "light" else cls.DARK)[key]  # 键不存在会 KeyError,属开发期错误

    @classmethod
    def ramp(cls, start_key: str, end_key: str, steps: int) -> list[str]:
        """从调色板的一个条目到另一个,线性插值出 `steps` 个十六进制颜色。

        用于需要比调色板命名颜色更多色阶的渐变:让移动的高亮能在两个格子之间淡入淡出,
        而不是从一种命名颜色直接跳到下一种。"""
        start, end = cls.rgb(cls.style(start_key)), cls.rgb(cls.style(end_key))
        span = max(1, steps - 1)  # 区间数 = 点数 - 1;steps 为 1 时避免除零
        return [cls.mix(start, end, index / span) for index in range(steps)]

    @staticmethod
    def mix(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> str:
        # 各通道按比例线性插值并保留两位十六进制;strict=True 保证通道数恒为 3
        return "#" + "".join(f"{round(channel + (channel_end - channel) * ratio):02x}" for channel, channel_end in zip(start, end, strict=True))

    @staticmethod
    def rgb(color: str) -> tuple[int, int, int]:
        value = color.rpartition(":")[2].lstrip("#")  # 兼容 "fg:#rrggbb" 与 "#rrggbb" 两种写法
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    @classmethod
    def detect(cls) -> str:
        # COLORFGBG 形如 "fg;bg"(rxvt/urxvt/Konsole)或 "fg;;bg"(iTerm2)。只有标准
        # 白色条目(7 与 15)才可靠地表示浅色;索引 8 是亮黑,必须仍按深色处理。
        fgbg = os.environ.get("COLORFGBG", "")
        if ";" in fgbg:  # 变量未设置或不含分号时无从判断,按深色处理
            with contextlib.suppress(ValueError):  # 非数字背景值(如 "default")解析失败时静默回退深色
                bg = int(fgbg.rsplit(";", 1)[1])
                return "light" if bg in {7, 15} else "dark"
        return "dark"

    @classmethod
    def resolve(cls, configured: str) -> str:
        configured = (configured or "auto").strip().lower()
        # "auto" 与非法值都回退到环境探测;显式 light/dark 直接采用
        return configured if configured in ("light", "dark") else cls.detect()

    @classmethod
    def pygments_style(cls) -> type[PygmentsStyle] | None:
        if pygments is None or get_style_by_name is None:
            return None  # pygments 未安装时整体禁用语法高亮
        name = cls.style("pygments")
        if name not in cls._pygments_cache:
            try:
                cls._pygments_cache[name] = get_style_by_name(name)
            except Exception:  # noqa: BLE001 - 可选样式解析失败降级为纯文本
                cls._pygments_cache[name] = None  # 缓存失败结果,后续调用不再重试
        return cls._pygments_cache[name]


class UiPrinter:
    """把已完成的输出渲染进终端原生 scrollback。

    这是终端边界上持久的那一半:它打印的内容在会话结束后依然存在,且能用终端自带的工具
    检索,因此这里绝不清屏。实时预览与状态属于 prompt-toolkit 应用。

    因为输出是永久的,所以要做净化而不是直接透传。Rich 会把每一行右填充到控制台宽度,
    这会把尾部空白烙进 scrollback,终端之后变窄时变成换行伪影,因此填充会被剥离——
    除非它带背景色、属于可见色带的一部分。prompt-toolkit 无法解析的终端控制串会预先
    剥离,因为它会丢掉这些串的框架却把载荷泄漏成可见垃圾。

    是否上色只判定一次,依据是输出是否通向真实终端。
    """

    MESSAGE_ROLE_STYLES: ClassVar[dict[str, str]] = {"user": "cyan bold", "assistant": "magenta bold"}  # 带 role 的消息的标签配色
    PROMPT_PREFIX: ClassVar[str] = "> "
    USER_LOG_PREFIX: ClassVar[str] = "• "  # 用户日志行的悬挂前缀
    MCP_STATUS_RE: ClassVar[re.Pattern[str]] = re.compile(r"● (connected|connecting|disconnected|disconnecting|error|skipped)")
    # 与 MCP_STATUS_RE 对应的 ANSI 颜色码:连接绿、断开黄、错误红、跳过灰
    MCP_STATUS_ANSI: ClassVar[dict[str, str]] = {
        "connected": "\x1b[32m",
        "connecting": "\x1b[32m",
        "disconnected": "\x1b[33m",
        "disconnecting": "\x1b[33m",
        "error": "\x1b[31m",
        "skipped": "\x1b[90m",
    }

    @classmethod
    def user_log_style(cls) -> str:
        return Theme.style("user.log")

    # 工具参数的词法切分:空白、引号字符串、key=、tr/job 编号、时间、分隔符、其余词
    TOOL_ARG_TOKEN: ClassVar[re.Pattern] = re.compile(
        r"""\s+|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_][\w.-]*=|(?:tr|job)\.\d+|\d+(?::\d+)?|[;,]|[^\s;,]+"""
    )

    def __init__(self, output_fn=print):
        self.output_fn = output_fn
        # 只对真实终端上色:管道/重定向时输出纯文本(测试注入的 output_fn 不算终端)
        self.color = output_fn is print and sys.stdout.isatty()

    def emit(self, text: str | LogBlock = "") -> None:
        if not self.color:
            self.output_fn(str(text))  # 无颜色走普通 print 路径,完全跳过样式处理
            return
        # LogBlock 按行+角色分派,普通字符串按前缀识别
        segments = self.log_segments(text) if isinstance(text, LogBlock) else self.segments(text)
        print_formatted_text(FormattedText(segments), end="", flush=True)  # 统一走 pt 输出,与 TUI 渲染不交错

    # Rich 会把每一行右填充空格到控制台宽度,以便背景色和 padding 填满整行。无颜色的
    # 填充会被烙进 scrollback,在更窄的终端上变成换行锯齿,因此要剥离;但带背景色的填充
    # (语法高亮的代码块、/diff 预览)必须保留,否则色块不再读作一个整体。这里按 token
    # 追踪 SGR 的背景状态,只剥离背景关闭状态下渲染的空白。
    SGR_RE: ClassVar[re.Pattern[str]] = re.compile(r"\x1b\[([0-9;]*)m")
    RECORD_TOKEN_RE: ClassVar[re.Pattern[str]] = re.compile(r"(?:tr|job)\.\d+|\d+(?::\d+)?")
    # OSC / APC / DCS / SOS / PM 是 prompt_toolkit 的 ANSI 解析器不认识的终端控制串。
    # 当它们混过 Rich 的输出时(OSC 8 超链接是历史罪魁,iTerm 图片转义 / Kitty 图形 /
    # shell 集成标记是潜在未来来源),pt 会吃掉 ESC 框架却把载荷泄漏成可见垃圾
    # (如 OSC 8 泄漏成 `8;id=…;https://…;;`)。预先剥离,让 pt 只见到 CSI 转义。
    # 代价是这些序列的合法用途(可点击超链接、内联图片)永远到不了终端 —— 但它们在经过
    # pt 时本来也不能工作;宁可干净也不可乱码。
    NON_CSI_ESCAPE_RE: ClassVar[re.Pattern[str]] = re.compile(r"\x1b[\]_PX^][^\x07\x1b]*(?:\x07|\x1b\\)")

    @classmethod
    def strip_unknown_escapes(cls, text: str) -> str:
        return cls.NON_CSI_ESCAPE_RE.sub("", text)

    @classmethod
    def strip_trailing_pad(cls, text: str) -> str:
        return "\n".join(cls._strip_line_pad(line) for line in text.split("\n"))

    @classmethod
    def _strip_line_pad(cls, line: str) -> str:
        tokens: list[tuple[str, str]] = []  # ("sgr"|"text", payload)
        bg_states: list[bool] = []  # 每个 token 渲染时背景是否激活
        bg, idx = False, 0
        for m in cls.SGR_RE.finditer(line):  # 逐段切分 SGR 转义与文本,记录渲染时刻的背景状态
            if m.start() > idx:
                tokens.append(("text", line[idx : m.start()]))
                bg_states.append(bg)
            tokens.append(("sgr", m.group(0)))
            bg_states.append(bg)
            for param in (m.group(1) or "0").split(";"):
                n = int(param) if param else 0
                if n == 0 or n == 49:
                    bg = False  # 重置(0)或背景复位(49)关闭背景
                elif 40 <= n <= 47 or 100 <= n <= 107 or n == 48:
                    bg = True  # 标准/亮色背景,或 48 开头的扩展背景
            idx = m.end()
        if idx < len(line):
            tokens.append(("text", line[idx:]))
            bg_states.append(bg)
        seen_content = False
        for i in range(len(tokens) - 1, -1, -1):  # 从行尾往回:只有末尾连续空白才是可剥的填充
            kind, payload = tokens[i]
            if kind == "sgr" or seen_content:
                continue  # 转义本身不处理;已见内容之后的空白不动
            if bg_states[i]:
                if payload.strip():
                    seen_content = True  # 色带内的空白是背景的一部分,保留
                continue
            stripped = payload.rstrip()  # 背景关闭的空白直接右剥
            if stripped != payload:
                tokens[i] = ("text", stripped)
            if stripped:
                seen_content = True
        return "".join(payload for _, payload in tokens)

    def emit_answer(self, text: str, *, role: str = "", rule: bool = True, indent: int = 0) -> None:
        if not self.color:
            if role == "user":
                # 无颜色路径:user 消息补换行+悬挂前缀,模拟着色路径的版式
                text, role = "\n" + self.USER_LOG_PREFIX + text, ""
            elif role == "assistant":
                role = ""  # 加粗前缀在纯文本路径下没有意义,只保留内容
            self.output_fn(self.indent_message(text, role, indent))
            return
        # 强制终端模式:无论 stdout 是否终端都生成 ANSI,再统一交给 pt 输出
        console = Console(force_terminal=True, color_system="truecolor", no_color=False, width=shutil.get_terminal_size().columns)
        with console.capture() as capture:  # 先渲染进内存,避免 Rich 直写 stdout 与 TUI 渲染交错
            self.render_message(console, text, role, rule, indent)
        cleaned = self.strip_unknown_escapes(self.strip_trailing_pad(capture.get()))  # 双净化:剥控制串、剥尾部填充
        print_formatted_text(ANSI(cleaned), end="", flush=True)

    # 标签放在一小段引线之后,而不是顶到第 0 列(Rich 的 `align="left"` 会把它推到最边缘,
    # 读起来像游离的标签而不是横线上的文字),也不居中:一条长破折线延伸到全宽,
    # 让分界线仍然从边到边地收束这一轮。
    TURN_END_LEAD: ClassVar[int] = 2

    def emit_turn_end(self, started_at: float) -> None:
        """用一条安静的、全宽的灰色分隔线收束本轮,并携带总耗时。

        这是动画版工作分割线的持久对应物:分割线在轮次运行期间计时,结束时被拆除,
        因此最终的耗时数值在这里定格。它复用 `elapsed_since`,让分隔线读起来像分割线的
        最后一帧(`5s`、`1m05s`),而不是旧的 `0m5s` / `1m5s` 格式。标签偏左(一小段
        破折号引线,接着标签,再延伸一条长破折线到全宽),前面空一行把分隔线与上方的
        回答隔开。"""
        label = f"done in {Text.elapsed_since(started_at)}"
        if not self.color:
            self.output_fn(label)  # 无颜色路径只打印文本标签,不画线
            return
        self.emit()  # 先输出空行,把分隔线与上方回答视觉分离
        width = shutil.get_terminal_size((80, 20)).columns
        lead = "─" * self.TURN_END_LEAD + " "
        trail = max(0, width - get_cwidth(lead) - get_cwidth(label) - 1)  # 尾线补满整行(减 1 预留行末空格);极窄终端为 0
        fragments = [
            ("ansibrightblack", lead),
            ("fg:default", label),
            ("ansibrightblack", " " + "─" * trail + "\n"),
        ]
        print_formatted_text(FormattedText(fragments), end="", flush=True)

    @staticmethod
    def indent_message(text: str, role: str = "", indent: int = 0) -> str:
        body = "\n".join(LogBlock.margin(indent) + line for line in text.splitlines() or [""])  # 空文本也输出一行缩进,行数稳定
        return f"{LogBlock.margin(indent)}{role}:\n{body}" if role else body  # 有角色时首行加 "role:" 前缀

    @classmethod
    def colorize_mcp_status(cls, text: str) -> str:
        # 把 "● connected" 之类的状态点替换成带颜色的 ANSI 版本(39m 复位前景色)
        return cls.MCP_STATUS_RE.sub(lambda match: cls.MCP_STATUS_ANSI[match.group(1)] + "●\x1b[39m " + match.group(1), text)

    def render_message(self, console: Console, text: str, role: str, rule: bool, indent: int) -> None:
        error = text.startswith(("Error:", "ConfigError:", "Unknown command:"))  # 错误前缀的消息不走 markdown,防格式意外
        styled_text = self.colorize_mcp_status(text) if role != "user" else text  # 用户消息不染状态点颜色
        if rule and not error:
            console.print(Rule(style="bright_black", characters="─"))  # 错误消息上方不画分隔线,减少干扰
        margin = LogBlock.margin(indent)
        if role == "user":
            console.print("")  # 用户消息前空一行,与上一轮输出分隔
            console.print(Padding(RichText(UiPrinter.USER_LOG_PREFIX + text, style=self.user_log_style()), (0, 0, 0, len(margin))))
        elif role == "assistant":
            # 错误用纯文本红色渲染;正常消息走 markdown。hyperlinks=False:pt 不识别 OSC 8 超链接,关闭防泄漏垃圾
            content = RichText(styled_text, style="red") if error else Markdown(styled_text, hyperlinks=False)
            console.print(Padding(content, (0, 0, 0, len(margin))))
        else:
            if role:
                label = RichText(role + ":", style=self.MESSAGE_ROLE_STYLES.get(role, "bright_black"))  # 未知角色回退灰标签
                console.print(Padding(label, (0, 0, 0, len(margin))))
            content = RichText(styled_text, style="red") if error else Markdown(styled_text, hyperlinks=False)
            console.print(Padding(content, (0, 0, 0, len(margin))))

    def emit_markdown(self, text: str) -> None:
        # 先把 markdown 渲染成 ANSI 字符串,再经 prompt-toolkit 输出。TUI 运行时直接打印
        # Rich 输出会让裸转义与它的渲染器交错;先捕获再以 ANSI 发射,保证所有终端输出
        # 都留在共享应用之内。
        if not self.color:
            self.emit(text)  # 无颜色时直接吐原文,跳过渲染管线
            return
        console = Console(force_terminal=True, color_system="truecolor", no_color=False, width=shutil.get_terminal_size().columns)
        with console.capture() as capture:
            console.print(Markdown(text, hyperlinks=False))
        cleaned = self.strip_unknown_escapes(self.strip_trailing_pad(capture.get()))
        print_formatted_text(ANSI(cleaned), end="", flush=True)

    @staticmethod
    def tab_segments(titles: tuple[str, ...], active: int) -> list[tuple[str, str]]:
        parts: list[tuple[str, str]] = []
        for index, title in enumerate(titles):
            parts.append(("class:tab.active" if index == active else "class:tab.inactive", f" {title} "))  # 当前标签高亮,其余淡化
            if index < len(titles) - 1:
                parts.append(("class:choice.disabled", " │ "))  # 分隔符只在标签之间,不在末尾
        return parts

    def segments(self, text: str) -> list[tuple[str, str]]:
        if text.startswith(("goal:", "check:", "plan:", "known:")):
            return self.memory_segments(text)  # 记忆块:按行内关键字配色
        if text.startswith(self.USER_LOG_PREFIX):
            prefix, content = self.USER_LOG_PREFIX, text[len(self.USER_LOG_PREFIX) :]
            return [(self.user_log_style(), prefix + content + "\n")]  # 用户日志行:悬挂前缀 + 主题色
        if text.startswith("+ "):
            return [("ansibrightblack", "+ "), ("fg:default", text[2:] + "\n")]  # 追加行:灰色加号提示
        if text.startswith("done in "):
            return [("ansibrightblack", text + "\n")]  # 轮次耗时行整体灰显,作收尾
        if text.startswith("yucode "):
            return [("ansicyan", text + "\n")]  # 版本横幅等元信息用青色
        if text.startswith(("Error:", "ConfigError:", "Unknown command:")):
            return [("ansired", text + "\n")]  # 错误前缀整行红色
        return [("fg:default", line + "\n") for line in text.splitlines() or [""]]  # 普通文本逐行输出;空文本也保证一行

    # 每种日志角色的 (标签色, 正文色) 组合
    LOG_STYLES: ClassVar[dict[LogRole, tuple[str, str]]] = {
        LogRole.TOOL: ("ansigreen", "fg:default"),
        LogRole.AUTO: ("ansiblue", "fg:default"),
        LogRole.META: ("ansibrightblack", "ansibrightblack"),
        LogRole.OUTPUT: ("ansibrightblack", "ansibrightblack"),
        LogRole.ERROR: ("ansired", "fg:default"),
        LogRole.MUTED: ("ansibrightblack", "ansibrightblack"),
        LogRole.DIFF: ("fg:default", "fg:default"),
    }

    def log_segments(self, block: LogBlock) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        width = max(1, shutil.get_terminal_size((120, 20)).columns - 1)  # 留一列,防自动换行打乱光标数学
        entries = list(block.walk())
        index = 0
        while index < len(entries):
            line, level = entries[index]
            if line.role is LogRole.DIFF:
                # diff 行按"连续同层级"合并成块,整块一起词法高亮,行间配色统一
                end = index + 1
                while end < len(entries) and entries[end][0].role is LogRole.DIFF and entries[end][1] == level:
                    end += 1
                diff_lines = [entry[0] for entry in entries[index:end]]
                sample_prefix = [("", block.margin(level)), *self.edge_segments(diff_lines[0].edge)]
                sample_prefix_width = sum(get_cwidth(fragment[1]) for fragment in sample_prefix)
                diff_row_width = max(1, width - sample_prefix_width)  # 高亮行宽 = 可用宽度减前缀
                diff_text = "\n".join(item.text for item in diff_lines)
                highlighted = self.segment_lines(self.diff_segments(diff_text, diff_row_width))  # 整块词法分析后按行切回
                for item, rendered in zip(diff_lines, highlighted):  # 高亮行与 diff 行一一对应
                    prefix = [("", block.margin(level)), *self.edge_segments(item.edge)]
                    rendered = self.remove_line_ending(rendered)
                    for row in Text.wrap_styled(prefix, prefix, rendered, width):
                        if item.text.startswith("+") and not item.text.startswith("+++"):
                            background = Theme.style("diff.added.bg")  # 真·新增行(排除 +++ 文件头)绿底
                        elif item.text.startswith("-") and not item.text.startswith("---"):
                            background = Theme.style("diff.removed.bg")  # 真·删除行(排除 --- 文件头)红底
                        else:
                            background = ""
                        if background:
                            used = sum(get_cwidth(fragment[1]) for fragment in row)
                            row.append((background, " " * max(0, width - used)))  # 背景补足整行,色块连续
                        segments.extend([*row, ("", "\n")])
                index = end
                continue
            label_style, text_style = self.LOG_STYLES[line.role]  # 按角色查 (标签色, 正文色)
            prefix = [("", block.margin(level)), *self.edge_segments(line.edge)]
            if line.label:
                prefix.append((label_style, line.label))
            content: list[tuple[str, str]] = []
            if line.text:
                separator = "  " if line.edge is LogEdge.NONE and line.label else " " if line.label else ""  # 无树线且带标签时双空格
                prefix.append((text_style, separator))
                content.extend(self.syntax_segments(line.text, line.syntax, text_style))  # 按声明的语法着色;syntax 为空降级纯文本
            if line.meta:
                content.append(("ansired" if line.role is LogRole.ERROR else "ansibrightblack", line.meta))  # 元信息:错误红、其余灰
            continuation = [("", block.margin(level) + " " * get_cwidth(line.text_prefix()))]  # 续行与首行文本前缀等宽对齐
            for row in Text.wrap_styled(prefix, continuation, content, width):
                segments.extend([*row, ("", "\n")])
            index += 1
        return segments

    @staticmethod
    def edge_segments(edge: LogEdge) -> list[tuple[str, str]]:
        return [] if edge is LogEdge.NONE else [("ansibrightblack", edge.value + " ")]  # 无树线返回空,避免多余空格

    @staticmethod
    def remove_line_ending(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
        result = list(segments)
        if result and result[-1][1].endswith("\n"):
            style, text = result[-1]
            result[-1] = (style, text[:-1])  # 去掉末尾换行,避免折行后出现空行
            if not result[-1][1]:
                result.pop()  # 剥离后为空片段则整体移除
        return result

    @classmethod
    def syntax_segments(cls, text: str, lexer_name: str, fallback_style: str) -> list[tuple[str, str]]:
        if lexer_name == "tool-args":
            return cls.tool_arg_segments(text, fallback_style)  # 工具参数是自研词法,不走 pygments
        if pygments is None or get_lexer_by_name is None or not lexer_name:
            return [(fallback_style, text)]  # 无 pygments 或无词法名时降级纯文本
        try:
            lexer = get_lexer_by_name(lexer_name, stripnl=False, ensurenl=False)  # 保留首尾换行:高亮不改变行结构
            return [(cls.pygments_style(token_type), value) for token_type, value in lexer.get_tokens(text) if value]  # 空 token 值跳过
        except Exception:  # noqa: BLE001 - 第三方词法器可能抛任意异常,降级纯文本保证渲染不崩
            return [(fallback_style, text)]

    @classmethod
    def tool_arg_segments(cls, text: str, fallback_style: str) -> list[tuple[str, str]]:
        segments = []
        for match in cls.TOOL_ARG_TOKEN.finditer(text):
            token = match.group(0)
            if token.isspace():
                style = fallback_style  # 空白继承回退样式
            elif token.endswith("="):
                style = Theme.style("syntax.assign")  # key= 视作赋值语法
            elif token.startswith(('"', "'")):
                style = Theme.style("syntax.string")  # 引号字符串字面量
            elif UiPrinter.RECORD_TOKEN_RE.fullmatch(token):
                style = Theme.style("syntax.number")  # tr.3 / job.2 / 时间戳等记录引用
            elif token in {";", ","}:
                style = "ansibrightblack"  # 分隔符弱化
            else:
                style = Theme.style("syntax.ident")
            segments.append((style, token))
        return segments or [(fallback_style, text)]  # 完全没有切分时兜底一段

    def memory_segments(self, text: str) -> list[tuple[str, str]]:
        segments = []
        for line in text.splitlines() or [""]:
            if line.startswith(("goal:", "check:")):
                segments.append(("ansimagenta", line))  # 目标/检查项标题:洋红
            elif line in {"summary:", "plan:", "known:"}:
                segments.append(("ansicyan", line))  # 段标题:青色(必须独立成行才匹配)
            elif line.lstrip().startswith("- [x]"):
                segments.append(("ansigreen", line))  # 已完成任务:绿
            elif line.lstrip().startswith("- [~]"):
                segments.append(("ansiyellow", line))  # 进行中任务:黄
            elif line.lstrip().startswith("- [-]"):
                segments.append(("ansired", line))  # 失败任务:红
            elif line.lstrip().startswith("+ "):
                segments.append(("ansigreen", line))  # 新增条目:绿
            else:
                segments.append(("fg:default", line))
            segments.append(("", "\n"))  # 行尾换行不带样式,后续片段样式独立
        return segments

    @classmethod
    def pygments_style(cls, token_type: Any) -> str:
        style = Theme.pygments_style()
        if style is None or Token is None:
            return "fg:default"  # pygments 不可用时一律默认色
        if token_type in Token.Text.Whitespace:
            return "fg:default"  # 空白不着色,保持主题底色
        if token_type in Token.Name.Builtin:
            return Theme.style("syntax.builtin")  # 内置名走主题 builtin 色,统一视觉
        definition = style.style_for_token(token_type)
        color = definition.get("color")
        default_hex = Theme.style("syntax.default_hex")
        # 与主题默认色相同的颜色折叠为 fg:default,减少样式串数量
        parts = ["fg:default" if not color or color.lower() == default_hex else f"fg:#{color}"]
        parts.extend(attribute for attribute in ("bold", "italic", "underline") if definition.get(attribute))  # 附加粗体/斜体/下划线属性
        return " ".join(parts)

    def _diff_tokenize_lines(self, code_text: str, path: str | None) -> list[list[tuple[str, str]]] | None:
        """对整块代码做词法分析,返回按行切分的高亮片段。

        Pygments 词法器是为完整文件设计的;按 diff 行拆分后逐行独立分析会破坏多行字符串
        和缩进敏感的语言。因此这里把拼装好的代码块整体分析一次,再把 token 流按行切回。"""
        if pygments is None or get_lexer_for_filename is None or not path:
            return None  # 无 pygments,或没有可判别的文件名(如新建文件的空端)时放弃高亮
        try:
            lexer = get_lexer_for_filename(path, stripnl=False)
        except Exception:  # noqa: BLE001 - 按扩展名查词法器失败(未知扩展名)降级
            return None
        try:
            tokens = lexer.get_tokens(code_text)
        except Exception:  # noqa: BLE001 - 第三方词法器执行失败降级
            return None

        lines: list[list[tuple[str, str]]] = [[]]
        for token_type, value in tokens:
            style = self.pygments_style(token_type)
            parts = value.split("\n")
            for i, part in enumerate(parts):
                if i > 0:
                    lines.append([])  # 遇到 token 内的换行就开新行
                if part:
                    lines[-1].append((style, part))
        return lines

    # diff_segments 里行列号 gutter 占用的宽度(`NNNN NNNN | `)。
    DIFF_GUTTER_WIDTH: ClassVar[int] = 12

    def diff_segments(self, text: str, row_width: int | None = None) -> list[tuple[str, str]]:
        return self._diff_segments(text, row_width=row_width, live=False)

    def diff_segments_live(self, text: str, row_width: int | None = None) -> list[tuple[str, str]]:
        """与 diff_segments 相同,但把背景色带填充到当前窗格宽度。仅供随尺寸变化重绘的
        实时渲染器使用(`/diff` 查看器)。scrollback 调用方绝不能用它 —— 烙进去的宽填充
        在窗格变窄后会换行,折行续行还会丢失背景色,看起来是坏的。"""
        return self._diff_segments(text, row_width=row_width, live=True)

    def _diff_segments(self, text: str, *, row_width: int | None, live: bool) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        old_line: int | None = None  # 当前 hunk 的旧(删除侧)行号
        new_line: int | None = None  # 当前 hunk 的新(新增侧)行号
        lines = text.splitlines()
        # 实时查看器会随尺寸变化重绘,可以直接填充到当前窗格宽度。scrollback 只在
        # log_segments 折行之后才填充;在这里先给逻辑行填充,要么在单词边界被丢弃,
        # 要么产生额外的一行。
        changed_width: int | None = None  # 非 live 模式无目标宽度,色带留到折行后再补
        if live:
            if row_width is None:
                row_width = shutil.get_terminal_size((120, 20)).columns - 3
            changed_width = max(1, row_width - self.DIFF_GUTTER_WIDTH)

        # 从 diff 头确定目标文件路径。`+++` 行给出结果文件名;新建文件的 `---` 是 /dev/null。
        file_path: str | None = None
        for header in lines:
            if header.startswith("+++"):
                candidate = header[4:].strip()
                if candidate != "/dev/null":
                    file_path = candidate
                break  # 取第一个 +++ 行即可

        # 收集属于新文件版本的代码行:上下文行与新增行。它们一起词法分析,保证高亮后的
        # diff 在语法上连贯。删除行保持中性并配红底,使"之前"的状态不干扰对"之后"
        # 状态的词法分析。
        new_code_lines: list[str] = []
        new_code_indices: list[int] = []
        for i, line in enumerate(lines):
            # 跳过 unified-diff 的文件头与 hunk 标记(尾部空格避免误伤内容以 "+++" 开头的
            # 真实新增行);只把真正的代码喂给词法器。
            if line.startswith(("+++ ", "--- ", "@@ ")):
                continue
            if line.startswith(("+", " ")):  # 新增行与上下文行属于"新版本"
                new_code_lines.append(line[1:])  # 去掉 +/ 前缀,喂给词法器的是纯净代码
                new_code_indices.append(i)

        highlighted: list[list[tuple[str, str]]] | None = None
        if new_code_lines:
            highlighted = self._diff_tokenize_lines("\n".join(new_code_lines), file_path)

        hl_by_index: dict[int, list[tuple[str, str]]] = {}
        if highlighted is not None:
            for hl_index, line_index in enumerate(new_code_indices):
                if hl_index < len(highlighted):  # 高亮行数不足(词法器异常)时缺省,走兜底样式
                    hl_by_index[line_index] = highlighted[hl_index]  # 把高亮结果按原 diff 行号挂回

        def hunk_start(part: str, prefix: str) -> int | None:
            # 从 "-5,3" / "+7" 解析起始行号;",N" 段可忽略,只需要起点
            if not part.startswith(prefix):
                return None
            try:
                return int(part[1:].split(",", 1)[0])
            except ValueError:
                return None  # 格式异常返回 None,调用方按"行号未知"处理

        def number(old: int | None, new: int | None, background: str = "") -> None:
            old_text = "" if old is None else str(old)
            new_text = "" if new is None else str(new)
            # 双列行号右对齐固定宽度,构成 12 列 gutter
            segments.append((("ansibrightblack " + background).strip(), f"{old_text:>4} {new_text:>4} | "))

        def append_hl(prefix: str, prefix_style: str, content_hl: list[tuple[str, str]], suffix: str, background: str = "") -> None:
            def styled(style: str) -> str:
                return (style + " " + background).strip()  # 高亮样式与背景色合并成一个样式串

            segments.append((styled(prefix_style), prefix))
            for style, piece in content_hl:
                segments.append((styled(style), piece))
            width = get_cwidth(prefix) + sum(get_cwidth(fragment[1]) for fragment in content_hl)  # 已占用宽度
            # 只有 live 模式有目标宽度;scrollback 模式在折行后再补(见 log_segments)
            padding = " " * max(0, changed_width - width) if background and changed_width is not None else ""
            segments.append((background if padding else "", padding + suffix))

        for index, line in enumerate(lines):
            suffix = "\n" if index < len(lines) - 1 else ""  # 最后一行不加换行,由调用方拼接
            if line.startswith("@@"):
                parts = line.split()
                if len(parts) >= 3:  # hunk 头解析新老行号起点;格式异常保持上一步行号,不中断渲染
                    old_line = hunk_start(parts[1], "-")
                    new_line = hunk_start(parts[2], "+")
                number(None, None)
                segments.append(("ansicyan", line + suffix))  # hunk 头青色
            elif line.startswith(("---", "+++")):
                number(None, None)
                segments.append(("ansibrightblack", line + suffix))  # 文件头行:灰显
            elif line.startswith("+"):
                background = Theme.style("diff.added.bg")
                number(None, new_line, background)
                content_hl = hl_by_index.get(index) or [(Theme.style("diff.added.fg"), line[1:])]  # 无整块高亮时主题色兜底
                append_hl("+", "ansigreen", content_hl, suffix, background)
                new_line = None if new_line is None else new_line + 1  # 行号推进;hunk 行号未知时保持 None
            elif line.startswith("-"):
                background = Theme.style("diff.removed.bg")
                number(old_line, None, background)
                append_hl("-", "ansired", [(Theme.style("diff.removed.fg"), line[1:])], suffix, background)  # 删除行不高亮,保持中性
                old_line = None if old_line is None else old_line + 1
            elif line.startswith(" "):
                number(old_line, new_line)
                content_hl = hl_by_index.get(index) or [("fg:default", line[1:])]
                append_hl(" ", "fg:default", content_hl, suffix)  # 上下文行:双行号、中性色
                old_line = None if old_line is None else old_line + 1
                new_line = None if new_line is None else new_line + 1
            else:
                number(None, None)
                segments.append(("fg:default", line + suffix))  # 非 diff 行原样输出
        return segments

    @staticmethod
    def segment_lines(segments: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = [[]]
        for style, text in segments:
            parts = text.split("\n")
            for index, part in enumerate(parts):
                if index > 0:
                    lines[-1].append((style, "\n"))  # 换行符并入前一行,保持片段样式
                    lines.append([])
                if part:
                    lines[-1].append((style, part))
        if lines and not lines[-1]:
            lines.pop()  # 文本以换行结尾时末尾多一个空行,移除
        return lines


class BashLivePreview:
    """阻塞命令期间画在 stderr 上的实时预览:持续渲染命令输出与运行计时。"""

    HEIGHT: ClassVar[int] = 6
    MAX_CHARS: ClassVar[int] = 8000
    # 心跳间隔:即使命令没有任何输出(如安静的长时间运行,或 `... | tail` 这类缓冲到
    # EOF 才输出的命令),计时器仍在推进,阻塞期间终端不会显得卡死。
    TICK: ClassVar[float] = 0.3

    def __init__(self):
        self.output = create_output(sys.stderr)  # 直接写 stderr,与 TUI 应用互不干扰
        self.active = False
        self.rendered_lines = 0  # 上次绘制的行数,用于精确的光标上移擦除
        self.rendered_rows: list[list[tuple[str, str]]] = []  # 上次绘制的帧内容,内容不变则跳过重绘
        self.text = ""
        self.started_at = 0.0
        self.lock = threading.Lock()  # 心跳线程与主线程共享状态,全部操作持锁
        self.timer: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stderr.isatty():
            return  # 非终端(管道/文件)不画预览,输出原样透传
        with self.lock:
            self.active, self.rendered_lines, self.rendered_rows, self.text = True, 0, [], ""
            self.started_at = time.monotonic()  # 单调时钟计时,不受系统时间调整影响
            self.render()  # 先画一帧,立即给出反馈而不是等首个输出
        self.timer = threading.Thread(target=self.tick, daemon=True)  # 守护线程:主进程退出时不挂起
        self.timer.start()

    def tick(self) -> None:
        while True:
            time.sleep(self.TICK)
            with self.lock:
                if not self.active:
                    return  # finish() 已调用:退出循环,结束线程
                self.render()  # 每次心跳重绘,驱动计时显示前进

    def update(self, text: str) -> None:
        with self.lock:
            if not self.active:
                return  # 未激活(非终端或已结束)时丢弃输出
            self.text = (self.text + text)[-self.MAX_CHARS :]  # 环形保留最近 MAX_CHARS 字符,防长输出撑爆内存
            self.render()

    def finish(self) -> None:
        with self.lock:
            if not self.active:
                return
            self.active = False
        timer = self.timer
        if timer is not None:
            timer.join()  # 等心跳线程退出,避免它在清理时再画帧
        with self.lock:
            self.rendered_lines, self.rendered_rows, self.text = 0, [], ""  # 清空渲染状态,后续 stderr 从第一行开始

    def render(self) -> None:
        if not self.active:
            return
        rows: list[list[tuple[str, str]]] = [[("ansibrightblack", line)] for line in self.frame_lines()]
        if rows == self.rendered_rows:
            return  # 内容没变(如计时未前进)时跳过重绘,省去闪烁
        previous = self.rendered_lines
        if self.rendered_lines:
            self.output.write_raw(f"\x1b[{self.rendered_lines}A")  # 光标上移上次绘制的行数,原地重画
        for row in rows:
            self.output.write_raw("\r")
            self.output.erase_end_of_line()  # 擦掉行尾残留,防旧内容更长时留尾巴
            print_formatted_text(FormattedText(row), output=self.output, end="", flush=True)
            self.output.write_raw("\n")
        for _ in range(max(0, previous - len(rows))):
            # 新帧行数变少时,多出的旧行逐行擦除
            self.output.write_raw("\r")
            self.output.erase_end_of_line()
            self.output.write_raw("\n")
        if previous > len(rows):
            self.output.write_raw(f"\x1b[{previous - len(rows)}A")  # 光标回到首行,保持下一帧基准一致
        self.output.flush()
        self.rendered_lines = len(rows)  # 记录本次行数,供下帧上移
        self.rendered_rows = rows

    def frame_lines(self) -> list[str]:
        width = max(20, shutil.get_terminal_size((120, 20)).columns)
        # 归一化换行、Tab 展开为 4 空格,只保留最后 HEIGHT 行
        body = [line.expandtabs(4) for line in self.text.replace("\r", "\n").splitlines()[-self.HEIGHT :]]
        label = Text.elapsed_since(self.started_at, precise=True)
        # `limit` 预留一列余量:全宽行不会自动换行,从而不打断 render() 的光标上移计算。
        limit = max(1, width - get_cwidth(LogBlock.prefix(2, LogEdge.CONTINUE)) - 1)

        def clip(line: str) -> str:
            return Text.clip_width(line, limit)

        # 始终输出状态行:即使命令还没有任何输出,帧也是可见的。
        status = f"output · {label}" if body else f"running… {label}"
        lines = [LogLine(status, role=LogRole.META, edge=LogEdge.BRANCH)]
        lines.extend(LogLine("", clip(line), LogRole.OUTPUT, LogEdge.CONTINUE) for line in body)  # 输出行以 CONTINUE 边线挂在状态行下
        return str(LogBlock.hierarchy(None, lines)).splitlines()


class StatusBar:
    """在计时器线程上展示 agent 此刻在做什么,且不拥有任何状态。

    展示的每个值都来自引擎已经维护的 session 状态。它是一个视图:绝不阻塞轮次,
    也绝不允许自己成为某个状态存在的理由。

    它只写 stderr,且仅当 stderr 是终端时,重绘行才不会混进管道转录,也不会碰到
    stdout 上已完成的输出。它原地重绘、停止时擦除,在 scrollback 里不留痕迹。
    """

    INTERVAL: ClassVar[float] = 0.2  # 重绘与转圈动画共用的节拍
    RETRY_NOTICE_DURATION: ClassVar[float] = 2.0
    INDEX_SPINNER: ClassVar[tuple[str, ...]] = ("~", "/", "-", "\\", "|")
    ROLE_KEYS: ClassVar[tuple[str, ...]] = ("provider", "reason", "mcp", "ctx", "update", "index", "warn")  # 有专属配色的角色;未列出的回退 status.base
    # 工作状态扫描:每几秒有一个波峰扫过状态行。`SWEEP_FALLOFF` 把波峰半宽设为行宽的
    # 一个分数(5.0 → 五分之一),宽到足以"漂移"而不是"闪烁"。Bands 与 levels 分别
    # 量化渐变与波峰,见 `sweep_fragments`。
    SWEEP_CYCLES_PER_SEC: ClassVar[float] = 0.55
    SWEEP_FALLOFF: ClassVar[float] = 5.0
    SWEEP_BANDS: ClassVar[int] = 10
    SWEEP_LEVELS: ClassVar[int] = 10

    @classmethod
    def role_style(cls, role: str) -> str:
        # 已知角色取专属色,未知角色回退基础色,保证任何输入都有样式
        return Theme.style("status." + role) if role in cls.ROLE_KEYS else Theme.style("status.base")

    def __init__(self, session: Session):
        self.session = session
        self.started_at = 0.0
        self.stop_event = threading.Event()  # 停止信号:run 循环每拍检查一次
        self.thread: threading.Thread | None = None
        self.rendered = False  # 是否已画过:stop 时据此决定要不要擦除
        self.output = create_output(sys.stderr)
        self.seen_retry_count = session.state.model_retry_count  # 上次见过的重试计数,用于触发重试提示窗口
        self.retry_notice_until = 0.0

    def start(self, *, reset: bool = True) -> None:
        if self.thread is not None or not sys.stderr.isatty():
            return  # 已在运行,或 stderr 非终端(管道/重定向)时不启动
        self.begin(reset=reset)
        self.stop_event.clear()  # 复用同一状态栏多次启动/停止
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def begin(self, *, reset: bool = True) -> None:
        if reset or not self.started_at:
            self.started_at = time.monotonic()  # reset 或尚未计时时才重新计时:允许只改标签不重计时

    def stop(self) -> None:
        if self.thread is None:
            return  # 未在运行则无事可做
        self.stop_event.set()
        self.thread.join()  # 等循环退出再清理,避免它回头再画一帧
        self.thread = None
        self.clear()  # 擦掉最后一行状态,scrollback 不留痕迹

    def is_running(self) -> bool:
        return self.thread is not None

    def run(self) -> None:
        while not self.stop_event.is_set():  # 停止事件置位即退出
            self.output.write_raw("\r")
            self.output.erase_end_of_line()  # 回到行首并擦除整行,原地重绘
            print_formatted_text(FormattedText(self.display_fragments(active=True)), output=self.output, end="", flush=True)
            self.rendered = True
            self.stop_event.wait(self.INTERVAL)  # 用事件等待而非 sleep:stop() 可立即打断本拍

    def clear(self) -> None:
        if self.rendered:  # 从未画过就不需要擦除
            self.output.write_raw("\r")
            self.output.erase_end_of_line()
            self.output.flush()
            self.rendered = False  # 擦除后复位标记,重复调用 clear 无副作用

    def display_fragments(self, *, active: bool) -> StyleAndTextTuples:
        if not active:
            return self.fragments(sweep=False, show_elapsed=False)
        return self.fragments(sweep=True, show_elapsed=True)

    def retry_notice_active(self) -> bool:
        now = time.monotonic()
        count = self.session.state.model_retry_count
        if count != self.seen_retry_count:
            # 重试计数刚变化:开启 2 秒提示窗口
            self.seen_retry_count = count
            self.retry_notice_until = now + self.RETRY_NOTICE_DURATION
        return self.retry_notice_until > now  # 窗口过后自动熄灭,不打扰后续显示

    def model_attempt_status(self) -> str:
        attempt = self.session.state.current_model_attempt
        return f"attempt {attempt}/{MODEL_REQUEST_RETRIES + 1}" if attempt > 1 else ""  # 首次尝试不显示,只有重试才告知用户

    def retry_status(self) -> str:
        if not self.retry_notice_active():
            return ""  # 提示窗口已过,不显示
        attempt = self.session.state.current_model_attempt
        text = f"retrying {attempt}/{MODEL_REQUEST_RETRIES + 1}" if attempt > 1 else "retrying"
        reason = self.session.state.model_retry_reason  # 附上重试原因(如限流),让用户知道为什么变慢
        return text + (" · " + reason if reason else "")  # 原因可空,避免悬空的 " · "

    def fragments(self, *, sweep: bool, show_elapsed: bool) -> StyleAndTextTuples:
        entries = self.entries(show_elapsed=show_elapsed)
        text = " | ".join(text for text, _ in entries)  # 先拼纯文本,快速判断是否超宽
        columns = shutil.get_terminal_size((120, 20)).columns
        if get_cwidth(text) >= columns:
            if sweep:
                return self.sweep_fragments(Text.clip_width(text, columns - 1))
            # 空闲态按片段裁剪,保留各角色的颜色,而不是整行塌成一个 status.base 色调
            # (窄窗格里出现一条无色的白条)。
            return self.clip_fragments(self.styled_fragments(entries), columns - 1)
        return self.sweep_fragments(text) if sweep else self.styled_fragments(entries)  # 未超宽:扫描模式带波峰,否则静态分段

    def entries(self, *, show_elapsed: bool) -> list[tuple[str, str]]:
        provider = self.session.config.provider
        model = provider.model.rsplit("/", 1)[-1] or "(no model)"  # 去掉 "org/model" 前缀只留模型名;未配置给占位文案
        reason = provider.reasoning
        # 头两项固定:provider/模型 与推理级别
        parts = [(self.session.config.active_provider + "/" + model, "provider"), (reason, "reason")]

        mcp_status = self.mcp_status()
        if mcp_status:
            parts.append((mcp_status, "mcp"))  # 有 MCP 会话才显示
        skill_count = len(self.session.skills.skills) if self.session.skills else 0
        if skill_count:
            parts.append((f"skills {skill_count}", "mcp"))  # 技能数非零才显示,避免空段
        running_jobs = len(self.session.running_jobs())
        if running_jobs:
            parts.append((f"jobs {running_jobs}", "warn"))  # 有后台任务时以 warn 色提醒
        usage = self.session.usage
        if usage.last_prompt_tokens and usage.last_prompt_budget:
            # provider 报告的 token 数与上次请求的预算才是展示层的事实来源;
            # 估算值(state.context_percent)只在还没有任何请求时兜底。
            # 这里只负责渲染;压缩(compaction)依然基于估算值触发(见 DESIGN.md、context.py)。
            ctx_percent = min(100, usage.last_prompt_tokens * 100 // usage.last_prompt_budget)  # 预算口径下可能超 100%,钳制显示
        else:
            ctx_percent = self.session.state.context_percent
        ctx_text = "ctx " + str(ctx_percent) + "%"
        if usage.last_prompt_tokens:
            ctx_text += " · cache " + str(usage.last_cached_prompt_tokens * 100 // usage.last_prompt_tokens) + "%"  # 有实测 token 才附缓存命中率,避免 0/0
        parts.append((ctx_text, "ctx"))
        update_status = self.update_status()
        if update_status:
            parts.append((update_status, "update"))
        index_status = self.index_status()
        if index_status:
            parts.append(("index" + index_status, "index"))
        if self.session.settings.yolo:
            parts.append(("yolo", "warn"))
        if show_elapsed:
            turn_step = self.session.state.turn_step
            max_steps = self.session.settings.max_steps
            # 只在接近上限(80% 以上)时显示步数,远离上限时隐藏
            if turn_step * 5 >= max_steps * 4:
                parts.append((f"step {turn_step}/{max_steps}", "warn"))
            if retry_status := self.retry_status():
                parts.append((retry_status, "warn"))  # 重试提示优先
            elif attempt_status := self.model_attempt_status():
                parts.append((attempt_status, "warn"))
        return parts

    def styled_fragments(self, entries: list[tuple[str, str]]) -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = []
        for index, (text, role) in enumerate(entries):
            if index:
                fragments.append((Theme.style("status.sep"), " | "))  # 段间分隔符不放在行首
            fragments.append((self.role_style(role), text))
        return fragments or [("", "")]  # 空列表给一个空片段,防渲染层处理 None

    @staticmethod
    def clip_fragments(fragments: StyleAndTextTuples, width: int) -> StyleAndTextTuples:
        """把带样式的片段裁剪到显示宽度,同时保留每段的样式,并复刻 Text.clip_width 的
        尾部省略号。行宽超出终端时,空闲状态栏仍能保留各角色的颜色,而不是塌成单一色调。"""
        width = max(0, width)
        if width == 0:
            return [("", "")]  # 宽度为 0 无内容可画
        ellipsis = "." * min(3, width)
        available = width - get_cwidth(ellipsis)
        clipped: StyleAndTextTuples = []
        used = 0
        for style, text, *_ in fragments:
            for char in text:
                char_width = max(0, get_cwidth(char))
                if used + char_width > available:
                    clipped.append((style, ellipsis))  # 按显示宽度(宽字符按 2 计)累计,超限以省略号收尾
                    return clipped
                clipped.append((style, char))
                used += char_width
        return clipped or [("", "")]  # 全空时兜底空片段

    def sweep_fragments(self, text: str) -> StyleAndTextTuples:
        """把工作状态行画成一条在安静渐变上移动的波峰。

        渐变与波峰都做了量化,相邻格子共享同一个样式串。连续逐格变色意味着每列每帧都要
        一条转义序列,还会铸造出 prompt-toolkit 永久缓存的样式串;按带量化后,渲染器每条
        色带只发一条转义,样式串集合也保持很小。对这么宽的波峰而言,量化步长远小于
        眼睛的分辨率,运动依然读起来是连续的。"""
        if not text:
            return [("", "")]  # 空行(无任何条目)不画渐变
        width = max(1, len(text) - 1)
        sweep = (time.monotonic() * self.SWEEP_CYCLES_PER_SEC) % 1.0  # 波峰相位:随单调时钟推进,模 1 循环
        bases = Theme.ramp("status.sweep.start", "status.sweep.end", self.SWEEP_BANDS)
        crest = Theme.rgb(Theme.style("status.sweep.crest"))
        fragments: StyleAndTextTuples = []
        for index, char in enumerate(text):
            ratio = index / width
            base = bases[round(ratio * (self.SWEEP_BANDS - 1))]
            # 距波峰越远衰减越快(平方衰减),离开半宽后归零
            level = round(max(0.0, 1.0 - abs(ratio - sweep) * self.SWEEP_FALLOFF) ** 2 * (self.SWEEP_LEVELS - 1))
            fragments.append(
                (base if not level else Theme.mix(Theme.rgb(base), crest, level / (self.SWEEP_LEVELS - 1)), char)
            )  # 波峰处混入高亮色,其余用纯基底色
        return fragments

    def index_status(self) -> str:
        if self.session.state.code_index_error:
            return CodeIndex.label("error")  # 索引出错时优先显示错误标签
        if self.session.state.code_index_refreshing:
            notice = self.session.state.code_index_notice or "syncing"
            # 动态提示词(syncing/updating)配转圈动画;静态提示词直接显示
            return self.INDEX_SPINNER[int(time.monotonic() / self.INTERVAL) % len(self.INDEX_SPINNER)] if notice in {"syncing", "updating"} else notice
        return CodeIndex.label(self.session.state.code_index_status)  # 稳定状态显示最近一次索引状态标签

    def update_status(self) -> str:
        update = self.session.update
        if update.checking:
            return "update..."  # 检查进行中给省略号提示
        return "update " + update.latest if update.newer_than(__version__) else ""  # 只在有新版本时显示版本号

    def mcp_status(self) -> str:
        if self.session.mcp is None:
            return ""  # 未配置任何 MCP 时不显示
        configs = self.session.mcp.parse_configs()
        if not configs:
            return ""  # 配置为空同样不显示
        status = self.session.mcp.discovery_status
        if status == "discovering":
            spinner = self.INDEX_SPINNER[int(time.monotonic() / self.INTERVAL) % len(self.INDEX_SPINNER)]
            loaded, total = self.session.mcp.discovery_progress()
            return f"mcp {loaded}/{total}{spinner}"  # 发现阶段:已加载/总数 + 转圈
        if status == "error":
            return "mcp err"  # 发现失败压缩为一个词
        if status != "ready":
            return ""  # 其他中间状态(如 idle)不打扰用户
        # "!" 标记工具索引超出上限、部分工具被隐藏。
        return f"mcp {len(self.session.mcp.tools)}{'!' if self.session.mcp.index_truncated else ''}"
