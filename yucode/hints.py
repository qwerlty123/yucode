"""空闲输入提示机制。

空输入占位符中展示的低干扰提示都集中在这里。每条提示声明自己的文本、权重,
以及一个基于小型 Context(描述会话当前状况)的可选适用性谓词。HintPicker 在适用的提示中
按权重随机抽取一条,并按 Context 缓存结果,这样频繁重渲染的占位符保持稳定,
只在状况变化(进入新一轮编辑、离开早期阶段……)时重新抽取。

新增一条提示只需在 HINTS 里加一行;新增一种状况只需在 Context 上加一个字段并写一个谓词。
选择逻辑本身永不改动。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Context:
    """决定哪些提示适用的会话状况。"""

    early: bool  # 会话尚未开始任何工作;此时欢迎导航类提示
    edited_round: int | None  # 刚刚编辑过文件的那一轮,或 None
    skills_available: bool = False  # 是否至少安装了一个技能
    mcp_connected: bool = False  # 是否至少连接了一个 MCP 服务器
    jobs_running: bool = False  # 是否仍有后台任务在运行


def _when_early(ctx: Context) -> bool:
    return ctx.early


def _when_edited(ctx: Context) -> bool:
    return ctx.edited_round is not None


def _when_skills(ctx: Context) -> bool:
    return ctx.skills_available


def _when_mcp(ctx: Context) -> bool:
    return ctx.mcp_connected


def _when_jobs(ctx: Context) -> bool:
    return ctx.jobs_running


@dataclass(frozen=True)
class Hint:
    """一条候选提示:文本、选择权重,以及适用条件(None 表示始终适用)。"""

    text: str
    weight: int = 1
    when: Callable[[Context], bool] | None = None


HINTS: tuple[Hint, ...] = (
    Hint("Esc then Enter inserts a newline"),
    Hint("Tab completes commands and @mentions"),
    Hint("↑ or Ctrl-P recalls earlier prompts"),
    Hint("Ctrl-R searches prompt history"),
    Hint("$skill loads a skill inline", when=_when_skills),
    Hint("@server.tool mentions an MCP tool", when=_when_mcp),
    Hint("Ctrl-X Ctrl-E opens $EDITOR"),
    Hint("Ctrl-U clears the line"),
    Hint("Paste an image path to attach it"),
    Hint("Questions about yucode? Just ask"),
    Hint("Type / for commands"),
    Hint("/sessions resumes a past session", when=_when_early),
    # 刚编辑完后 /diff 是最有用的提示:权重调高,但仍保持随机抽取,
    # 这样反复编辑时不会每次都显示同一行。
    Hint("/diff reviews recent edits", weight=3, when=_when_edited),
    # 后台任务运行时提醒用户它可以被列出;没有任务运行后该提示自然消失。
    Hint("/ps lists background jobs", weight=2, when=_when_jobs),
)


class HintPicker:
    """按权重、按 Context 缓存的 HINTS 选择(参见模块 docstring)。

    抽取结果按 (Context, round_no) 缓存,这样频繁重渲染的占位符不会闪烁;
    当状况变化或新一轮开始时重新抽取。调用方提供 round_no(例如会话轮次计数器);
    选择器自身不保存轮次状态。测试时可注入 `choice` 以获得确定性结果。
    """

    def __init__(self, choice: Callable[[Sequence[str]], str] = random.choice) -> None:
        self._choice = choice
        self._cache: tuple[Context, int, str] | None = None

    def pick(self, ctx: Context, round_no: int = 0) -> str:
        if self._cache is None or self._cache[:2] != (ctx, round_no):
            self._cache = (ctx, round_no, self._select(ctx))
        return self._cache[2]

    def _select(self, ctx: Context) -> str:
        pool = [hint.text for hint in HINTS if hint.when is None or hint.when(ctx) for _ in range(hint.weight)]
        return self._choice(pool)
