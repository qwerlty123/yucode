"""会话级工具目录：让模型可见能力与执行能力共享同一个事实来源。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from yucode.base import Json
from yucode.tools.base import Tool


class ToolCatalog:
    """按声明顺序保存工具，并负责能力收缩与 schema 解析。

    根会话通常使用完整目录；子 Agent 使用 ``filtered`` 得到不可扩权的目录。
    调用方只需要认识这个接口，不需要知道全局注册表或可选工具的显示条件。
    """

    def __init__(self, tools: Iterable[type[Tool]]):
        ordered = tuple(tools)
        names = [tool.NAME for tool in ordered]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ValueError("工具名称必须非空且唯一")
        self._tools = ordered
        self._by_name = dict(zip(names, ordered))

    def __iter__(self) -> Iterator[type[Tool]]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    def get(self, name: str) -> type[Tool] | None:
        return self._by_name.get(name)

    def filtered(self, *, allow: Iterable[str] | None = None, deny: Iterable[str] = ()) -> ToolCatalog:
        """返回只会缩小当前能力的目录，未知 allow 项不会凭空注册工具。"""

        allowed = set(self.names if allow is None else allow)
        denied = set(deny)
        return ToolCatalog(tool for tool in self._tools if tool.NAME in allowed and tool.NAME not in denied)

    def resolved_schemas(self, session) -> list[Json]:
        """返回当前会话真正可执行且当前可用的工具 schema。"""

        from yucode.tools import MCPTool, NextHintsTool, SkillTool

        strict = session.config.provider.resolve().strict_tools_active
        has_skills = bool(session.skills and session.skills.skills)
        has_mcp = bool(session.mcp and (session.mcp.tools or session.mcp.resources))
        return [
            tool.schema(strict)
            for tool in self._tools
            if (tool is not SkillTool or has_skills) and (tool is not MCPTool or has_mcp) and (tool is not NextHintsTool or session.settings.quick_hints)
        ]
