"""yucode session:agent 状态、记录与会话持久化。"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, cast

from yucode.base import (
    SESSION_EVENT_KEY,
    Config,
    ConfigFile,
    Json,
    ModelUsage,
    RuntimeSettings,
    SystemInfo,
    Text,
    ToolArgs,
    UpdateStatus,
    YucodeError,
)
from yucode.image import IMAGE_REFS_KEY, ImageInputs, ImageRef, UserInput
from yucode.prompts import COMPACTION_SUMMARY_TITLE, LIVE_FOLLOWUP_PREFIX, WORKING_STATE_CHECKPOINT_TITLE

if TYPE_CHECKING:
    from yucode.mcp import MCPManager
    from yucode.memory import ProjectMemory
    from yucode.skill import SkillLibrary


CONTEXT_LAYOUT_VERSION = 2  # 上下文布局版本:升级时触发一次性的状态检查点迁移


def local_timestamp(value: float | None = None) -> str:
    """返回人类可读的本地墙钟时间戳,附数字形式的 UTC 偏移。"""
    current = datetime.now().astimezone() if value is None else datetime.fromtimestamp(value).astimezone()  # 无参数取当前时间;有参数按时间戳转换
    return current.isoformat(timespec="seconds")  # 精确到秒,不带微秒


@dataclass
class PlanItem:
    _PLAN_LINE_RE: ClassVar[re.Pattern] = re.compile(r"\[( |x|X|~|-)\]\s+(.+)")
    STATUSES: ClassVar[tuple[str, ...]] = ("todo", "doing", "done", "blocked")
    SYMBOLS: ClassVar[dict[str, str]] = {"todo": " ", "doing": "~", "done": "x", "blocked": "-"}
    LEGACY_MARKERS: ClassVar[dict[str, str]] = {" ": "todo", "~": "doing", "x": "done", "X": "done", "-": "blocked"}  # 旧格式符号 → 新状态

    status: str
    text: str

    @classmethod
    def parse(cls, value: object) -> PlanItem | None:
        if isinstance(value, cls):
            status, text = value.status, value.text  # 已是 PlanItem:直接取字段
        elif isinstance(value, dict):
            status = str(value.get("status") or "todo").strip().lower()
            text = str(value.get("text") or "").strip()
        else:
            raw = str(value).strip()
            match = PlanItem._PLAN_LINE_RE.fullmatch(raw)
            status = cls.LEGACY_MARKERS[match.group(1)] if match else "todo"  # 旧符号映射到新状态
            text = match.group(2).strip() if match else raw  # 不匹配行格式:整串作为文本
        if not text:
            return None  # 空文本条目丢弃
        return cls(status if status in cls.STATUSES else "todo", text)  # 未知状态归一为 todo

    def row(self, *, status: bool = False, style: str = "text") -> str:
        prefix = f"[{self.SYMBOLS[self.status]}] " if status and style == "symbol" else f"{self.status}: " if status else ""
        return "- " + prefix + self.text


@dataclass
class AgentState:
    goal: str = ""
    plan: list[PlanItem | Json | str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    check: str = ""
    summary: str = ""
    # 会话在列表中如何命名,以及名称来源。`apply` 从不设置这两者:
    # 名称跟随用户与目标,而不是工具调用碰巧写入的内容。
    name: str = ""
    name_source: str = ""  # 名称来源:"" | user | goal | input
    code_index_status: str = ""
    code_index_error: str = ""
    code_index_notice: str = ""
    code_index_refreshing: bool = False
    code_index_checking: bool = False
    context_percent: int = 0
    turn_step: int = 0
    turn_messages: int = 0
    round_count: int = 0
    current_model_call_started_at: float = 0.0
    manual_model_retry_requested: bool = False
    model_retry_count: int = 0
    current_model_attempt: int = 0
    model_retry_reason: str = ""
    compaction_count: int = 0

    def __post_init__(self) -> None:
        self.plan = cast(list[PlanItem | Json | str], self.plan_items(self.plan))  # 统一把 plan 归一化为 PlanItem

    @classmethod
    def plan_items(cls, items: Iterable[object]) -> list[PlanItem]:
        return [item for raw in items if (item := PlanItem.parse(raw))]  # 过滤掉无法解析的条目

    @classmethod
    def plan_rows_for(cls, items: Iterable[object], *, status: bool = False, style: str = "text") -> list[str]:
        rows = [item.row(status=status, style=style) for item in cls.plan_items(items)]
        return rows or ["- (empty)"]  # 空计划也给出占位行

    def apply(self, data: Json) -> None:
        for attr in ("goal", "summary", "check"):
            if isinstance(data.get(attr), str):  # 只有字符串才更新,防止模型塞入非字符串
                setattr(self, attr, str(data[attr]).strip())
        for attr in ("plan", "known"):
            value = data.get(attr)
            if isinstance(value, list):
                # known 是纯字符串列表;plan 则走 PlanItem 解析。
                items = list(filter(None, (str(item).strip() for item in value))) if attr == "known" else self.plan_items(value)
                setattr(self, attr, items)

    def format(self, *, include_summary: bool = False) -> str:
        known = ["- " + item for item in self.known] or ["- (empty)"]
        rows = [
            "Goal: " + (self.goal or "(empty)"),
            "Plan:",
            *self.plan_rows_for(self.plan, status=True),
            "Known:",
            *known,
            "Check: " + (self.check or "(empty)"),
        ]
        if include_summary:
            rows.extend(("Summary:", self.summary or "(empty)"))  # 压缩场景才附带 summary
        return "\n".join(rows)


@dataclass
class ToolResultRecord:
    key: str
    name: str
    args: ToolArgs
    output: str
    note: str = ""


@dataclass
class ToolErrorRecord:
    key: str
    name: str
    args: ToolArgs
    error: str


@dataclass
class TurnDiff:
    SNAPSHOT_CHAR_LIMIT: ClassVar[int] = 1_000_000

    key: str
    turn: int
    path: str
    diff: str
    before: str = ""
    after: str = ""
    round: int = 0

    @classmethod
    def bounded_snapshots(cls, before: str, after: str) -> tuple[str, str]:
        """对每个快照单独设上限。快照按唯一内容只存一次,一对快照通常只多占一个新版本
        而不是两个;若把两个长度相加设限,上限会被压在它实际能承受的一半。
        任一快照过大时两者一起丢弃:只留一个会被读成文件被整体创建或删除。"""
        return ("", "") if max(len(before), len(after)) > cls.SNAPSHOT_CHAR_LIMIT else (before, after)


@dataclass
class HistorySegment:
    """一段被压缩的对话,保留以备日后召回。被逐出的消息在压缩时一次性捕获(绝不再摘要),
    因此重复压缩不会叠加损失;有界的原文摘录以内容寻址 blob 存储,`RecallContext`
    按需列出、搜索或取回它。"""

    key: str
    title: str
    text: str = ""


class SessionSnapshotCodec:
    """决定哪些内容需要持久化,并编码成"会话越大、保存越便宜"的形式。

    会话在每次响应和工具批次后都要快照,若每次都整体重写,会话越长保存成本越高。
    每次保存记录只追加序列的长度与摘要,下一次保存只发出追加的部分;加载器把这些 delta
    回放到最后一次完整快照上。任何以非增长方式变化的序列会被整体重写,
    确保过期的前缀永远不会被静默持久化。

    大量重复文本——diff 背后的文件快照、压缩逐出的消息文本——按唯一内容只存一次、
    以哈希引用,因为同一内容经常既是一次编辑的 `before`,又是上一次编辑的 `after`。

    迁移时会过滤旧的 system 角色 resume 标记。新的生命周期事件是只追加的 user 消息:
    持久的模型上下文,带协议无关、对 UI 隐藏的元数据。
    """

    @staticmethod
    def digest(value: object) -> str:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))  # sort_keys:同一对象序列化结果稳定;紧凑分隔符
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def marker(cls, session: Session) -> Json:
        messages = cls.snapshot_messages(session)
        records = [cls.tool_record(record) for record in session.tool_records]
        errors = [cls.tool_error(error) for error in session.tool_errors]
        turn_diff_keys = [diff.key for diff in session.turn_diffs]
        # 各只追加序列的长度与摘要:下一次保存据此算出增量。
        # fmt: off
        return {
            "messages_len": len(messages), "messages_digest": cls.digest(messages), "tool_counter": session.tool_counter,
            "pending_user_inputs_digest": cls.digest([item.to_json() for item in session.pending_user_inputs]),
            "tool_records_len": len(records), "tool_records_digest": cls.digest(records),
            "tool_errors_len": len(errors), "tool_errors_digest": cls.digest(errors),
            "turn_diffs_len": len(turn_diff_keys), "turn_diffs_keys_digest": cls.digest(turn_diff_keys),
            "history_len": len(session.history), "history_keys_digest": cls.digest([seg.key for seg in session.history]),
        }
        # fmt: on

    @classmethod
    def turn_diff(cls, diff: TurnDiff, blobs: dict[str, str]) -> Json:
        """文件快照按内容哈希存储而非内联。反复编辑同一文件会让每个版本出现两次——
        作为一次编辑的 `after` 和下一次编辑的 `before`——否则重写保留窗口时
        会把每个快照再序列化一遍。"""
        before, after = TurnDiff.bounded_snapshots(diff.before, diff.after)
        return {
            "key": diff.key,
            "turn": diff.turn,
            "path": diff.path,
            "diff": diff.diff,
            "before_blob": cls.blob_ref(before, blobs),
            "after_blob": cls.blob_ref(after, blobs),
            "round": diff.round,
        }

    @staticmethod
    def blob_ref(text: str, blobs: dict[str, str]) -> str:
        if not text:
            return ""  # 空快照用空串引用,不产生 blob
        ref = hashlib.sha256(text.encode("utf-8")).hexdigest()
        blobs[ref] = text  # 登记到本次保存的 blob 表,由 write_blobs 写出
        return ref

    @staticmethod
    def tool_record(record: ToolResultRecord) -> Json:
        return asdict(record)

    @staticmethod
    def tool_error(error: ToolErrorRecord) -> Json:
        return asdict(error)

    @staticmethod
    def turn_diffs(data: list[Json], blobs: dict[str, str]) -> list[TurnDiff]:
        diffs: list[TurnDiff] = []
        for d in data:
            # 日志中缺失的 blob 会让快照为空,`net_diff_sections` 已经能通过
            # 记录的 hunks 重建该路径的 diff,所以这里不需要报错。
            before = blobs.get(d.get("before_blob", ""), "")
            after = blobs.get(d.get("after_blob", ""), "")
            before, after = TurnDiff.bounded_snapshots(before, after)  # 恢复时同样按上限约束
            diffs.append(TurnDiff(key=d["key"], turn=d["turn"], path=d["path"], diff=d["diff"], before=before, after=after, round=d.get("round", 0)))
        return diffs

    @classmethod
    def history_segment(cls, segment: HistorySegment, blobs: dict[str, str]) -> Json:
        """被逐出的消息文本是内容寻址 blob,每个唯一内容只写一次,
        因此追加段永远不会重新序列化先前的段。"""
        return {"key": segment.key, "title": segment.title, "blob": cls.blob_ref(segment.text, blobs)}

    @staticmethod
    def history(data: list[Json], blobs: dict[str, str]) -> list[HistorySegment]:
        return [HistorySegment(key=d["key"], title=d.get("title", ""), text=blobs.get(d.get("blob", ""), "")) for d in data]  # 缺失 blob 退化为空文本

    @classmethod
    def has_content(cls, session: Session) -> bool:
        state = session.state
        return any(  # 任一来源有内容即视为可持久化:全空时跳过首次落盘
            (
                bool(cls.snapshot_messages(session)),
                bool(session.pending_user_inputs),
                bool(session.tool_records),
                bool(session.tool_errors),
                bool(session.turn_diffs),
                bool(session.history),
                bool(state.goal or state.plan or state.known or state.check or state.summary),
            )
        )

    @staticmethod
    def is_internal_message(message: Json) -> bool:
        return SessionSnapshotCodec.is_legacy_internal_message(message) or bool(message.get(SESSION_EVENT_KEY))

    @staticmethod
    def is_legacy_internal_message(message: Json) -> bool:
        return message.get("role") == "system" and str(message.get("content") or "").startswith("[Session resumed:")  # 旧版恢复标记

    @classmethod
    def persistable_messages(cls, messages: list[Json]) -> list[Json]:
        return [message for message in messages if not cls.is_legacy_internal_message(message)]  # 过滤旧内部消息;新生命周期事件(SESSION_EVENT_KEY)保留

    @classmethod
    def snapshot_messages(cls, session: Session) -> list[Json]:
        return cls.persistable_messages([*session.messages, *session._active_turn_messages])  # 已提交历史 + 暂存回合

    @staticmethod
    def state(state: AgentState) -> Json:
        data = asdict(state)
        return {
            key: data[key]
            for key in (
                "goal",
                "plan",
                "known",
                "check",
                "summary",
                "name",
                "name_source",
                "compaction_count",
                "round_count",
            )
        }  # 只选可持久化的子集:瞬态与派生字段不进快照

    @staticmethod
    def usage(usage: ModelUsage) -> Json:
        return asdict(usage)

    @classmethod
    def snapshot(cls, session: Session, blobs: dict[str, str]) -> Json:
        # 完整快照:一次性覆盖所有可持久化内容。
        # fmt: off
        return {
            "uid": session.uid, "cwd": session.cwd, "created_at": session.created_at,
            "context_layout_version": session.context_layout_version, "messages": cls.snapshot_messages(session),
            "pending_user_inputs": [item.to_json() for item in session.pending_user_inputs],
            "state": cls.state(session.state), "usage": cls.usage(session.usage), "tool_counter": session.tool_counter,
            "tool_records": [cls.tool_record(record) for record in session.tool_records], "tool_errors": [cls.tool_error(error) for error in session.tool_errors],
            "turn_diffs": [cls.turn_diff(diff, blobs) for diff in session.turn_diffs],
            "history": [cls.history_segment(segment, blobs) for segment in session.history],
        }
        # fmt: on

    @classmethod
    def delta(cls, session: Session, saved: Json, blobs: dict[str, str]) -> Json:
        delta: Json = {
            "tool_counter": session.tool_counter,  # 计数变化也标记:任何差异都不静默
            "usage": cls.usage(session.usage),
            "state": cls.state(session.state),
            "created_at": session.created_at,
            "context_layout_version": session.context_layout_version,
        }
        cls.add_sequence_delta(delta, "messages", cls.snapshot_messages(session), saved, "messages_len", "messages_digest")  # 消息序列按长度/摘要增量
        pending_user_inputs = [item.to_json() for item in session.pending_user_inputs]
        if cls.digest(pending_user_inputs) != saved.get("pending_user_inputs_digest", cls.digest([])):
            delta["pending_user_inputs"] = pending_user_inputs  # 摘要不同才全量写
        cls.add_sequence_delta(
            delta,
            "tool_records",
            [cls.tool_record(record) for record in session.tool_records],
            saved,
            "tool_records_len",
            "tool_records_digest",
        )
        cls.add_sequence_delta(
            delta,
            "tool_errors",
            [cls.tool_error(error) for error in session.tool_errors],
            saved,
            "tool_errors_len",
            "tool_errors_digest",
        )
        cls.add_turn_diffs_delta(delta, session.turn_diffs, saved, blobs)
        cls.add_history_delta(delta, session.history, saved, blobs)
        return delta

    @classmethod
    def add_sequence_delta(cls, delta: Json, key: str, current: list[Json], saved: Json, len_key: str, digest_key: str) -> None:
        last_len = saved.get(len_key, 0)
        if cls.digest(current[:last_len]) == saved.get(digest_key):
            if len(current) > last_len:
                delta[key] = current[last_len:]  # 前缀未变:只发追加部分
        elif cls.digest(current) != saved.get(digest_key):
            delta[key + "_replace"] = current  # 前缀变了(修改/删除):整体替换,绝不静默保留过期前缀

    @classmethod
    def add_turn_diffs_delta(cls, delta: Json, current: list[TurnDiff], saved: Json, blobs: dict[str, str]) -> None:
        keys = [diff.key for diff in current]
        last_len = int(saved.get("turn_diffs_len", 0) or 0)
        saved_digest = saved.get("turn_diffs_keys_digest")
        if cls.digest(keys[:last_len]) == saved_digest:
            if len(current) > last_len:
                delta["turn_diffs"] = [cls.turn_diff(diff, blobs) for diff in current[last_len:]]
        elif cls.digest(keys) != saved_digest:
            # 这里只重写引用;它们指向的快照已在日志中,所以无论文件多大,
            # 窗口重写都保持很小。
            delta["turn_diffs_replace"] = [cls.turn_diff(diff, blobs) for diff in current]

    @classmethod
    def add_history_delta(cls, delta: Json, current: list[HistorySegment], saved: Json, blobs: dict[str, str]) -> None:
        keys = [segment.key for segment in current]
        last_len = int(saved.get("history_len", 0) or 0)
        saved_digest = saved.get("history_keys_digest")
        if cls.digest(keys[:last_len]) == saved_digest:
            if len(current) > last_len:
                delta["history"] = [cls.history_segment(segment, blobs) for segment in current[last_len:]]
        elif cls.digest(keys) != saved_digest:
            delta["history_replace"] = [cls.history_segment(segment, blobs) for segment in current]

    @classmethod
    def merge(cls, data: Json, delta: Json) -> None:
        cls.merge_sequence(data, delta, "messages")
        cls.merge_sequence(data, delta, "tool_records")
        cls.merge_sequence(data, delta, "tool_errors")
        cls.merge_sequence(data, delta, "turn_diffs")
        cls.merge_sequence(data, delta, "history")
        if "tool_counter" in delta:  # 以下标量字段仅在 delta 中显式出现时才覆盖
            data["tool_counter"] = delta["tool_counter"]
        if "usage" in delta:
            data["usage"] = delta["usage"]
        if "state" in delta:
            data["state"] = delta["state"]
        if "pending_user_inputs" in delta:
            data["pending_user_inputs"] = delta["pending_user_inputs"]
        for key in ("created_at", "context_layout_version"):
            if key in delta:
                data[key] = delta[key]

    @staticmethod
    def merge_sequence(data: Json, delta: Json, key: str) -> None:
        replace_key = key + "_replace"
        if replace_key in delta:
            data[key] = delta[replace_key]  # 替换优先
        if key in delta:
            data.setdefault(key, []).extend(delta[key])  # 追加其次:老数据与增量合并

    @staticmethod
    def model_usage(data: Json) -> ModelUsage:
        usage = ModelUsage()
        usage.calls = data.get("calls", 0)
        usage.prompt_tokens = data.get("prompt_tokens", 0)
        usage.completion_tokens = data.get("completion_tokens", 0)
        usage.total_tokens = data.get("total_tokens", 0)
        usage.cached_prompt_tokens = data.get("cached_prompt_tokens", 0)
        usage.cache_write_prompt_tokens = data.get("cache_write_prompt_tokens", 0)
        usage.last_cached_prompt_tokens = data.get("last_cached_prompt_tokens", 0)
        usage.last_cache_write_prompt_tokens = data.get("last_cache_write_prompt_tokens", 0)
        usage.last_prompt_tokens = data.get("last_prompt_tokens", 0)
        usage.last_prompt_budget = data.get("last_prompt_budget", 0)
        return usage

    @staticmethod
    def tool_records(data: list[Json]) -> list[ToolResultRecord]:
        # fmt: off
        return [ToolResultRecord(key=rec["key"], name=rec["name"], args=rec.get("args", []), output=rec.get("output", ""), note=rec.get("note", "")) for rec in data]
        # fmt: on

    @staticmethod
    def tool_errors(data: list[Json]) -> list[ToolErrorRecord]:
        return [ToolErrorRecord(key=err["key"], name=err["name"], args=err.get("args", []), error=err.get("error", "")) for err in data]


@dataclass(frozen=True)
class SessionEntry:
    """列表视角下的一条已存会话:标签与事实,不含对话内容。"""

    uid: str
    name: str
    opening: str
    rounds: int
    cwd: str
    updated_at: float
    path: str

    def matches(self, query: str) -> bool:
        needle = query.strip().lower()  # 大小写不敏感匹配
        return bool(needle) and (self.uid.lower().startswith(needle) or needle in (self.name + " " + self.opening).lower())  # uid 前缀或名称/开场文本包含

    def label(self) -> str:
        return self.name or self.opening or self.uid  # 优先展示名称


class SessionSnapshotStore:
    """会话日志存放在 `<data_dir>/projects/<project>/<uid>.jsonl`,每个工作目录一个子目录,
    各自持有自己的 `latest` 指针。分片让 resume 限定在所属项目内,
    也让按项目的列出与删除成为目录级操作。

    每条日志以头部行开头(`{"v": 2, "uid", "cwd", "created_at"}`),它把关格式版本,
    也让日志在人工阅读时自描述。第 2 行是完整快照;`blob` 行与 delta 从第 3 行起追加。
    """

    FORMAT_VERSION: ClassVar[int] = 2
    PROJECTS_DIR: ClassVar[str] = "projects"
    META_SUFFIX: ClassVar[str] = ".meta.json"
    _SLUG_RE: ClassVar[re.Pattern] = re.compile(r"[^A-Za-z0-9._-]+")

    def __init__(self, session: Session):
        self.session = session

    def save(self) -> str:
        if not self.session._snapshot_saved and not SessionSnapshotCodec.has_content(self.session):
            return ""  # 从未保存且无内容:跳过落盘
        path = self.session_path(self.session.config.data_dir, self.session.cwd, self.session.uid)
        os.makedirs(os.path.dirname(path), exist_ok=True)  # 项目目录按需创建
        blobs: dict[str, str] = {}
        if not self.session._snapshot_saved:
            self.write_jsonl(path, self.header(self.session), mode="w")  # 首行:版本门卫
            record = SessionSnapshotCodec.snapshot(self.session, blobs)  # 首次保存:完整快照
        else:
            record = SessionSnapshotCodec.delta(self.session, self.session._snapshot_saved, blobs)  # 之后:只写增量
        self.write_blobs(path, blobs)  # blob 行先于引用它们的记录写入
        self.write_jsonl(path, record, mode="a")
        self.session._snapshot_saved = SessionSnapshotCodec.marker(self.session)  # 更新标记,下一次才能算 delta
        self.write_latest(self.session.config.data_dir, self.session.cwd, self.session.uid)
        self.write_meta()
        self.garbage_collect_assets()  # 顺手清理已无引用的图片资产
        return self.session.uid

    def write_meta(self) -> None:
        """把列表所需的信息放在日志旁边,浏览会话时无需解析日志。

        日志仍是事实来源;这里只是从中派生的缓存,仅在值变化时重写。
        文件缺失或不可读只会让该会话在列表中失去标签,别无其他影响,
        这也是它永远不会被读回恢复的会话的原因。
        """
        meta: Json = {
            "name": self.session.name,
            "opening": self.session.clip_name(self.session.opening_text()),
            "rounds": self.session.state.round_count,
            "cwd": self.session.cwd,
        }
        if meta == self.session._meta_written:
            return  # 值未变化:不重写
        path = self.meta_path(self.session.config.data_dir, self.session.cwd, self.session.uid)
        with contextlib.suppress(OSError):  # 元数据写失败不影响主保存流程
            self.write_jsonl(path, meta, mode="w")
            self.session._meta_written = meta

    def garbage_collect_assets(self) -> None:
        directory = self.session.images.assets_dir()
        if not os.path.isdir(directory):
            return
        refs: set[str] = set()  # 收集所有仍在引用的图片 ref
        for message in SessionSnapshotCodec.snapshot_messages(self.session):
            raw_images = message.get(IMAGE_REFS_KEY)
            if not isinstance(raw_images, list):
                continue
            refs.update(image.ref for raw in raw_images if (image := ImageRef.from_json(raw)) is not None)  # 消息中的图片引用
        refs.update(image.ref for item in self.session.pending_user_inputs for image in item.images)  # 排队输入中的图片
        refs.update(self.session.images.retained_refs)  # 输入保留的图片
        with contextlib.suppress(OSError):
            for entry in os.scandir(directory):
                if entry.is_file() and entry.name not in refs:
                    os.unlink(entry.path)  # 删除无引用的资产文件
            if not any(os.scandir(directory)):
                os.rmdir(directory)  # 目录清空后删除

    def write_blobs(self, path: str, blobs: dict[str, str]) -> None:
        """blob 行写在引用它们的记录之前,每个内容哈希只写入日志一次。
        会话已存过的内容再次引用时零成本。"""
        for ref, text in blobs.items():
            if ref in self.session._blobs_written:
                continue  # 已写过的 blob 不重复落盘
            self.write_jsonl(path, {"blob": ref, "text": text}, mode="a")
            self.session._blobs_written.add(ref)

    @classmethod
    def header(cls, session: Session) -> Json:
        return {"v": cls.FORMAT_VERSION, "uid": session.uid, "cwd": session.cwd, "created_at": session.created_at}  # 首行:版本门卫 + 自描述

    @staticmethod
    def write_jsonl(path: str, data: Json, *, mode: str) -> None:
        with open(path, mode, encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False) + "\n")

    @classmethod
    def project_slug(cls, cwd: str) -> str:
        """可读的 basename 加上真实路径的哈希:既可浏览,又能在同名目录间保持唯一。"""
        real = os.path.realpath(cwd)  # 真实路径:同名目录用哈希区分
        name = SessionSnapshotStore._SLUG_RE.sub("-", os.path.basename(real)).strip("-") or "root"  # 非法字符换成连字符;空结果用 "root"
        return name + "-" + hashlib.sha256(real.encode("utf-8")).hexdigest()[:10]

    @classmethod
    def project_dir(cls, data_dir: str, cwd: str) -> str:
        return cls.path_for(data_dir, cls.PROJECTS_DIR, cls.project_slug(cwd))

    @classmethod
    def session_path(cls, data_dir: str, cwd: str, uid: str) -> str:
        return os.path.join(cls.project_dir(data_dir, cwd), uid + ".jsonl")

    @classmethod
    def meta_path(cls, data_dir: str, cwd: str, uid: str) -> str:
        return os.path.join(cls.project_dir(data_dir, cwd), uid + cls.META_SUFFIX)

    @classmethod
    def read_meta(cls, directory: str, uid: str) -> Json:
        try:
            with open(os.path.join(directory, uid + cls.META_SUFFIX), encoding="utf-8") as file:
                data = json.loads(file.read())
        except (OSError, ValueError):
            return {}  # 缺失或损坏的 sidecar:按空处理,不影响列表
        return data if isinstance(data, dict) else {}

    @classmethod
    def list_sessions(cls, data_dir: str, cwd: str = "", *, all_projects: bool = False) -> list[SessionEntry]:
        """列出所有已存会话,最新在前,全程不打开任何日志。

        一次目录扫描,每个会话一次小型 sidecar 读取。sidecar 缺失的会话仍会列出——
        以 uid 为名——因为磁盘上的日志才是它真实存在的依据。
        """
        directories = cls.project_dirs(data_dir) if all_projects else [cls.project_dir(data_dir, cwd)]
        entries: list[SessionEntry] = []
        for directory in directories:
            try:
                found = list(os.scandir(directory))
            except OSError:
                continue  # 目录不存在或不可读:跳过该项目
            for entry in found:
                if not entry.name.endswith(".jsonl") or not entry.is_file():
                    continue
                uid = entry.name[:-6]  # 去掉 .jsonl 后缀
                meta = cls.read_meta(directory, uid)
                try:
                    rounds = int(meta.get("rounds") or 0)
                except (TypeError, ValueError):
                    # sidecar 是缓存而非记录;格式损坏只损失轮数,不损失整个列表
                    # (str() 已对上面的文本字段做了防护)。
                    rounds = 0
                with contextlib.suppress(OSError):
                    entries.append(
                        SessionEntry(
                            uid=uid,
                            name=str(meta.get("name") or ""),
                            opening=str(meta.get("opening") or ""),
                            rounds=rounds,
                            cwd=str(meta.get("cwd") or ""),
                            updated_at=entry.stat().st_mtime,
                            path=entry.path,
                        )
                    )
        return sorted(entries, key=lambda item: item.updated_at, reverse=True)  # 按修改时间倒序,最新在前

    @classmethod
    def search_sessions(cls, query: str, data_dir: str, cwd: str = "") -> list[SessionEntry]:
        """匹配 uid 前缀或名称关键词的会话,先查当前项目再查全部。

        只搜当前项目会在用户更换过目录时找不到他想要的会话,
        因此这里查不到就放宽范围,而不是直接失败。
        """
        matches = [entry for entry in cls.list_sessions(data_dir, cwd) if entry.matches(query)]
        if matches:
            return matches
        # 只在未命中时放宽:全项目扫描的成本高于单项目,命中就无需放宽。
        return [entry for entry in cls.list_sessions(data_dir, all_projects=True) if entry.matches(query)]

    @classmethod
    def project_dirs(cls, data_dir: str) -> list[str]:
        try:
            return [entry.path for entry in os.scandir(cls.path_for(data_dir, cls.PROJECTS_DIR)) if entry.is_dir()]
        except OSError:
            return []

    @classmethod
    def find_session_path(cls, data_dir: str, uid: str) -> str:
        """仅凭 UID 定位会话。项目数量少,扫描胜过索引文件——索引文件可能与它描述
        的目录失去同步。"""
        for directory in cls.project_dirs(data_dir):
            path = os.path.join(directory, uid + ".jsonl")
            if os.path.isfile(path):
                return path
        return ""

    @classmethod
    def clean_expired(cls, session: Session) -> int:
        days = session.settings.session_retention_days
        if days <= 0:
            return 0  # 0 或负数表示不清理
        cutoff = time.time() - days * 86400  # 过期线
        removed = 0
        for directory in cls.project_dirs(session.config.data_dir):
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            stale_latest = False
            for entry in entries:
                if not entry.name.endswith(".jsonl") or not entry.is_file():
                    continue
                uid = entry.name[:-6]
                if uid == session.uid:
                    continue  # 当前会话永不清除
                try:
                    if entry.stat().st_mtime >= cutoff:
                        continue  # 未过期
                    os.unlink(entry.path)  # 删除过期日志
                    shutil.rmtree(os.path.join(directory, uid + ".assets"), ignore_errors=True)  # 资产目录同步删除
                    # sidecar 描述的是已不存在的日志;它随日志一起过期。
                    with contextlib.suppress(OSError):
                        os.unlink(os.path.join(directory, uid + cls.META_SUFFIX))
                    removed += 1
                    stale_latest = stale_latest or cls.read_latest(directory) == uid  # 过期的若是 latest 指向的会话,标记指针失效
                except OSError:
                    continue
            if stale_latest:
                cls.clear_latest_dir(directory)  # 失效指针清除
            cls.prune_empty(directory)
        return removed

    @classmethod
    def prune_empty(cls, directory: str) -> None:
        """最后一个会话过期后删除项目目录,避免存储为每个启动过 yucode 的目录
        都积累一个条目。"""
        with contextlib.suppress(OSError):
            if not any(entry.name.endswith(".jsonl") for entry in os.scandir(directory)):
                cls.clear_latest_dir(directory)
                os.rmdir(directory)

    @classmethod
    def write_latest(cls, data_dir: str, cwd: str, uid: str) -> None:
        with open(os.path.join(cls.project_dir(data_dir, cwd), "latest"), "w", encoding="utf-8") as file:
            file.write(uid)  # 指针文件内容就是 uid

    @classmethod
    def read_latest(cls, directory: str) -> str:
        try:
            with open(os.path.join(directory, "latest"), encoding="utf-8") as file:
                return file.read().strip()
        except OSError:
            return ""

    @classmethod
    def latest_uid(cls, data_dir: str, cwd: str) -> str:
        """`cwd` 最近的一次会话。只读一个指针:不做目录扫描,resume 永远不会跨入其他项目。"""
        directory = cls.project_dir(data_dir, cwd)
        uid = cls.read_latest(directory)
        if uid and os.path.isfile(os.path.join(directory, uid + ".jsonl")):
            return uid  # 指针有效:直接使用
        return cls.newest_uid(directory)  # 指针缺失/过期/失效:按 mtime 找最新

    @classmethod
    def newest_uid(cls, directory: str) -> str:
        """指针缺失或过期时的兜底:按 mtime 取项目里最新的日志。"""
        try:
            entries = [entry for entry in os.scandir(directory) if entry.name.endswith(".jsonl") and entry.is_file()]
        except OSError:
            return ""
        newest = max(entries, key=lambda entry: entry.stat().st_mtime, default=None)
        return newest.name[:-6] if newest else ""

    @classmethod
    def clear_latest_dir(cls, directory: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(directory, "latest"))

    @classmethod
    def load(cls, uid: str, config: Config | None = None, settings: RuntimeSettings | None = None, cwd: str = "") -> Session:
        if config is None:
            config = Config.from_dict(ConfigFile.load())
        if settings is None:
            settings = RuntimeSettings()
        cwd = cwd or os.getcwd()  # 未指定时用当前目录
        uid = cls.resolve_uid(uid, config.data_dir, cwd)  # 解析 latest/搜索等别名
        path = cls.find_session_path(config.data_dir, uid)
        if not path:
            raise YucodeError(f"Session snapshot not found: {uid} under {cls.path_for(config.data_dir, cls.PROJECTS_DIR)}")
        data, blobs, header = cls.read_merged(path)  # 合并完整快照与所有 delta
        tool_records = SessionSnapshotCodec.tool_records(data.get("tool_records", []))
        raw_created_at = data.get("created_at", header.get("created_at"))  # 老日志没有 created_at 时退回头部
        if isinstance(raw_created_at, (int, float)):
            created_at = local_timestamp(float(raw_created_at))  # 数字时间戳转字符串
        elif isinstance(raw_created_at, str) and raw_created_at.strip():
            created_at = raw_created_at.strip()  # 已是字符串直接用
        else:
            created_at = local_timestamp()  # 都没有:用当前时间
        session = Session(
            cwd=data.get("cwd", cwd),
            config=config,
            settings=settings,
            messages=SessionSnapshotCodec.persistable_messages(data.get("messages", [])),
            state=AgentState(**data.get("state", {})),
            usage=SessionSnapshotCodec.model_usage(data.get("usage", {})),
            tool_counter=data.get("tool_counter", 0),
            tool_results={record.key: record.output for record in tool_records},
            tool_records=tool_records,
            tool_errors=SessionSnapshotCodec.tool_errors(data.get("tool_errors", [])),
            turn_diffs=SessionSnapshotCodec.turn_diffs(data.get("turn_diffs", []), blobs),
            history=SessionSnapshotCodec.history(data.get("history", []), blobs),
            pending_user_inputs=[item for value in data.get("pending_user_inputs", []) if (item := QueuedInput.from_json(value)) is not None],
            uid=data.get("uid", uid),
            resumed=True,
            created_at=created_at,
            context_layout_version=int(data.get("context_layout_version", 1) or 1),
        )
        # 在追加持久化生命周期/检查点事件之前标记已加载的前缀,
        # 这样下一次快照把它们作为只追加的 delta 写入。
        session._snapshot_saved = SessionSnapshotCodec.marker(session)
        if session.context_layout_version < CONTEXT_LAYOUT_VERSION:
            # 旧布局:一次性迁移。有工作状态才补状态检查点。
            if session.state.goal or session.state.plan or session.state.known or session.state.check or session.state.summary:
                session.messages.append(session.state_checkpoint_event())
            session.context_layout_version = CONTEXT_LAYOUT_VERSION
        resumed_at = local_timestamp()
        session.messages.append(
            {
                "role": "user",
                "content": f'<session_event type="resumed" at="{resumed_at}" />',
                SESSION_EVENT_KEY: "resumed",  # 生命周期事件:只追加、对 UI 隐藏
            }
        )
        session._blobs_written = set(blobs)  # 已存在的 blob 视为已写,避免重复写
        return session

    @classmethod
    def resolve_uid(cls, uid: str, data_dir: str, cwd: str) -> str:
        """`latest`/`last` 指*本项目内*最近的会话,绝不会是别处的会话。

        其他输入按 uid 处理,找不到再搜索:没有人会重打一个他能描述的 uid。
        歧义搜索会列出候选而不是擅自挑一个。
        """
        if uid in {"latest", "last"}:
            resolved = cls.latest_uid(data_dir, cwd)  # 别名解析
            if not resolved:
                raise YucodeError(f"No previous session for this project: {cwd}")
            return resolved
        if cls.find_session_path(data_dir, uid):
            return uid  # 精确 uid 命中
        matches = cls.search_sessions(uid, data_dir, cwd)  # 否则按名称/前缀搜索
        if len(matches) == 1:
            return matches[0].uid  # 唯一候选:直接采用
        if matches:
            listed = "\n".join(f"  {entry.uid}  {entry.label()}" for entry in matches[:5])
            more = f"\n  ... and {len(matches) - 5} more" if len(matches) > 5 else ""
            raise YucodeError(f"{len(matches)} sessions match {uid!r}:\n{listed}{more}")  # 多个候选:全部列出并报歧义
        return uid  # 无候选:按原始 uid 继续(留给上层报错)

    @classmethod
    def read_merged(cls, path: str) -> tuple[Json, dict[str, str], Json]:
        merged: Json | None = None
        blobs: dict[str, str] = {}
        header: Json = {}
        with open(path, encoding="utf-8") as file:
            for index, line in enumerate(file):
                line = line.strip()
                if not line:
                    continue
                parsed = json.loads(line)  # 每行一条 JSON
                if index == 0:
                    cls.check_header(parsed, path)  # 头部行:校验版本
                    header = parsed
                elif "blob" in parsed:
                    blobs[parsed["blob"]] = parsed.get("text", "")  # blob 行
                elif merged is None:
                    merged = parsed  # 第一条记录是完整快照
                else:
                    SessionSnapshotCodec.merge(merged, parsed)  # 其后都是 delta
        if merged is None:
            raise YucodeError(f"Empty session file: {path}")  # 只有头部没有快照:文件损坏
        return merged, blobs, header

    @classmethod
    def check_header(cls, header: Json, path: str) -> None:
        version = header.get("v")
        if version != cls.FORMAT_VERSION:
            raise YucodeError(f"Unsupported session format v{version} (expected v{cls.FORMAT_VERSION}): {path}")  # 版本不匹配即拒绝,保证向前兼容

    @staticmethod
    def path_for(data_dir: str, *parts: str) -> str:
        return os.path.abspath(os.path.join(os.path.expanduser(data_dir), *parts))  # 展开 ~ 再取绝对路径


@dataclass
class BackgroundJob:
    """由会话跟踪的非阻塞 shell 进程。输出要么重定向到磁盘日志文件
    (通过 `Job(start)` 启动的作业),要么由 drainer 线程累积到内存 tail 缓冲区
    (bash_wait_timeout 后从运行中的 BashTool 调用提升的作业)。两种变体暴露相同的
    tail/status/wait/kill 接口。"""

    id: str
    command: str
    process: subprocess.Popen[bytes]
    log_path: str
    started_at: float
    status: str = "running"
    exit_code: int | None = None
    # 内存 tail,由 BashTool.promote_to_job 的 drainer 线程填充。设置后 tail()
    # 从这里而不是 log_path 读取。drainer 以 BUFFER_LIMIT 字符为上限。
    stream_buffer: list[str] | None = None
    stream_lock: threading.Lock | None = None

    BUFFER_LIMIT: ClassVar[int] = 32 * 1024  # 每条流 tail 缓冲的字符上限

    def update_status(self) -> None:
        if self.status != "running":
            return  # 终态(含 killed)不再更新
        code = self.process.poll()  # 非阻塞检查
        if code is not None:
            self.status = "done"  # 进程已退出
            self.exit_code = code

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at  # 单调时钟:不受系统时间调整影响

    def kill(self, grace: float = 3.0) -> None:
        """先 SIGTERM,等待 grace 秒,仍在运行则 SIGKILL。最后删除日志文件。"""
        if self.status == "running":  # 只处理运行中作业
            try:
                os.killpg(self.process.pid, signal.SIGTERM)  # 杀进程组,连带子进程
            except OSError:
                self.process.terminate()  # 进程组不存在:退回单进程
            try:
                self.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)  # 宽限期未退出:升级 SIGKILL
                except OSError:
                    self.process.kill()
                self.process.wait()
            self.update_status()
            if self.status == "running":
                self.status = "killed"  # 强杀后标记
                self.exit_code = -1
        if self.log_path:
            with contextlib.suppress(OSError):
                os.unlink(self.log_path)  # 清理日志文件

    def tail(self, limit: int) -> str:
        """返回合并的 stdout+stderr 日志的最后 `limit` 个字符。"""
        limit = max(0, limit)  # 负值钳制为 0
        if self.stream_buffer is not None:
            with self.stream_lock or contextlib.nullcontext():  # 无锁时退化为空上下文
                text = "".join(self.stream_buffer)
        else:
            try:
                with open(self.log_path, "rb") as file:
                    file.seek(0, 2)
                    size = file.tell()  # 先到文件尾取大小
                    # UTF-8 每字符最多 4 字节;多读一点,确保解码后至少得到 `limit` 个字符。
                    file.seek(max(0, size - limit * 4), 0)
                    text = file.read().decode("utf-8", errors="replace")  # 非法字节用替换符,不抛错
            except OSError:
                return ""  # 日志被删除等:返回空
        if len(text) <= limit:
            return text
        if limit <= 3:
            return "." * limit  # 太小放不下省略号:退化为点
        return "..." + text[-(limit - 3) :]  # 省略号占 3 字符


@dataclass(eq=False)
class QueuedInput:
    text: str
    images: tuple[ImageRef, ...] = ()
    draft: str = ""
    inflight: bool = False

    def to_json(self) -> str | Json:
        if not self.images:
            return self.text  # 无图片:保持纯字符串,兼容旧格式
        return {
            "text": self.text,
            "draft": self.draft,
            IMAGE_REFS_KEY: [image.to_json() for image in self.images],
        }

    @classmethod
    def from_json(cls, value: object) -> QueuedInput | None:
        if isinstance(value, str):
            return cls(value) if value.strip() else None  # 空字符串不入队
        if not isinstance(value, dict):
            return None  # 未知形状丢弃
        text = str(value.get("text") or "")
        raw_images = value.get(IMAGE_REFS_KEY)
        images = (
            tuple(image for raw in raw_images if (image := ImageRef.from_json(raw)) is not None) if isinstance(raw_images, list) else ()
        )  # 非法图片引用跳过
        draft = str(value.get("draft") or text)
        if not text.strip():
            return None
        if draft.count("\ufffc") != len(images):
            return cls(text)  # 占位符与图片数不符:draft 失效,退回纯文本
        return cls(text, images, draft)

    def user_input(self) -> UserInput:
        return UserInput(self.draft or self.text, self.images)

    def message(self, prefix: str = "") -> Json:
        message: Json = {"role": "user", "content": prefix + self.text}  # prefix 用于 follow-up 等标记前缀
        if self.images:
            message[IMAGE_REFS_KEY] = [image.to_json() for image in self.images]  # 图片引用单独字段
        return message


@dataclass
class Session:
    """协议无关的语义状态,加上限定在一次运行会话内的资源。

    持久的事实来源包括消息、保留的工具输出、diff 与 usage。同一个聚合体也持有瞬时
    会话资源(作业、provider/更新状态、能力管理器、缓存),但 `SessionSnapshotCodec`
    显式选择足以恢复的子集。provider 客户端、流片段与终端布局按设计不持久化,
    由恢复过程重建。

    进行中的回合与已提交历史分开暂存,因此被打断或崩溃的回合可以被收尾或丢弃,
    不会在记录里留下半个回合。

    排队输入与快照写入都有锁保护:输入在 UI 线程到达,而 agent 在另一个线程运行。
    """

    cwd: str = field(default_factory=os.getcwd)  # 默认当前目录
    system_info: SystemInfo | None = None
    config: Config = field(default_factory=Config)
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)
    messages: list[Json] = field(default_factory=list)
    state: AgentState = field(default_factory=AgentState)
    tool_results: dict[str, str] = field(default_factory=dict)
    tool_records: list[ToolResultRecord] = field(default_factory=list)
    memory_reads: dict[str, int] = field(default_factory=dict)  # Memory 工具本会话内完整读到正文的 topic → 当时的 mtime_ns;瞬时状态,不随快照持久化
    tool_errors: list[ToolErrorRecord] = field(default_factory=list)
    pending_user_inputs: list[QueuedInput] = field(default_factory=list)
    quick_hints: tuple[str, ...] = field(default_factory=tuple)  # 瞬时的下一步输入建议;从不序列化,每轮清空
    tool_counter: int = 0
    turn_diffs: list[TurnDiff] = field(default_factory=list)
    history: list[HistorySegment] = field(default_factory=list)
    jobs: dict[str, BackgroundJob] = field(default_factory=dict)
    job_counter: int = 0
    usage: ModelUsage = field(default_factory=ModelUsage)
    update: UpdateStatus = field(default_factory=UpdateStatus)
    mcp: MCPManager | None = None
    skills: SkillLibrary | None = None
    memory: ProjectMemory | None = field(default=None, repr=False)
    images: ImageInputs = field(init=False, repr=False)
    _gitignore_cache: dict[str, tuple[int, list[str]]] = field(default_factory=dict)  # (mtime, 规则) 缓存,避免重复解析 .gitignore
    uid: str = ""
    resumed: bool = False
    created_at: str = field(default_factory=local_timestamp)
    context_layout_version: int = CONTEXT_LAYOUT_VERSION
    _snapshot_saved: dict = field(default_factory=dict)  # 已保存的序列标记,用于计算下一次 delta
    _blobs_written: set[str] = field(default_factory=set)  # 已写入日志的 blob 哈希
    _meta_written: dict = field(default_factory=dict)  # 已写入的 sidecar 内容,避免重复写
    _active_turn_messages: list[Json] = field(default_factory=list)  # 进行中回合的暂存消息,未提交前不进入 messages
    _queue_lock: threading.RLock = field(default_factory=threading.RLock)  # 输入队列锁:UI 线程入队 vs agent 线程领取
    _snapshot_lock: threading.RLock = field(default_factory=threading.RLock)  # 快照锁:防止并发保存交错

    def __post_init__(self) -> None:
        self.images = ImageInputs(self)
        if not self.uid:
            self.uid = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + str(uuid.uuid4())[:12]  # noqa: DTZ005 - 会话 ID 有意使用本地墙钟时间,保证可读且唯一
        if self.system_info is None:
            self.system_info = SystemInfo.detect(self.cwd)
        if self.mcp is None:
            from yucode.mcp import MCPManager  # 局部导入:mcp 构建在 session 之上,避免循环依赖

            self.mcp = MCPManager(self)
        if self.skills is None:
            from yucode.skill import SkillLibrary  # 局部导入:skill 构建在 session 之上,避免循环依赖

            self.skills = SkillLibrary.load(self)
        if self.memory is None:
            from yucode.memory import ProjectMemory  # 局部导入:memory 是 session 之上的纵向功能 Module

            directory = os.path.join(SessionSnapshotStore.project_dir(self.config.data_dir, self.cwd), "memory")
            self.memory = ProjectMemory(directory)

    def store_turn_diff(
        self,
        key: str,
        turn: int,
        path: str,
        diff: str,
        *,
        before: str = "",
        after: str = "",
        round: int = 0,
    ) -> None:
        before, after = TurnDiff.bounded_snapshots(before, after)  # 快照过大则双双丢弃
        self.turn_diffs.append(TurnDiff(key, turn, path, diff, before, after, round))
        if len(self.turn_diffs) > 100:
            self.turn_diffs.pop(0)  # 只保留最近 100 条,防止无限增长

    @classmethod
    def from_config_file(cls, *, path: str | None = None, yolo: bool = False, theme: str = "") -> Session:
        data = ConfigFile.load(path)
        return cls(config=Config.from_dict(data), settings=RuntimeSettings.from_dict(data, yolo=yolo, theme=theme))

    def resolve_path(self, path: str) -> str:
        path = os.path.expanduser(path)  # 展开 ~
        return os.path.abspath(path if os.path.isabs(path) else os.path.join(self.cwd, path))  # 相对路径按 cwd 解析

    def relpath(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.cwd)
        except ValueError:
            return path  # 跨盘符等无法相对化时返回原路径

    def in_cwd(self, path: str) -> bool:
        try:
            return os.path.commonpath([os.path.realpath(self.cwd), os.path.realpath(path)]) == os.path.realpath(self.cwd)  # 真实路径比较,防符号链接绕过
        except ValueError:
            return False  # 不同盘符等场景

    def data_path(self, *parts: str) -> str:
        root = os.path.expanduser(self.config.data_dir)
        return os.path.abspath(os.path.join(root if os.path.isabs(root) else os.path.join(self.cwd, root), *parts))  # 相对 data_dir 以 cwd 为基准

    def running_jobs(self) -> list[BackgroundJob]:
        for job in self.jobs.values():
            job.update_status()  # 先刷新状态再过滤
        return [job for job in self.jobs.values() if job.status == "running"]

    def missing_config(self) -> list[str]:
        provider = self.config.provider
        # url/key/model 任一为空即视为配置缺失。
        return [key for key, value in (("provider.url", provider.url), ("provider.key", provider.key), ("provider.model", provider.model)) if not value]

    def store_tool_result(self, name: str, args: ToolArgs, output: str, note: str = "") -> str:
        self.tool_counter += 1
        key = f"tr.{self.tool_counter}"  # 与 TOOL_RECORD_KEY(tr.\d+) 对应的键
        args, output = Text.value(list(args)), Text.clean(output)  # 参数与输出统一归一化
        self.tool_results[key] = output
        self.tool_records.append(ToolResultRecord(key, name, args, output, note))
        if len(self.tool_results) > 400:
            old = self.tool_records.pop(0)  # FIFO 淘汰最旧记录,与 prune_tool_records 的 400 上限一致
            self.tool_results.pop(old.key, None)
        return key

    def enqueue_user_input(self, value: str | UserInput) -> None:
        if isinstance(value, UserInput) and value.images:
            message = self.images.message(value)  # 带图片的输入展开为消息形态
            text = str(message.get("content") or "").strip()
            images = self.images.refs(message)
            draft = str(value)
        else:
            text = Text.clean(str(value).strip())
            images = ()
            draft = text
        if not text:
            return  # 空白输入不入队
        with self._queue_lock:  # 与 agent 线程的 claim/ack 互斥
            self.pending_user_inputs.append(QueuedInput(text, images, draft))

    def claim_user_inputs(self) -> list[QueuedInput]:
        # claim/ack/release 是跨模型重试的事务;即使每一步都很小也保持这个边界。
        with self._queue_lock:
            for item in self.pending_user_inputs:
                item.inflight = True  # 标记在途,防止重试期间重复消费
            return list(self.pending_user_inputs)  # 返回副本,锁外不可修改

    def acknowledge_user_inputs(self, inputs: list[QueuedInput]) -> None:
        with self._queue_lock:
            self.pending_user_inputs = [item for item in self.pending_user_inputs if item not in inputs]  # 只移除已确认的

    def has_inflight_user_inputs(self) -> bool:
        with self._queue_lock:
            return any(item.inflight for item in self.pending_user_inputs)  # 有在途输入说明回合仍在进行

    def release_user_inputs(self) -> None:
        with self._queue_lock:
            for item in self.pending_user_inputs:
                item.inflight = False  # 重试前释放在途标记

    def set_quick_hints(self, hints: list[str]) -> None:
        """空闲提示符处提供的临时下一步输入;整体替换,从不快照。"""
        with self._queue_lock:
            self.quick_hints = tuple(hints)

    def clear_quick_hints(self) -> None:
        with self._queue_lock:
            self.quick_hints = ()

    @staticmethod
    def net_diff_for_path(status: str, path: str, before: str, after: str) -> tuple[str, str, str] | None:
        from yucode.tools import ReadTool  # 局部导入:tools 构建在 session 之上,避免循环依赖

        if before == after:
            return None  # 内容未变化:不产出 diff
        text = "".join(
            difflib.unified_diff(ReadTool.split_lines(before), ReadTool.split_lines(after), fromfile="/dev/null" if not before else path, tofile=path)
        )  # 新建文件时 from 侧用 /dev/null,表示"从无到有"
        return (status, path, text) if text else None  # 空 diff(仅换行差异)不返回

    @classmethod
    def net_diff_sections(cls, diffs: list[TurnDiff], status: str, *, cwd: str = "") -> list[tuple[str, str, str]]:
        states: dict[str, tuple[str, str]] = {}
        legacy: dict[str, list[str]] = {}
        # 每个路径最近一次编辑是否带快照。文件在会话中途超过快照大小上限时,
        # 同一路径可能同时持有两种描述,两者重叠——同时输出会重复该文件的变化。
        snapshot_tail: dict[str, bool] = {}
        paths: list[str] = []
        for diff in diffs:
            if diff.path not in paths:
                paths.append(diff.path)  # 保持路径首次出现顺序
            snapshot_tail[diff.path] = bool(diff.before or diff.after)
            if not diff.before and not diff.after:
                legacy.setdefault(diff.path, []).append(diff.diff)  # 无快照的编辑归入 legacy hunks
                continue
            before, _ = states.get(diff.path, (diff.before, diff.after))
            states[diff.path] = (before, diff.after)  # 起点保持最早,终点推进到最新

        # Bash 可以在两次 Edit 之间移动文件。当某路径的 `.after` 与另一路径的 `.before`
        # 在两侧都唯一匹配时,那就是一次移动的边界:合并进目标路径,
        # 让逻辑历史跟随文件到达最终路径。
        while (move := cls._find_unambiguous_move(states, legacy)) is not None:
            source, target = move
            states[target] = (states[source][0], states[target][1])  # 移动 = 源起点 + 目标终点
            del states[source]

        sections = []
        for path in paths:
            chunk = cls.net_diff_chunk(path, status, states, legacy, snapshot_tail, cwd)
            if chunk:
                sections.append((status, path, chunk.rstrip("\n") + "\n"))  # 保证以单个换行结尾
        return sections

    @classmethod
    def net_diff_chunk(
        cls,
        path: str,
        status: str,
        states: dict[str, tuple[str, str]],
        legacy: dict[str, list[str]],
        snapshot_tail: dict[str, bool],
        cwd: str,
    ) -> str:
        """每个路径一条 diff,且只来自对它的历史的一种描述。"""
        if path in states and snapshot_tail.get(path):
            # 最近一次编辑带快照,记录的 `after` 就是文件最终内容。
            before, after = states[path]
            if legacy_chunks := legacy.get(path, []):
                # 快照只覆盖一个后缀:无快照的编辑发生在第一个快照之前
                # (文件在会话中途缩小到上限以下),它们的起点内容不在 `states` 里。
                # 从第一个快照的 `before` 反推它们的 hunks 恢复起点,让净 diff 覆盖整个路径。
                # 若无法干净应用——它们穿插在快照之间,快照区间已包含其效果,
                # 或文件在 Edit 之外被改动——则快照区间原样保留。
                original = cls._reverse_apply(before, legacy_chunks)
                if original is not None:
                    before = original  # 反推成功:起点回退到无快照编辑之前
            section = cls.net_diff_for_path(status, path, before, after)
            return section[2] if section else ""
        if path in states and not snapshot_tail.get(path):
            # 快照在该路径历史中途停止(文件超过上限);起点内容仍然精确已知。
            # 终点状态取文件当前的磁盘内容;若文件已不存在,则把后续无快照的 hunks
            # 正向应用到最后一个快照的 `after` 上恢复它,不让精确已知的快照历史被丢弃。
            # 两者都不可用时,落到下面的原始 hunks 兜底。
            final = cls._current_content(cwd, path)  # 当前磁盘内容
            if final is None:
                final = cls._forward_apply(states[path][1], legacy.get(path, []))  # 文件删除时用快照 + hunks 恢复
            if final is not None:
                section = cls.net_diff_for_path(status, path, states[path][0], final)
                return section[2] if section else ""
        legacy_chunks = legacy.get(path, [])
        if not legacy_chunks:
            return ""
        # 该文件没有可用快照。尽力而为:把记录的每次 Edit hunks 反向应用到文件当前磁盘状态,
        # 重建编辑前的内容,再输出一条干净的合成 diff。当重建无法唯一定位 hunk 时
        # (如文件在 Edit 之外被改动),退回拼接原始 per-Edit hunks。
        reconstructed = cls._reconstruct_legacy_diff(cwd, path, legacy_chunks, status) if cwd else None
        if reconstructed is not None:
            return reconstructed
        return "\n".join(chunk.rstrip("\n") for chunk in legacy_chunks)

    @staticmethod
    def _current_content(cwd: str, path: str) -> str | None:
        if not cwd:
            return None  # 无 cwd(如旧日志)不做磁盘恢复
        abspath = path if os.path.isabs(path) else os.path.join(cwd, path)
        try:
            with open(abspath, encoding="utf-8") as file:
                return file.read()
        except (OSError, UnicodeDecodeError):
            return None  # 读取失败视为不可恢复

    _HUNK_RE: ClassVar[re.Pattern[str]] = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")  # unified diff 的 hunk 头:@@ -a,b +c,d @@

    @classmethod
    def _reverse_apply(cls, current: str, chunks: list[str]) -> str | None:
        """按时间倒序反向应用给定的 per-Edit hunks,把 `current` 走回到编辑前的状态。
        每个 hunk 的 after 文本必须在缓冲区中唯一出现;若不满足(外部改动、上下文歧义,
        或 hunks 不属于该缓冲区历史),返回 None 让调用方回退。"""
        hunk_pairs: list[tuple[str, str]] = []
        for chunk in chunks:
            pairs = cls._split_hunks(chunk)
            if pairs is None:
                return None
            hunk_pairs.extend(pairs)
        for after_text, before_text in reversed(hunk_pairs):  # 倒序:先撤销最近的编辑
            if not after_text or not before_text:
                return None
            if current.count(after_text) != 1:
                return None  # 唯一性守卫:匹配不唯一即放弃
            current = current.replace(after_text, before_text, 1)
        return current

    @classmethod
    def _forward_apply(cls, current: str, chunks: list[str]) -> str | None:
        """按时间顺序把给定的 per-Edit hunks 正向应用到 `current`,推导出它们产生的内容。
        每个 hunk 的 before 文本必须在缓冲区中唯一出现;若不满足(外部改动或上下文歧义),
        返回 None 让调用方回退。它是 `_reverse_apply` 的镜像:用于文件已不在磁盘时,
        从最后一个快照恢复文件的最终内容。"""
        hunk_pairs: list[tuple[str, str]] = []
        for chunk in chunks:
            pairs = cls._split_hunks(chunk)
            if pairs is None:
                return None
            hunk_pairs.extend(pairs)
        for after_text, before_text in hunk_pairs:  # 正序:依次应用每次编辑
            if not after_text or not before_text:
                return None
            if current.count(before_text) != 1:
                return None  # 唯一性守卫
            current = current.replace(before_text, after_text, 1)
        return current

    @classmethod
    def _reconstruct_legacy_diff(cls, cwd: str, path: str, chunks: list[str], status: str) -> str | None:
        final = cls._current_content(cwd, path)
        if final is None:
            return None
        original = cls._reverse_apply(final, chunks)
        if original is None:
            return None
        section = cls.net_diff_for_path(status, path, original, final)
        return section[2] if section else ""

    @classmethod
    def _split_hunks(cls, chunk: str) -> list[tuple[str, str]] | None:
        pairs: list[tuple[str, str]] = []
        before_lines: list[str] | None = None
        after_lines: list[str] | None = None
        for line in chunk.splitlines():
            if line.startswith(("--- ", "+++ ")):
                continue  # 文件头行跳过
            if cls._HUNK_RE.match(line):
                if before_lines is not None and after_lines is not None:
                    pairs.append(("\n".join(after_lines), "\n".join(before_lines)))  # 结算上一对 (after, before)
                before_lines, after_lines = [], []
                continue
            if before_lines is None or after_lines is None:
                return None  # hunk 头之前出现内容行:格式非法
            if line.startswith("+"):
                after_lines.append(line[1:])
            elif line.startswith("-"):
                before_lines.append(line[1:])
            elif line.startswith(" "):
                before_lines.append(line[1:])  # 上下文行两侧都计入
                after_lines.append(line[1:])
            elif line == "\\ No newline at end of file":
                continue  # 无尾换行标记
            else:
                return None  # 未知行格式:无法解析
        if before_lines is not None and after_lines is not None:
            pairs.append(("\n".join(after_lines), "\n".join(before_lines)))
        return pairs

    @staticmethod
    def _find_unambiguous_move(states: dict[str, tuple[str, str]], legacy: dict[str, list[str]]) -> tuple[str, str] | None:
        sources_by_after: dict[str, list[str]] = {}
        targets_by_before: dict[str, list[str]] = {}
        for path, (before, after) in states.items():
            if path in legacy:
                continue  # 有 legacy hunks 的路径无法精确匹配,不参与移动判定
            if after:
                sources_by_after.setdefault(after, []).append(path)
            if before:
                targets_by_before.setdefault(before, []).append(path)
        for content, sources in sources_by_after.items():
            targets = targets_by_before.get(content, [])
            if len(sources) == 1 and len(targets) == 1 and sources[0] != targets[0]:
                return sources[0], targets[0]  # 两侧唯一匹配且不是同一路径:判为移动
        return None

    def latest_round_diff_sections(self) -> tuple[int, list[tuple[str, str, str]]] | None:
        if not self.turn_diffs:
            return None
        round = max(diff.round or diff.turn for diff in self.turn_diffs)  # 最新一轮的编号
        diffs = [diff for diff in self.turn_diffs if (diff.round or diff.turn) == round]
        return round, self.net_diff_sections(diffs, "edit", cwd=self.cwd)

    def session_diff_sections(self) -> list[tuple[str, str, str]]:
        return self.net_diff_sections(self.turn_diffs, "overall", cwd=self.cwd)

    def record_tool_error(self, key: str, name: str, args: ToolArgs, error: str) -> None:
        self.tool_errors.append(ToolErrorRecord(key, name, Text.value(list(args)), " ".join(Text.clean(error).split())))  # 错误压成单行
        self.tool_errors = self.tool_errors[-5:]  # 只保留最近 5 条

    NAME_WIDTH: ClassVar[int] = 72

    @property
    def name(self) -> str:
        """会话在列表中显示的名称。仅在第一条消息之前为空。"""
        return self.state.name

    def rename(self, text: str) -> str:
        """显式命名会话。用户起的名字永远不会被派生名字替换。"""
        self.state.name, self.state.name_source = self.clip_name(text), "user"
        return self.state.name

    def refresh_name(self) -> str:
        """先锁定一个名称,然后让它跟随目标,直到用户自己命名。

        每次读取都重新派生更简单但不对:压缩最终会丢掉开场消息,昨天以某个名称列出的会话
        不能因为历史被裁剪,今天就用另一个名字出现。因此名称只决定一次,
        只会因更优的来源被修订,绝不会因更晚的来源被替换。
        """
        if self.state.name_source == "user":
            return self.state.name  # 用户命名优先且不变
        if self.state.name_source != "goal" and (goal := self.clip_name(self.state.goal)):
            self.state.name, self.state.name_source = goal, "goal"  # 尚未以 goal 命名时才用 goal
        elif not self.state.name and (opening := self.opening_text()):
            self.state.name, self.state.name_source = self.clip_name(opening), "input"  # 都没有才用开场文本
        return self.state.name

    def opening_text(self) -> str:
        """用户最初提出的要求,取一行。压缩摘要不算。"""
        for message in self.messages:
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, str) or message.get(SESSION_EVENT_KEY):
                continue
            text = ImageInputs.label_text(message).strip()
            if text and not text.startswith(COMPACTION_SUMMARY_TITLE) and not text.startswith(LIVE_FOLLOWUP_PREFIX):
                return text.splitlines()[0]  # 只取第一行
        return ""

    def state_checkpoint_event(self) -> Json:
        return {
            "role": "user",
            "content": WORKING_STATE_CHECKPOINT_TITLE + "\n" + self.state.format(include_summary=True),
            SESSION_EVENT_KEY: "state_checkpoint",  # 迁移用:把工作状态写成持久事件
        }

    @classmethod
    def clip_name(cls, text: str) -> str:
        return Text.clip_width(" ".join(str(text).split()), cls.NAME_WIDTH)  # 按显示宽度截断,避免溢出 UI

    def save_snapshot(self) -> str:
        # Session 拥有持久化边界;调用方不应依赖快照存储的具体实现。
        with self._snapshot_lock, self._queue_lock:  # 双锁:快照一致性 + 队列一致(快照包含排队输入)
            self.refresh_name()  # 保存前刷新派生名称
            return SessionSnapshotStore(self).save()

    @classmethod
    def load_snapshot(cls, uid: str, config: Config | None = None, settings: RuntimeSettings | None = None, cwd: str = "") -> Session:
        return SessionSnapshotStore.load(uid, config=config, settings=settings, cwd=cwd)  # 加载入口,代理到 store
