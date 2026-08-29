"""子 Agent 配置档案的发现、校验和覆盖规则。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

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

    def __init__(self, profiles: dict[str, AgentProfile]):
        self.profiles = profiles

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
            for entry in sorted(os.listdir(root)):
                path = os.path.join(root, entry, "AGENT.md")
                if not os.path.isfile(path):
                    continue
                profile = cls.parse(path, entry, source, session)
                profiles[profile.name.casefold()] = profile
        return cls(profiles)

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
        if not description:
            errors.append("缺少 description")
        tools = cls.csv(fields.get("tools", "*")) or ("*",)
        denied = cls.csv(fields.get("disallowed_tools", ""))
        known = set(session.tool_catalog.names if session.tool_catalog else ())
        for tool in (*(() if tools == ("*",) else tools), *denied):
            if tool not in known:
                errors.append(f"未知工具: {tool}")
        overlap = sorted(set(tools).intersection(denied)) if tools != ("*",) else []
        if overlap:
            errors.append("工具同时出现在允许和禁止列表: " + ", ".join(overlap))
        model = fields.get("model", "inherit").strip() or "inherit"
        available_models = session.config.provider.available_models
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
