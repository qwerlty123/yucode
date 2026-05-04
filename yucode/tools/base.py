"""工具基类:schema 生成、参数解析与结果辅助函数。"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, ClassVar

from yucode.base import Json, ToolArgs, ToolError
from yucode.session import Session, TurnDiff


class Tool:
    """模型可调用的一项能力:它的 schema、参数与一次调用。

    子类通过类属性声明自身并实现 `call`。这些属性不是文档——运行器会读取它们:
    `MUTATES` 决定一次调用是否需要确认,`STORES_RESULT` 决定其输出是否保留供回忆(recall),
    `PRODUCES_MODEL_OBSERVATION` 决定它是否贡献超出文本的内容。`DESCRIPTION` 与 `EXAMPLE`
    是每次请求都携带的提示词表面与成本上下文。

    JSON Schema 来自 `params_schema`;当供应商要求严格函数调用时会被改写,
    此时每个属性都是必填,可选项变为可空。包含自由形式对象的 schema 无法用这种方式表达,
    会回退为非严格模式,而不是被静默收窄。

    实例按调用而非按会话创建:调用之后读取的状态(如一次编辑的 diff)描述的是那一次调用。
    """

    NAME: ClassVar[str] = ""
    DESCRIPTION: ClassVar[str] = ""
    EXAMPLE: ClassVar[tuple[str, ...]] = ()
    RANGE_SCHEMA: ClassVar[Json] = {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2}
    MUTATES: ClassVar[bool] = False
    PRODUCES_MODEL_OBSERVATION: ClassVar[bool] = False
    STORES_RESULT: ClassVar[bool] = True
    LOG_LEXER: ClassVar[str] = "tool-args"
    SILENT: ClassVar[bool] = False  # 纯 UI 工具,其效果展示在别处;抑制其调用/结果日志行

    def __init__(self, session: Session, args: ToolArgs):
        self.session = session
        self.args = args

    def turn_diff(self) -> TurnDiff | None:
        """该工具上次运行产生的文件 diff;若未做任何编辑则为 None。
        由 EditTool 覆写;运行器将其记录到存储结果上,供 /diff 查看器使用。"""
        return None

    def model_observation(self) -> Json | None:
        """完成调用后产生的面向模型的观察(若有)。"""
        return None

    @classmethod
    def schema(cls, strict: bool = False) -> Json:
        description = "\n".join([cls.DESCRIPTION, *(("- " + item) for item in cls.EXAMPLE if item)])
        function: Json = {"name": cls.NAME, "description": description, "parameters": cls.params_schema()}
        if strict and cls._strictifiable(function["parameters"]):
            function["parameters"] = cls._strict_schema(function["parameters"])
            function["strict"] = True  # 标记为严格函数
        return {"type": "function", "function": function}

    @staticmethod
    def resolved_schemas(session: Session) -> list[Json]:
        """返回该会话与供应商可用的工具 schema。"""

        from yucode.tools import TOOL_REGISTRY, MCPTool, NextHintsTool, SkillTool  # 局部导入:注册表构建在所有工具之上

        strict = session.config.provider.resolve().strict_tools_active
        # 可选工具族在具备可用的会话状态之前,不进入模型前缀。
        has_skills = bool(session.skills and session.skills.skills)
        has_mcp = bool(session.mcp and (session.mcp.tools or session.mcp.resources))
        return [
            tool.schema(strict)
            for tool in TOOL_REGISTRY.values()
            if (tool is not SkillTool or has_skills) and (tool is not MCPTool or has_mcp) and (tool is not NextHintsTool or session.settings.quick_hints)
        ]

    @staticmethod
    def _strictifiable(schema: object) -> bool:
        """若 schema 包含自由形式对象(没有 `properties` 的 `object`)则返回 False——
        严格函数调用无法表达它,此类工具回退为非严格模式。"""
        if isinstance(schema, dict):
            if schema.get("type") == "object" and "properties" not in schema:
                return False
            return all(Tool._strictifiable(value) for value in schema.values())
        if isinstance(schema, list):
            return all(Tool._strictifiable(item) for item in schema)
        return True

    @staticmethod
    def _strict_schema(schema: Json) -> Json:
        """改写 JSON Schema 以满足严格函数调用(OpenAI / DeepSeek beta):
        每个对象属性都变为必填(真正的可选项转为可空),
        additionalProperties 强制为 false,不支持的 关键字被丢弃。"""
        # 严格校验器只允许 `type` 联合中出现标量类型;对象/数组的可空性
        # 必须改用 anyOf 表达(例如 {"anyOf": [<array schema>, {"type": "null"}]})。
        scalars = ("string", "number", "integer", "boolean")

        def nullable(sub: Json) -> Json:
            kind = sub.get("type")
            if isinstance(kind, str) and kind in scalars:
                sub["type"] = [kind, "null"]
            elif isinstance(kind, list) and all(item in (*scalars, "null") for item in kind):
                if "null" not in kind:
                    sub["type"] = [*kind, "null"]
            else:
                return {"anyOf": [sub, {"type": "null"}]}
            # enum 也必须接受 null,否则严格校验会拒绝"省略"这一取值。
            if isinstance(sub.get("enum"), list) and None not in sub["enum"]:
                sub["enum"] = [*sub["enum"], None]
            return sub

        # Json 刻意保持浅层(dict[str, Any]);这个递归 schema 变换就是保留动态值类型
        # 比反复转换类型更清晰的地方之一。
        def transform(node: Any) -> Any:
            if isinstance(node, list):
                return [transform(item) for item in node]
            if not isinstance(node, dict):
                return node
            transformed = {key: transform(value) for key, value in node.items() if key not in ("minItems", "maxItems", "minLength", "maxLength")}
            if isinstance(transformed.get("properties"), dict):
                required = set(transformed.get("required") or [])
                for key, sub in transformed["properties"].items():
                    if key not in required and isinstance(sub, dict):
                        transformed["properties"][key] = nullable(sub)
                transformed["required"] = list(transformed["properties"].keys())
                transformed["additionalProperties"] = False
            return transformed

        return transform(copy.deepcopy(schema))

    @staticmethod
    def object_schema(properties: Json, required: list[str] | None = None) -> Json:
        schema: Json = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        return schema

    @classmethod
    def params_schema(cls) -> Json:
        return cls.object_schema({})

    @classmethod
    def payload_args(cls, payload: Json) -> ToolArgs:
        return [payload]

    def needs_confirmation(self) -> bool:
        return self.MUTATES

    @classmethod
    def log_lexer(cls, _: ToolArgs) -> str:
        return cls.LOG_LEXER

    def single_dict_arg(self, message: str) -> Json:
        if len(self.args) != 1 or not isinstance(self.args[0], dict):
            raise ToolError(message)
        return self.args[0]

    def preview(self) -> str:
        return f"{self.NAME}({', '.join(self.short_args())})"

    def short_args(self) -> list[str]:
        return [self.compact(arg) for arg in self.args]

    def call(self) -> str:
        raise NotImplementedError

    def strings(self, *, min_count: int = 0, max_count: int | None = None) -> list[str]:
        if len(self.args) < min_count or (max_count is not None and len(self.args) > max_count):
            limit = f"{min_count}" if max_count == min_count else f"{min_count}-{max_count or 'many'}"
            raise ToolError(f"{self.NAME} requires {limit} string args")
        if not all(isinstance(arg, str) for arg in self.args):
            raise ToolError(f"{self.NAME} args must be strings")
        return [str(arg) for arg in self.args]

    @staticmethod
    def line_range(value: object, label: str = "range") -> tuple[int, int]:
        if not isinstance(value, list) or len(value) != 2 or any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            raise ToolError(f"{label} must be [start,end] integers")
        start, end = value
        if start < 0 or end < 0:
            raise ToolError(f"{label} values must be >= 0")
        return int(start), int(end)

    @staticmethod
    def compact(value: Any, limit: int = 120) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 3] + "..."

    @staticmethod
    def compile_regex(pattern: str, *, case_sensitive: bool = False, multiline: bool = False) -> re.Pattern[str]:
        try:
            flags = (0 if case_sensitive else re.IGNORECASE) | (re.MULTILINE if multiline else 0)
            return re.compile(pattern, flags)
        except re.error as error:
            raise ToolError(f"invalid regex: {error}") from error

    @staticmethod
    def process_result(tag: str, code: int, stdout: str, stderr: str) -> str:
        lines = [f"<{tag}>", f"* exit_code: {code}"]
        for name, text in (("stdout", stdout), ("stderr", stderr)):
            if text:
                lines.extend([f"<{name}>", text.rstrip(), f"</{name}>"])
        lines.append(f"</{tag}>")
        return "\n".join(lines)

    @staticmethod
    def file_stat(path: str) -> str:
        stat = os.stat(path)
        return f'<file_stat mtime_ns="{stat.st_mtime_ns}" size="{stat.st_size}"/>'
