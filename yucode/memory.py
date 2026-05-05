"""Project-scoped persistent memory stored as small Markdown topic files."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from yucode.base import SESSION_EVENT_KEY, Json, Text, ToolError

if TYPE_CHECKING:
    from yucode.model import ModelClient
    from yucode.session import Session, SessionEntry


@dataclass(frozen=True)
class MemoryDocument:
    id: str
    type: str
    description: str
    content: str
    mtime_ns: int
    modified_at: str
    expires_at: str
    age_days: int
    freshness: str
    freshness_warning: str


@dataclass(frozen=True)
class MemoryChange:
    action: str
    id: str
    type: str = ""
    description: str = ""
    content: str = ""
    expires_at: datetime | None = None
    expires_at_supplied: bool = False


@dataclass(frozen=True)
class MemoryConsolidationOutcome:
    attempted: bool = False
    upserted: int = 0
    forgotten: int = 0
    error: str = ""


class ProjectMemory:
    """Persist and recall durable project memory behind one filesystem seam.

    Topic files are the fact source; ``MEMORY.md`` is a derived human-readable
    index. ``context()`` deliberately freezes its first result within one cache
    generation; successful compaction calls ``reset_context()`` before the next
    projection, while ordinary turns keep a stable prompt prefix.
    """

    TYPES: ClassVar[tuple[str, ...]] = ("user", "feedback", "project", "reference")
    ID_RE: ClassVar[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
    MAX_DESCRIPTION_CHARS: ClassVar[int] = 240
    MAX_CONTENT_CHARS: ClassVar[int] = 8000
    MAX_RESULTS: ClassVar[int] = 100
    MAX_CONTEXT_ENTRIES: ClassVar[int] = 50
    INDEX_NAME: ClassVar[str] = "MEMORY.md"
    CONSOLIDATION_LOCK_NAME: ClassVar[str] = ".consolidate-lock"
    MAX_CONSOLIDATION_CHANGES: ClassVar[int] = 100
    DEFAULT_TTL_DAYS: ClassVar[dict[str, int]] = {"user": 365, "feedback": 180, "project": 30, "reference": 90}

    def __init__(self, directory: str, *, now: Callable[[], datetime] | None = None):
        self.directory = os.path.abspath(directory)
        self._now = now or (lambda: datetime.now(UTC))
        self._context_snapshot: str | None = None

    def context(self) -> str:
        """Return a bounded, instance-stable index for session-start context."""

        if self._context_snapshot is not None:
            return self._context_snapshot
        memories = [memory for memory in self.find(limit=self.MAX_RESULTS) if memory.freshness != "expired"][: self.MAX_CONTEXT_ENTRIES]
        if not memories:
            self._context_snapshot = ""
            return ""
        rows = [
            "--- Project Memory (session-start snapshot) ---",
            "These are point-in-time notes from earlier conversations. Use Memory get/search for current content; aging memories require verification against current evidence. Expired memories are omitted.",
            *(
                f"- [{memory.type}] `{memory.id}` ({memory.freshness}, updated {self._age_text(memory.age_days)}, expires {memory.expires_at}): {memory.description}"
                for memory in memories
            ),
        ]
        self._context_snapshot = "\n".join(rows)
        return self._context_snapshot

    def reset_context(self) -> None:
        """Clear only the cached index so the next cache generation reloads disk."""

        self._context_snapshot = None

    def revision(self) -> tuple[tuple[str, int], ...]:
        """Return a stable topic revision used to reject stale consolidation output."""

        return tuple(sorted((memory.id, memory.mtime_ns) for memory in self._documents()))

    def last_consolidated_at(self) -> datetime:
        """Read the successful-consolidation timestamp from the lock file mtime."""

        try:
            timestamp = os.stat(self._consolidation_lock_path()).st_mtime
        except OSError:
            timestamp = 0.0
        return datetime.fromtimestamp(timestamp, UTC)

    @contextmanager
    def consolidation_lock(self) -> Iterator[bool]:
        """Try to own the project consolidation lock without blocking another process."""

        os.makedirs(self.directory, exist_ok=True)
        path = self._consolidation_lock_path()
        created = False
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(path, os.O_RDWR)
        file = os.fdopen(descriptor, "r+", encoding="utf-8")
        acquired = False
        try:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            acquired = True
            if created:
                # A newly-created lock means consolidation has never succeeded. Opening the
                # file would otherwise make it look as though it ran just now and postpone
                # the first eligible review by 24 hours.
                os.utime(path, (0, 0))
            yield True
        finally:
            if acquired:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            file.close()

    def mark_consolidated(self) -> None:
        """Record one successful consolidation without introducing a second state file."""

        timestamp = self._now_utc().timestamp()
        os.utime(self._consolidation_lock_path(), (timestamp, timestamp))

    def apply_consolidation(
        self,
        data: Json,
        *,
        expected_revision: tuple[tuple[str, int], ...],
        allowed_existing_ids: frozenset[str] | None = None,
    ) -> tuple[int, int]:
        """Validate an entire model proposal before applying its topic operations."""

        changes = self._consolidation_changes(data)
        existing_ids = {memory_id for memory_id, _mtime_ns in expected_revision}
        if allowed_existing_ids is not None:
            disallowed = sorted(change.id for change in changes if change.id in existing_ids and change.id not in allowed_existing_ids)
            if disallowed:
                raise ToolError(f"memory consolidation targeted topics whose full bodies were omitted: {', '.join(disallowed)}")
        if self.revision() != expected_revision:
            raise ToolError("memory changed while consolidation was running")
        existing = {memory.id: memory for memory in self._documents()}
        upserted = forgotten = 0
        for change in changes:
            current = existing.get(change.id)
            if change.action == "forget":
                if current is None:
                    continue
                try:
                    os.unlink(self._path(change.id))
                except OSError as error:
                    raise ToolError(f"memory could not be removed: {error}") from error
                existing.pop(change.id, None)
                forgotten += 1
                continue
            same_content = current is not None and (current.type, current.description, current.content) == (
                change.type,
                change.description,
                change.content,
            )
            same_expiration = not change.expires_at_supplied or (current is not None and current.expires_at == self._iso(change.expires_at or self._now_utc()))
            if same_content and same_expiration:
                continue  # 不为纯改写刷新 mtime；新鲜度只能由真实内容变化推进。
            saved = self._write_document(change.id, change.type, change.description, change.content, change.expires_at)
            existing[change.id] = saved
            upserted += 1
        self._write_index()  # MEMORY.md 是派生索引；成功 review 同时修复外部手工编辑留下的漂移。
        return upserted, forgotten

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

    def remember(
        self,
        memory_id: str,
        memory_type: str,
        description: str,
        content: str,
        *,
        expires_at: str | None = None,
    ) -> MemoryDocument:
        """Create or replace one semantic topic and refresh the derived index."""

        memory_id, memory_type, description, content = self._validated_values(memory_id, memory_type, description, content)
        expiration = self._expiration(expires_at)
        saved = self._write_document(memory_id, memory_type, description, content, expiration)
        self._write_index()
        return saved

    def _write_document(
        self,
        memory_id: str,
        memory_type: str,
        description: str,
        content: str,
        expires_at: datetime | None,
    ) -> MemoryDocument:
        os.makedirs(self.directory, exist_ok=True)
        path = self._path(memory_id)
        frontmatter = ["---", f"type: {memory_type}", "description: " + json.dumps(description, ensure_ascii=False)]
        if expires_at is not None:
            frontmatter.append("expires_at: " + json.dumps(self._iso(expires_at)))
        text = "\n".join([*frontmatter, "---", "", content, ""])
        self._atomic_write(path, text)
        saved = self._read_document(path)
        if saved is None:  # 只可能是内部序列化回归,不要留下看似成功的调用结果。
            raise ToolError("memory could not be read after writing")
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
        modified_at = datetime.fromtimestamp(stat.st_mtime, UTC)
        expires_at = self._parse_timestamp(fields.get("expires_at")) or (modified_at + timedelta(days=self.DEFAULT_TTL_DAYS[memory_type]))
        now = self._now_utc()
        age_days = max(0, (now.date() - modified_at.date()).days)
        freshness = "expired" if now >= expires_at else "aging" if age_days > 1 else "fresh"
        warning = self._freshness_warning(freshness, age_days, self._iso(expires_at))
        return MemoryDocument(
            os.path.basename(path)[:-3],
            memory_type,
            description.strip(),
            content,
            stat.st_mtime_ns,
            self._iso(modified_at),
            self._iso(expires_at),
            age_days,
            freshness,
            warning,
        )

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

    def _consolidation_lock_path(self) -> str:
        return os.path.join(self.directory, self.CONSOLIDATION_LOCK_NAME)

    def _consolidation_changes(self, data: Json) -> list[MemoryChange]:
        if set(data) != {"operations"} or not isinstance(data.get("operations"), list):
            raise ToolError("memory consolidator must return an operations array")
        raw_changes = data["operations"]
        if len(raw_changes) > self.MAX_CONSOLIDATION_CHANGES:
            raise ToolError(f"memory consolidator returned more than {self.MAX_CONSOLIDATION_CHANGES} operations")
        changes: list[MemoryChange] = []
        seen: set[str] = set()
        for raw in raw_changes:
            if not isinstance(raw, dict):
                raise ToolError("memory consolidation operation must be an object")
            action = str(raw.get("action") or "").strip()
            if action not in {"upsert", "forget"}:
                raise ToolError("memory consolidation action must be upsert or forget")
            allowed = {"action", "id"} if action == "forget" else {"action", "id", "type", "description", "content", "expires_at"}
            if unexpected := sorted(set(raw) - allowed):
                raise ToolError("memory consolidation operation has unexpected field: " + ", ".join(unexpected))
            memory_id = self._memory_id(raw.get("id"))
            if memory_id in seen:
                raise ToolError(f"memory consolidation returned duplicate id: {memory_id}")
            seen.add(memory_id)
            if action == "forget":
                changes.append(MemoryChange(action, memory_id))
                continue
            memory_id, memory_type, description, content = self._validated_values(
                memory_id,
                raw.get("type"),
                raw.get("description"),
                raw.get("content"),
            )
            expires_at_supplied = raw.get("expires_at") is not None and bool(str(raw.get("expires_at")).strip())
            expires_at = self._expiration(str(raw["expires_at"])) if expires_at_supplied else None
            changes.append(MemoryChange(action, memory_id, memory_type, description, content, expires_at, expires_at_supplied))
        return changes

    def _expiration(self, value: str | None) -> datetime | None:
        if value is None or not str(value).strip():
            return None
        parsed = self._parse_timestamp(str(value))
        if parsed is None:
            raise ToolError("memory expires_at must be an ISO 8601 timestamp")
        return parsed

    def _now_utc(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        try:
            decoded = json.loads(raw) if raw.startswith('"') else raw
            parsed = datetime.fromisoformat(str(decoded))
        except (ValueError, json.JSONDecodeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _age_text(age_days: int) -> str:
        if age_days == 0:
            return "today"
        if age_days == 1:
            return "yesterday"
        return f"{age_days} days ago"

    @staticmethod
    def _freshness_warning(freshness: str, age_days: int, expires_at: str) -> str:
        if freshness == "fresh":
            return ""
        if freshness == "expired":
            return f"This memory expired at {expires_at}. Treat it as historical context only and do not apply it without current confirmation."
        return f"This memory is {age_days} days old. Memories are point-in-time observations, not live state; verify it against current evidence before relying on it."

    def _validated_values(
        self,
        memory_id: object,
        memory_type: object,
        description: object,
        content: object,
    ) -> tuple[str, str, str, str]:
        normalized_id = self._memory_id(memory_id)
        normalized_type = str(memory_type).strip()
        if normalized_type not in self.TYPES:
            raise ToolError("memory type must be one of: " + ", ".join(self.TYPES))
        normalized_description = str(description).strip()
        normalized_content = str(content).strip()
        if (
            not normalized_description
            or len(normalized_description) > self.MAX_DESCRIPTION_CHARS
            or "\n" in normalized_description
            or "\r" in normalized_description
        ):
            raise ToolError(f"memory description must be 1-{self.MAX_DESCRIPTION_CHARS} characters")
        if not normalized_content or len(normalized_content) > self.MAX_CONTENT_CHARS:
            raise ToolError(f"memory content must be 1-{self.MAX_CONTENT_CHARS} characters")
        return normalized_id, normalized_type, normalized_description, normalized_content

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


class MemoryConsolidator:
    """Periodically reconcile project memory after a completed main-thread turn.

    The external interface is deliberately one operation: check the scheduling gate and, when
    eligible, perform one tool-free model call. Session discovery, prompt bounding, filesystem
    locking, stale-output detection, validation, and scheduling timestamps stay inside this
    module so the turn loop does not learn any of those rules.
    """

    MIN_INTERVAL: ClassVar[timedelta] = timedelta(hours=24)
    SESSION_SCAN_INTERVAL: ClassVar[timedelta] = timedelta(minutes=10)
    MIN_HISTORICAL_SESSIONS: ClassVar[int] = 5
    MAX_HISTORICAL_SESSIONS: ClassVar[int] = 20
    MAX_MEMORY_BODY_CHARS: ClassVar[int] = 40_000
    MAX_TRANSCRIPT_CHARS: ClassVar[int] = 8_000
    MAX_TRANSCRIPTS_CHARS: ClassVar[int] = 56_000

    def __init__(self, store: ProjectMemory):
        self.store = store
        self._next_session_scan_at = datetime.fromtimestamp(0, UTC)

    def run_if_due(
        self,
        session: Session,
        model: ModelClient,
        *,
        on_start: Callable[[], None] | None = None,
    ) -> MemoryConsolidationOutcome:
        now = self.store._now_utc()
        last_consolidated = self.store.last_consolidated_at()
        if now - last_consolidated < self.MIN_INTERVAL or now < self._next_session_scan_at:
            return MemoryConsolidationOutcome()
        self._next_session_scan_at = now + self.SESSION_SCAN_INTERVAL
        entries = self._eligible_sessions(session, last_consolidated)
        if len(entries) < self.MIN_HISTORICAL_SESSIONS:
            return MemoryConsolidationOutcome()
        with self.store.consolidation_lock() as acquired:
            if not acquired:
                return MemoryConsolidationOutcome()
            # Another process may have completed consolidation after our first gate check but
            # before we acquired the lock. Re-read the mtime while ownership is exclusive.
            if now - self.store.last_consolidated_at() < self.MIN_INTERVAL:
                return MemoryConsolidationOutcome()
            transcripts = self._historical_transcripts(entries)
            if len(transcripts) < self.MIN_HISTORICAL_SESSIONS:
                return MemoryConsolidationOutcome()
            documents = self.store.find(limit=self.store.MAX_RESULTS)
            revision = self.store.revision()
            prompt_input, allowed_existing_ids = self._prompt_input(session, documents, transcripts, now, last_consolidated)
            if on_start is not None:
                on_start()
            try:
                data = model.consolidate_memory(prompt_input)
                upserted, forgotten = self.store.apply_consolidation(
                    data,
                    expected_revision=revision,
                    allowed_existing_ids=allowed_existing_ids,
                )
                self.store.mark_consolidated()
                return MemoryConsolidationOutcome(True, upserted, forgotten)
            except KeyboardInterrupt:
                return MemoryConsolidationOutcome(True, error="cancelled")
            except Exception as error:  # noqa: BLE001 - maintenance failure must not fail the completed user turn
                return MemoryConsolidationOutcome(True, error=Text.clean(str(error))[:500] or error.__class__.__name__)

    def _eligible_sessions(self, session: Session, last_consolidated: datetime) -> list[SessionEntry]:
        from yucode.session import SessionSnapshotStore

        return [
            entry
            for entry in SessionSnapshotStore.list_sessions(session.config.data_dir, session.cwd)
            if entry.uid != session.uid and entry.updated_at > last_consolidated.timestamp()
        ][: self.MAX_HISTORICAL_SESSIONS]

    def _historical_transcripts(self, entries: list[SessionEntry]) -> list[tuple[SessionEntry, str]]:
        from yucode.session import SessionSnapshotCodec, SessionSnapshotStore

        transcripts: list[tuple[SessionEntry, str]] = []
        used = 0
        for entry in entries:
            try:
                data, _blobs, _header = SessionSnapshotStore.read_merged(entry.path)
                messages = SessionSnapshotCodec.persistable_messages(data.get("messages", []))
            except (OSError, ValueError, TypeError):
                continue
            transcript = self._transcript(messages)
            if not transcript:
                continue
            excerpt = self._clip(transcript, self.MAX_TRANSCRIPT_CHARS)
            if used + len(excerpt) > self.MAX_TRANSCRIPTS_CHARS:
                break
            transcripts.append((entry, excerpt))
            used += len(excerpt)
        return transcripts

    def _prompt_input(
        self,
        session: Session,
        documents: list[MemoryDocument],
        transcripts: list[tuple[SessionEntry, str]],
        now: datetime,
        last_consolidated: datetime,
    ) -> tuple[str, frozenset[str]]:
        manifest = [
            f"- id={json.dumps(memory.id)} type={memory.type} freshness={memory.freshness} modified_at={memory.modified_at} "
            f"expires_at={memory.expires_at} description={json.dumps(memory.description, ensure_ascii=False)}"
            for memory in documents
        ]
        priority = {"expired": 0, "aging": 1, "fresh": 2}
        bodies: list[str] = []
        included_body_ids: set[str] = set()
        used = 0
        for memory in sorted(documents, key=lambda item: (priority.get(item.freshness, 3), item.modified_at, item.id)):
            block = "\n".join(
                [
                    f"<memory id={json.dumps(memory.id)} type={json.dumps(memory.type)}>",
                    memory.content,
                    "</memory>",
                ]
            )
            if used + len(block) > self.MAX_MEMORY_BODY_CHARS:
                continue
            bodies.append(block)
            included_body_ids.add(memory.id)
            used += len(block)
        sessions = []
        for entry, transcript in transcripts:
            sessions.extend(
                [
                    f"<historical_session id={json.dumps(entry.uid)} updated_at={json.dumps(ProjectMemory._iso(datetime.fromtimestamp(entry.updated_at, UTC)))}>",
                    transcript,
                    "</historical_session>",
                ]
            )
        current = self._clip(self._transcript(session.messages), self.MAX_TRANSCRIPT_CHARS)
        prompt_input = "\n\n".join(
            [
                f"Current time: {ProjectMemory._iso(now)}",
                f"Last successful consolidation: {ProjectMemory._iso(last_consolidated)}",
                "Existing memory manifest:\n" + ("\n".join(manifest) or "(empty)"),
                "Existing memory bodies (only these bodies may be updated or forgotten):\n" + ("\n\n".join(bodies) or "(empty)"),
                "Historical sessions updated since the last consolidation:\n" + ("\n".join(sessions) or "(empty)"),
                "Current session (newest evidence; excluded from the five-session scheduling count):\n"
                + ("<current_session>\n" + current + "\n</current_session>" if current else "(empty)"),
            ]
        )
        return prompt_input, frozenset(included_body_ids)

    @staticmethod
    def _transcript(messages: list[Json]) -> str:
        rows: list[str] = []
        for message in messages:
            role = str(message.get("role") or "")
            event = message.get(SESSION_EVENT_KEY)
            if role not in {"user", "assistant"} or (event and event != "compaction_checkpoint"):
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            rows.append(role + ":\n" + Text.clean(content).strip())
        return "\n\n".join(rows)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        head = max(1, limit // 4)
        tail = max(1, limit - head - 45)
        return text[:head].rstrip() + "\n[... middle omitted for memory review ...]\n" + text[-tail:].lstrip()
