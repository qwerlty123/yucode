"""yucode 技能:按需加载的 Markdown 指令包。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yucode.session import Session


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: str
    source: str  # "builtin"、"user" 或 "project"


class SkillLibrary:
    """从内置、用户和项目技能目录中发现技能。

    每个技能都是一个带 `name`/`description` frontmatter 的 Markdown 文件;索引(name + description)
    随缓存稳定的前缀一起发送,让模型知道有哪些技能;只有当模型调用 Skill(name) 或用户以 `$name`
    引用某个技能时,其完整正文才会被拉入对话。"""

    FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
    META_LINE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$", re.MULTILINE)
    MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])\$([A-Za-z0-9_-]+)")

    def __init__(self, skills: dict[str, Skill]):
        self.skills = skills

    @classmethod
    def load(cls, session: Session) -> SkillLibrary:
        skills: dict[str, Skill] = {}
        # 后面的根目录覆盖前面的:项目可以定制用户技能,用户也可以定制 yucode 自带的只读技能。
        builtin_skills = os.path.join(os.path.dirname(__file__), "builtin_skills")
        project_skills = os.path.join(session.cwd, ".yucode", "skills")
        for root, source in (
            (builtin_skills, "builtin"),
            (session.data_path("skills"), "user"),
            (project_skills, "project"),
        ):
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                skill = cls.parse(os.path.join(root, entry, "SKILL.md"), entry, source)
                if skill is not None:
                    skills[skill.name] = skill
        return cls(skills)

    @classmethod
    def parse(cls, path: str, folder: str, source: str) -> Skill | None:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return None
        # 规范化 BOM 与 CRLF/CR,使依赖 "\n" 的 frontmatter 正则能匹配任何平台编写的文件;
        # 这里只读取两个简单标量,因此保持轻量正则即可。
        text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
        match = cls.FRONTMATTER.match(text)
        meta, body = (match.group(1), match.group(2)) if match else ("", text)
        fields = {key: cls.scalar(value) for key, value in cls.META_LINE.findall(meta)}
        name = fields.get("name") or folder.strip()
        if not name:
            return None
        return Skill(name, fields.get("description", ""), body.strip(), os.path.dirname(path), source)

    @staticmethod
    def scalar(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value.strip()

    def all(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda skill: skill.name)

    def get(self, name: str) -> Skill | None:
        if name in self.skills:
            return self.skills[name]
        resolved = {key.lower(): key for key in self.skills}.get(name.lower())
        return self.skills.get(resolved) if resolved else None

    def expand(self, skill: Skill) -> str:
        return skill.body.replace("{skill_dir}", skill.dir).replace("${SKILL_DIR}", skill.dir)  # 展开技能目录占位符

    def index(self) -> str:
        if not self.skills:
            return ""
        rows = [f"- {skill.name}: {skill.description or '(no description)'}" for skill in self.all()]
        return "\n".join(["--- SKILLS ---", "Use Skill(name) to load a skill's full instructions when its description fits the task.", "", *rows])

    def resolve_mentions(self, text: str) -> str:
        seen: set[str] = set()
        blocks: list[str] = []
        for raw in self.MENTION_PATTERN.findall(text):
            skill = self.get(raw)
            if skill is None or skill.name in seen:
                continue
            seen.add(skill.name)
            blocks.append(f"[{skill.name}] {skill.description}\n{self.expand(skill)}")
        if not blocks:
            return ""
        header = ["--- SKILL MENTIONS ---", "The user explicitly referenced these skills; follow their instructions unless clearly irrelevant.", ""]
        return "\n".join(header + blocks).strip()
