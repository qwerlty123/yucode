"""子 Agent 配置档案的发现、校验和覆盖规则。"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass, replace

from yucode.base import ToolError
from yucode.session import Session


@dataclass(frozen=True)
class AgentProfile:
    """一次 spawn 会冻结的 Agent 配置档案。"""

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = ("*",)
    disallowed_tools: tuple[str, ...] = ()
    model: str = "inherit"
    background: bool = False
    isolation: str = "shared"
    context: str = "fresh"
    max_steps: int = 0
    timeout_seconds: int = 0
    skills: tuple[str, ...] = ()
    source: str = "builtin"
    path: str = ""
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def tool_names(self, available: tuple[str, ...], hard_deny: tuple[str, ...] = ()) -> tuple[str, ...]:
        """把 allow/deny 解析成保持根目录顺序的最终工具名。"""

        allowed = set(available if self.tools == ("*",) else self.tools)
        denied = {*self.disallowed_tools, *hard_deny}
        return tuple(name for name in available if name in allowed and name not in denied)


class AgentProfileLibrary:
    """合并内置、用户和项目档案；后加载来源覆盖前一来源。"""

    FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
    META_LINE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")
    FIELDS = frozenset(
        {
            "name",
            "description",
            "tools",
            "disallowed_tools",
            "model",
            "background",
            "isolation",
            "context",
            "max_steps",
            "timeout_seconds",
            "skills",
        }
    )
    LIST_FIELDS = frozenset({"tools", "disallowed_tools", "skills"})

    NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

    def __init__(self, profiles: dict[str, AgentProfile], session: Session):
        self.profiles = profiles
        self.session = session

    @classmethod
    def load(cls, session: Session) -> AgentProfileLibrary:
        profiles = {profile.name.casefold(): profile for profile in cls.builtins()}
        roots = (
            (session.data_path("agents"), "user"),
            (os.path.join(session.cwd, ".yucode", "agents"), "project"),
        )
        for root, source in roots:
            if not os.path.isdir(root):
                continue
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                continue  # 单个 profile 根目录不可读不能阻止 yucode 启动；其余层仍然可用。
            source_seen: dict[str, AgentProfile] = {}
            for entry in entries:
                path = os.path.join(root, entry, "AGENT.md")
                if not os.path.isfile(path):
                    continue
                profile = cls.parse(path, entry, source, session)
                key = profile.name.casefold()
                previous = source_seen.get(key)
                if previous is not None:
                    paths = ", ".join(filter(None, (previous.path, profile.path)))
                    profile = replace(profile, errors=(*profile.errors, f"同一层级存在大小写不敏感的名称歧义: {paths}"))
                source_seen[key] = profile
                profiles[key] = profile
        return cls(profiles, session)

    @staticmethod
    def builtins() -> tuple[AgentProfile, ...]:
        research_tools = ("Read", "ViewImage", "InspectCode", "Search", "Skill", "Recall", "RecallContext")
        return (
            AgentProfile(
                "general-purpose",
                "用于复杂检索、分析和可执行多步骤任务的通用子 Agent",
                """You are a general-purpose yucode sub-agent. Complete the delegated objective fully without expanding its scope.

Working rules:
- Inspect the relevant code and local conventions before deciding or editing.
- Use tools directly and adapt when evidence disproves the proposed approach.
- Preserve unrelated working-tree changes. Do not create branches, commit, or perform destructive Git operations unless the task explicitly requires it.
- Do not create documentation or additional files unless they are necessary for the delegated objective.
- Verify changes in proportion to their risk and report any check you could not run.

Return one concise, evidence-based report covering the outcome, important files, verification, and unresolved warnings. The root Agent owns synthesis and the final user-facing response.""",
            ),
            AgentProfile(
                "explore",
                "快速、只读地定位文件、符号和实现关系",
                """You are a read-only codebase exploration specialist. Answer only the delegated research question and never modify files or repository state.

Working rules:
- Start broad when the location is unknown, then narrow with exact searches, symbols, and file reads.
- Try alternative names and related call sites when the first search is inconclusive.
- Distinguish verified facts from inference and include exact file paths and symbols for material findings.
- Avoid exhaustive reading once enough evidence answers the question.

Return a concise report with the answer, supporting locations, and any remaining uncertainty. Do not propose unrelated implementation work.""",
                tools=research_tools,
            ),
            AgentProfile(
                "plan",
                "只读分析实现路径并产出可直接执行的方案",
                """You are a read-only implementation planning specialist. Inspect the existing code and produce a decision-complete plan without modifying files.

Working rules:
- Resolve the requirements, current behavior, ownership boundaries, and relevant local conventions from evidence.
- Locate existing seams and analogous implementations before introducing new structure.
- Specify concrete files and symbols, data flow, edge cases, compatibility constraints, and verification.
- Keep the design proportional to the repository and call out decisions that still require user input.

Return an ordered implementation plan that another Agent can execute without repeating the investigation, followed by critical files and tests.""",
                tools=research_tools,
            ),
        )

    @classmethod
    def parse(cls, path: str, folder: str, source: str, session: Session) -> AgentProfile:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            return AgentProfile(folder, "", "", source=source, path=path, errors=(f"无法读取档案: {error}",))
        text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
        match = cls.FRONTMATTER.match(text)
        if match is None:
            return AgentProfile(folder, "", text.strip(), source=source, path=path, errors=("缺少 frontmatter",))
        metadata, body = match.group(1), match.group(2).strip()
        fields, errors = cls.parse_metadata(metadata)

        def scalar_field(key: str, default: str = "") -> str:
            value = fields.get(key, default)
            if isinstance(value, tuple):
                errors.append(f"{key} 必须是标量")
                return default
            return value

        def list_field(key: str) -> tuple[str, ...]:
            value = fields.get(key, "")
            return value if isinstance(value, tuple) else cls.csv(value)

        declared_name = scalar_field("name")
        name = declared_name.strip() or folder
        description = scalar_field("description").strip()
        warnings: list[str] = []
        if not declared_name.strip():
            errors.append("缺少 name")
        elif not cls.NAME.fullmatch(name):
            errors.append("name 必须由字母、数字、连字符或下划线组成，最长 64 字符")
        if not description:
            errors.append("缺少 description")
        tools = ("*",) if "tools" not in fields else list_field("tools")
        denied = list_field("disallowed_tools")
        known = set(session.tool_catalog.names if session.tool_catalog else ())
        for tool in (*(() if tools == ("*",) else tools), *denied):
            if tool not in known:
                errors.append(f"未知工具: {tool}")
        overlap = sorted(set(tools).intersection(denied)) if tools != ("*",) else []
        if overlap:
            errors.append("工具同时出现在允许和禁止列表: " + ", ".join(overlap))
        model = scalar_field("model", "inherit").strip() or "inherit"
        available_models = {session.config.provider.model, *session.config.provider.available_models}
        if model != "inherit" and available_models and model not in available_models:
            errors.append(f"当前 provider 不提供模型: {model}")
        isolation = scalar_field("isolation", "shared").strip().lower() or "shared"
        if isolation not in {"shared", "worktree"}:
            errors.append("isolation 必须是 shared 或 worktree")
            isolation = "shared"
        context = scalar_field("context", "fresh").strip().lower() or "fresh"
        if context not in {"fresh", "fork"}:
            errors.append("context 必须是 fresh 或 fork")
            context = "fresh"
        background = cls.boolean(scalar_field("background", "false"), "background", errors)
        max_steps = cls.integer(scalar_field("max_steps", "0"), "max_steps", errors, minimum=0)
        timeout = cls.integer(scalar_field("timeout_seconds", "0"), "timeout_seconds", errors, minimum=0)
        skills = list_field("skills")
        installed = {skill.name.casefold() for skill in session.skills.all()} if session.skills else set()
        for skill in skills:
            if skill.casefold() not in installed:
                warnings.append(f"未安装 skill: {skill}")
        return AgentProfile(
            name=name,
            description=description,
            prompt=body,
            tools=tools,
            disallowed_tools=denied,
            model=model,
            background=background,
            isolation=isolation,
            context=context,
            max_steps=max_steps,
            timeout_seconds=timeout,
            skills=skills,
            source=source,
            path=path,
            errors=tuple(dict.fromkeys(errors)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @classmethod
    def parse_metadata(cls, metadata: str) -> tuple[dict[str, str | tuple[str, ...]], list[str]]:
        """解析 AGENT.md 使用到的 YAML 子集，并把歧义输入留作可定位的校验错误。"""

        lines = metadata.splitlines()
        fields: dict[str, str | tuple[str, ...]] = {}
        errors: list[str] = []
        index = 0
        while index < len(lines):
            raw = lines[index]
            line_number = index + 2  # 文件首行是 frontmatter 起始分隔符。
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            if raw[:1].isspace():
                errors.append(f"第 {line_number} 行存在无归属的缩进内容")
                index += 1
                continue
            match = cls.META_LINE.fullmatch(raw)
            if match is None:
                errors.append(f"第 {line_number} 行不是有效的 key: value")
                index += 1
                continue
            source_key, raw_value = match.groups()
            key = source_key.replace("-", "_")
            if key not in cls.FIELDS:
                errors.append(f"第 {line_number} 行包含未知字段: {source_key}")
            duplicate = key in fields
            if duplicate:
                errors.append(f"第 {line_number} 行重复定义字段: {key}")
            value, index = cls.metadata_value(lines, index, key, raw_value, errors)
            if not duplicate:
                fields[key] = value
        return fields, errors

    @classmethod
    def metadata_value(
        cls,
        lines: list[str],
        index: int,
        field: str,
        raw_value: str,
        errors: list[str],
    ) -> tuple[str | tuple[str, ...], int]:
        line_number = index + 2
        value = cls.strip_comment(raw_value).strip()
        if value in {"|", ">"}:
            block, next_index = cls.indented_block(lines, index + 1, field, errors)
            text = "\n".join(block).rstrip() if value == "|" else " ".join(line.strip() for line in block if line.strip())
            return text, next_index
        if not value:
            block, next_index = cls.indented_block(lines, index + 1, field, errors)
            if block:
                return cls.block_list(block, field, line_number, errors), next_index
            return (() if field in cls.LIST_FIELDS else ""), next_index
        if value.startswith("["):
            if not value.endswith("]"):
                errors.append(f"第 {line_number} 行的 {field} 行内列表缺少 ]")
                return (), index + 1
            return cls.list_value(value[1:-1], field, line_number, errors), index + 1
        if value.endswith("]"):
            errors.append(f"第 {line_number} 行的 {field} 行内列表缺少 [")
            return (), index + 1
        if field in cls.LIST_FIELDS:
            return cls.list_value(value, field, line_number, errors), index + 1
        return cls.scalar_value(value, field, line_number, errors), index + 1

    @classmethod
    def indented_block(cls, lines: list[str], start: int, field: str, errors: list[str]) -> tuple[list[str], int]:
        """消费紧随字段的缩进块；空行可位于块内，但不会吞掉下一个顶层字段。"""

        index = start
        raw_block: list[tuple[int, str]] = []
        while index < len(lines):
            raw = lines[index]
            if raw and not raw[:1].isspace():
                break
            raw_block.append((index + 2, raw))
            index += 1
        if not any(raw.strip() for _line, raw in raw_block):
            return [], index
        block: list[str] = []
        for line_number, raw in raw_block:
            if not raw.strip():
                block.append("")
                continue
            prefix = raw[: len(raw) - len(raw.lstrip())]
            if "\t" in prefix:
                errors.append(f"第 {line_number} 行的 {field} 必须使用空格缩进")
            block.append(raw.lstrip(" \t"))
        while block and not block[-1]:
            block.pop()
        return block, index

    @classmethod
    def block_list(cls, block: list[str], field: str, line_number: int, errors: list[str]) -> tuple[str, ...]:
        values: list[str] = []
        for offset, raw in enumerate(block, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not stripped.startswith("-") or (len(stripped) > 1 and not stripped[1].isspace()):
                errors.append(f"第 {line_number + offset} 行的 {field} 列表项必须以 - 开头")
                continue
            value = cls.strip_comment(stripped[1:]).strip()
            if not value:
                errors.append(f"第 {line_number + offset} 行的 {field} 列表项不能为空")
                continue
            values.append(cls.scalar_value(value, field, line_number + offset, errors))
        return tuple(dict.fromkeys(value for value in values if value))

    @classmethod
    def list_value(cls, value: str, field: str, line_number: int, errors: list[str]) -> tuple[str, ...]:
        values: list[str] = []
        quote = ""
        escaped = False
        start = 0
        for index, character in enumerate(value):
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote == '"':
                escaped = True
                continue
            if quote:
                if character == quote:
                    quote = ""
                continue
            if character in "\"'":
                quote = character
            elif character == ",":
                cls.append_list_item(values, value[start:index], field, line_number, errors)
                start = index + 1
        if quote:
            errors.append(f"第 {line_number} 行的 {field} 包含未闭合引号")
        cls.append_list_item(values, value[start:], field, line_number, errors, allow_empty=not value.strip())
        return tuple(dict.fromkeys(values))

    @classmethod
    def append_list_item(
        cls,
        values: list[str],
        raw: str,
        field: str,
        line_number: int,
        errors: list[str],
        *,
        allow_empty: bool = False,
    ) -> None:
        value = cls.strip_comment(raw).strip()
        if not value:
            if not allow_empty:
                errors.append(f"第 {line_number} 行的 {field} 列表包含空项")
            return
        parsed = cls.scalar_value(value, field, line_number, errors)
        if parsed:
            values.append(parsed)

    @staticmethod
    def strip_comment(value: str) -> str:
        quote = ""
        escaped = False
        for index, character in enumerate(value):
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote == '"':
                escaped = True
                continue
            if quote:
                if character == quote:
                    quote = ""
                continue
            if character in "\"'":
                quote = character
            elif character == "#" and (index == 0 or value[index - 1].isspace()):
                return value[:index]
        return value

    @staticmethod
    def scalar_value(value: str, field: str, line_number: int, errors: list[str]) -> str:
        value = value.strip()
        if not value or value[0] not in "\"'":
            return value
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            errors.append(f"第 {line_number} 行的 {field} 包含未闭合引号")
            return value.lstrip(quote)
        if quote == "'":
            return value[1:-1].replace("''", "'").strip()
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            errors.append(f"第 {line_number} 行的 {field} 包含无效双引号转义")
            return value[1:-1].strip()
        return str(parsed).strip()

    @classmethod
    def scalar(cls, value: str) -> str:
        return cls.scalar_value(cls.strip_comment(value).strip(), "metadata", 0, [])

    @classmethod
    def csv(cls, value: str) -> tuple[str, ...]:
        return cls.list_value(value, "metadata", 0, [])

    @staticmethod
    def boolean(value: str, field: str, errors: list[str]) -> bool:
        lower = value.strip().lower()
        if lower in {"true", "yes", "on", "1"}:
            return True
        if lower in {"false", "no", "off", "0", ""}:
            return False
        errors.append(f"{field} 必须是布尔值")
        return False

    @staticmethod
    def integer(value: str, field: str, errors: list[str], *, minimum: int) -> int:
        try:
            parsed = int(value)
        except ValueError:
            errors.append(f"{field} 必须是整数")
            return minimum
        if parsed < minimum:
            errors.append(f"{field} 不能小于 {minimum}")
            return minimum
        return parsed

    def all(self) -> list[AgentProfile]:
        return sorted(self.profiles.values(), key=lambda profile: (profile.source != "builtin", profile.name.casefold()))

    def get(self, name: str) -> AgentProfile | None:
        return self.profiles.get(name.strip().casefold())

    def with_error(self, name: str, message: str) -> None:
        """为 reload 时发现的跨文件冲突保留原档案，同时把它标成不可运行。"""

        key = name.casefold()
        profile = self.profiles.get(key)
        if profile is not None:
            self.profiles[key] = replace(profile, errors=(*profile.errors, message))

    def create(self, name: str, *, source: str = "project", template: AgentProfile | None = None) -> AgentProfile:
        name = self._name(name)
        path = self._custom_path(name, source)
        directory = os.path.dirname(path)
        root = os.path.dirname(directory)
        if os.path.isdir(root):
            try:
                entries = os.listdir(root)
            except OSError as error:
                raise ToolError(f"读取 Agent profile 目录失败: {error}") from error
            for entry in entries:
                existing = os.path.join(root, entry, "AGENT.md")
                if not os.path.isfile(existing):
                    continue
                parsed = self.parse(existing, entry, source, self.session)
                if parsed.name.casefold() == name.casefold():
                    raise ToolError(f"Agent profile 已存在于 {source} 层: {parsed.name}")
        try:
            os.makedirs(directory, exist_ok=False)
        except OSError as error:
            raise ToolError(f"创建 Agent profile 失败: {error}") from error
        profile = template or AgentProfile(name, "自定义子 Agent", "完成委派任务并返回可验证结果。", source=source, path=path)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.serialize(profile, name))
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(path)
            with contextlib.suppress(OSError):
                os.rmdir(directory)
            raise ToolError(f"创建 Agent profile 失败: {error}") from error
        self._refresh()
        return self.parse(path, name, source, self.session)

    def copy(self, source_name: str, name: str, *, source: str = "project") -> AgentProfile:
        template = self.get(source_name)
        if template is None:
            raise ToolError(f"未知 Agent profile: {source_name}")
        return self.create(name, source=source, template=template)

    def delete(self, name: str) -> None:
        profile = self.get(name)
        if profile is None:
            raise ToolError(f"未知 Agent profile: {name}")
        if profile.source == "builtin" or not profile.path:
            raise ToolError("内置 Agent profile 只读，不能删除")
        try:
            os.unlink(profile.path)
        except OSError as error:
            raise ToolError(f"删除 Agent profile 失败: {error}") from error
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(profile.path))
        self._refresh()

    def _refresh(self) -> None:
        loaded = self.load(self.session)
        self.profiles = loaded.profiles

    def _custom_path(self, name: str, source: str) -> str:
        if source == "project":
            root = os.path.join(self.session.cwd, ".yucode", "agents")
        elif source == "user":
            root = self.session.data_path("agents")
        else:
            raise ToolError("Agent profile source 必须是 project 或 user")
        return os.path.join(root, name, "AGENT.md")

    @classmethod
    def _name(cls, name: str) -> str:
        name = name.strip()
        if not cls.NAME.fullmatch(name):
            raise ToolError("Agent profile 名称必须由字母、数字、连字符或下划线组成，最长 64 字符")
        return name

    @staticmethod
    def serialize(profile: AgentProfile, name: str) -> str:
        def scalar(value: str) -> str:
            return json.dumps(value, ensure_ascii=False)

        def sequence(values: tuple[str, ...]) -> str:
            return "[" + ", ".join(scalar(value) for value in values) + "]"

        rows = [
            "---",
            f"name: {scalar(name)}",
            f"description: {scalar(profile.description or '自定义子 Agent')}",
            "tools: " + sequence(profile.tools),
        ]
        if profile.disallowed_tools:
            rows.append("disallowed_tools: " + sequence(profile.disallowed_tools))
        rows.extend(
            [
                f"model: {scalar(profile.model)}",
                f"background: {'true' if profile.background else 'false'}",
                f"isolation: {profile.isolation}",
                f"context: {profile.context}",
            ]
        )
        if profile.max_steps:
            rows.append(f"max_steps: {profile.max_steps}")
        if profile.timeout_seconds:
            rows.append(f"timeout_seconds: {profile.timeout_seconds}")
        if profile.skills:
            rows.append("skills: " + sequence(profile.skills))
        rows.extend(["---", profile.prompt or "完成委派任务并返回可验证结果。", ""])
        return "\n".join(rows)
