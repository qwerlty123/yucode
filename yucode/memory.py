"""Project-scoped persistent memory stored as small Markdown topic files."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import ClassVar

from yucode.base import ToolError


@dataclass(frozen=True)
class MemoryDocument:
    id: str
    type: str
    description: str
    content: str
    mtime_ns: int


class ProjectMemory:
    """Persist and recall durable project memory behind one filesystem seam.

    Topic files are the fact source; ``MEMORY.md`` is a derived human-readable
    index. ``context()`` deliberately freezes its first result for the lifetime
    of this instance, so request projection keeps a stable prompt prefix.
    """

    TYPES: ClassVar[tuple[str, ...]] = ("user", "feedback", "project", "reference")
    ID_RE: ClassVar[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
    MAX_DESCRIPTION_CHARS: ClassVar[int] = 240
    MAX_CONTENT_CHARS: ClassVar[int] = 8000
    MAX_RESULTS: ClassVar[int] = 100
    MAX_CONTEXT_ENTRIES: ClassVar[int] = 50
    INDEX_NAME: ClassVar[str] = "MEMORY.md"

    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        self._context_snapshot: str | None = None

    def context(self) -> str:
        """Return a bounded, instance-stable index for session-start context."""

        if self._context_snapshot is not None:
            return self._context_snapshot
        memories = self.find(limit=self.MAX_CONTEXT_ENTRIES)
        if not memories:
            self._context_snapshot = ""
            return ""
        rows = [
            "--- Project Memory (session-start snapshot) ---",
            "These are durable notes from earlier conversations. Use Memory get/search for current content; verify stale claims against current evidence.",
            *(f"- [{memory.type}] `{memory.id}`: {memory.description}" for memory in memories),
        ]
        self._context_snapshot = "\n".join(rows)
        return self._context_snapshot

    def find(self, *, ids: list[str] | None = None, query: str = "", limit: int = 20) -> list[MemoryDocument]:
        """List, retrieve, or text-search memories from the current disk state."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.MAX_RESULTS:
            raise ToolError(f"memory limit must be 1-{self.MAX_RESULTS}")
        documents = {memory.id: memory for memory in self._documents()}
        if ids is not None:
            normalized = [self._memory_id(memory_id) for memory_id in ids]
            return [documents[memory_id] for memory_id in normalized if memory_id in documents][:limit]
        ordered = [documents[key] for key in sorted(documents)]
        needle = query.strip().casefold()
        if needle:
            ordered = [memory for memory in ordered if needle in f"{memory.id}\n{memory.type}\n{memory.description}\n{memory.content}".casefold()]
        return ordered[:limit]

    def remember(self, memory_id: str, memory_type: str, description: str, content: str) -> MemoryDocument:
        """Create or replace one semantic topic and refresh the derived index."""

        memory_id = self._memory_id(memory_id)
        memory_type = str(memory_type).strip()
        if memory_type not in self.TYPES:
            raise ToolError("memory type must be one of: " + ", ".join(self.TYPES))
        description = str(description).strip()
        content = str(content).strip()
        if not description or len(description) > self.MAX_DESCRIPTION_CHARS or "\n" in description or "\r" in description:
            raise ToolError(f"memory description must be 1-{self.MAX_DESCRIPTION_CHARS} characters")
        if not content or len(content) > self.MAX_CONTENT_CHARS:
            raise ToolError(f"memory content must be 1-{self.MAX_CONTENT_CHARS} characters")

        os.makedirs(self.directory, exist_ok=True)
        path = self._path(memory_id)
        text = "\n".join(
            [
                "---",
                f"type: {memory_type}",
                "description: " + json.dumps(description, ensure_ascii=False),
                "---",
                "",
                content,
                "",
            ]
        )
        self._atomic_write(path, text)
        saved = self._read_document(path)
        if saved is None:  # 只可能是内部序列化回归,不要留下看似成功的调用结果。
            raise ToolError("memory could not be read after writing")
        self._write_index()
        return saved

    def forget(self, memory_id: str) -> bool:
        """Remove one exact topic and refresh the derived index."""

        path = self._path(self._memory_id(memory_id))
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ToolError(f"memory could not be removed: {error}") from error
        self._write_index()
        return True

    def _documents(self) -> list[MemoryDocument]:
        try:
            entries = list(os.scandir(self.directory))
        except OSError:
            return []
        documents = []
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".md") or entry.name == self.INDEX_NAME:
                continue
            if not self.ID_RE.fullmatch(entry.name[:-3]):
                continue
            if memory := self._read_document(entry.path):
                documents.append(memory)
        return documents

    def _read_document(self, path: str) -> MemoryDocument | None:
        try:
            with open(path, encoding="utf-8") as file:
                lines = file.read().splitlines()
            stat = os.stat(path)
        except OSError:
            return None
        if len(lines) < 5 or lines[0] != "---":
            return None
        try:
            end = lines.index("---", 1)
        except ValueError:
            return None
        fields: dict[str, str] = {}
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        memory_type = fields.get("type", "")
        if memory_type not in self.TYPES:
            return None
        try:
            description = json.loads(fields.get("description", ""))
        except json.JSONDecodeError:
            return None
        if not isinstance(description, str) or not description.strip():
            return None
        content = "\n".join(lines[end + 1 :]).strip()
        if not content:
            return None
        return MemoryDocument(os.path.basename(path)[:-3], memory_type, description.strip(), content, stat.st_mtime_ns)

    def _write_index(self) -> None:
        memories = self.find(limit=self.MAX_RESULTS)
        rows = [
            "# Project Memory",
            "",
            "This file is generated by yucode. Memory content lives in the linked topic files.",
            "",
            *(f"- [{self._markdown_text(memory.description)}]({memory.id}.md) [{memory.type}]" for memory in memories),
            "",
        ]
        os.makedirs(self.directory, exist_ok=True)
        self._atomic_write(os.path.join(self.directory, self.INDEX_NAME), "\n".join(rows))

    def _path(self, memory_id: str) -> str:
        return os.path.join(self.directory, memory_id + ".md")

    @staticmethod
    def _markdown_text(value: str) -> str:
        return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")

    @classmethod
    def _memory_id(cls, value: object) -> str:
        memory_id = str(value).strip()
        if not cls.ID_RE.fullmatch(memory_id):
            raise ToolError("memory id must be 1-64 lowercase letters, numbers, dot, underscore, or hyphen")
        return memory_id

    @staticmethod
    def _atomic_write(path: str, content: str) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".memory-", dir=os.path.dirname(path), text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(content)
            os.replace(temporary, path)
        except OSError as error:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise ToolError(f"memory could not be written: {error}") from error
