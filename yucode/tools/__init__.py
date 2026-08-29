"""yucode 工具:暴露给模型的内置工具集。"""

from __future__ import annotations

from yucode.tools.agent import AgentTaskTool, AgentTool
from yucode.tools.ask import AskSpec, AskTool
from yucode.tools.base import Tool
from yucode.tools.catalog import ToolCatalog
from yucode.tools.files import Edit, EditApplyResult, EditTool, ReadTool, ViewImageTool
from yucode.tools.mcp import MCPTool
from yucode.tools.memory import MemoryTool, NextHintsTool, NoteTool, RecallContextTool, RecallTool
from yucode.tools.search import CodeIndex, InspectCodeTool, SearchTool
from yucode.tools.shell import BashTool, JobTool
from yucode.tools.skill import SkillTool

TOOLS: tuple[type[Tool], ...] = (
    AgentTool,
    AgentTaskTool,
    MCPTool,
    SkillTool,
    ReadTool,
    ViewImageTool,
    InspectCodeTool,
    SearchTool,
    EditTool,
    BashTool,
    JobTool,
    RecallTool,
    RecallContextTool,
    MemoryTool,
    NoteTool,
    NextHintsTool,
    AskTool,
)
TOOL_REGISTRY: dict[str, type[Tool]] = {tool.NAME: tool for tool in TOOLS}
TOOL_CATALOG = ToolCatalog(TOOLS)

__all__ = [
    "TOOLS",
    "TOOL_CATALOG",
    "TOOL_REGISTRY",
    "AgentTaskTool",
    "AgentTool",
    "AskSpec",
    "AskTool",
    "BashTool",
    "CodeIndex",
    "Edit",
    "EditApplyResult",
    "EditTool",
    "InspectCodeTool",
    "JobTool",
    "MCPTool",
    "MemoryTool",
    "NextHintsTool",
    "NoteTool",
    "ReadTool",
    "RecallContextTool",
    "RecallTool",
    "SearchTool",
    "SkillTool",
    "Tool",
    "ToolCatalog",
    "ViewImageTool",
]
