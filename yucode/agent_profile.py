"""子 Agent 配置档案的发现、校验和覆盖规则。"""

from __future__ import annotations

import contextlib
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
    META_LINE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$", re.MULTILINE)

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
                "Complete the delegated task fully. Work only on the supplied objective and return a concise evidence-based report.",
            ),
            AgentProfile(
                "explore",
                "快速、只读地定位文件、符号和实现关系",
                "Explore the codebase read-only. Start broad, narrow with evidence, and report exact paths and findings.",
                tools=research_tools,
            ),
            AgentProfile(
                "plan",
                "只读分析实现路径并产出可直接执行的方案",
                "Inspect the codebase read-only and produce a decision-complete implementation plan with verification steps.",
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
        fields = {key.replace("-", "_"): cls.scalar(value) for key, value in cls.META_LINE.findall(metadata)}
        name = fields.get("name", "").strip() or folder
        description = fields.get("description", "").strip()
        errors: list[str] = []
        warnings: list[str] = []
        if not fields.get("name", "").strip():
            errors.append("缺少 name")
        elif not cls.NAME.fullmatch(name):
            errors.append("name 必须由字母、数字、连字符或下划线组成，最长 64 字符")
        if not description:
            errors.append("缺少 description")
        tools = ("*",) if "tools" not in fields else cls.csv(fields["tools"])
        denied = cls.csv(fields.get("disallowed_tools", ""))
        known = set(session.tool_catalog.names if session.tool_catalog else ())
        for tool in (*(() if tools == ("*",) else tools), *denied):
            if tool not in known:
                errors.append(f"未知工具: {tool}")
        overlap = sorted(set(tools).intersection(denied)) if tools != ("*",) else []
        if overlap:
            errors.append("工具同时出现在允许和禁止列表: " + ", ".join(overlap))
        model = fields.get("model", "inherit").strip() or "inherit"
        available_models = {session.config.provider.model, *session.config.provider.available_models}
        if model != "inherit" and available_models and model not in available_models:
            errors.append(f"当前 provider 不提供模型: {model}")
        isolation = fields.get("isolation", "shared").strip().lower() or "shared"
        if isolation not in {"shared", "worktree"}:
            errors.append("isolation 必须是 shared 或 worktree")
            isolation = "shared"
        context = fields.get("context", "fresh").strip().lower() or "fresh"
        if context not in {"fresh", "fork"}:
            errors.append("context 必须是 fresh 或 fork")
            context = "fresh"
        background = cls.boolean(fields.get("background", "false"), "background", errors)
        max_steps = cls.integer(fields.get("max_steps", "0"), "max_steps", errors, minimum=0)
        timeout = cls.integer(fields.get("timeout_seconds", "0"), "timeout_seconds", errors, minimum=0)
        skills = cls.csv(fields.get("skills", ""))
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

    @staticmethod
    def scalar(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value.strip()

    @staticmethod
    def csv(value: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))

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
        rows = [
            "---",
            f"name: {name}",
            f"description: {profile.description or '自定义子 Agent'}",
            "tools: " + ", ".join(profile.tools),
        ]
        if profile.disallowed_tools:
            rows.append("disallowed_tools: " + ", ".join(profile.disallowed_tools))
        rows.extend(
            [
                f"model: {profile.model}",
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
            rows.append("skills: " + ", ".join(profile.skills))
        rows.extend(["---", profile.prompt or "完成委派任务并返回可验证结果。", ""])
        return "\n".join(rows)
