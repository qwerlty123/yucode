"""本地子 Agent 运行时：调度、状态、持久化与独立会话生命周期。"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace

from yucode.agent_profile import AgentProfile, AgentProfileLibrary
from yucode.base import DISMISSED, Json, LogBlock, ProviderConfig, Text, ToolError, YucodeError
from yucode.engine import Agent, AgentOutcome
from yucode.prompts import LIVE_FOLLOWUP_PREFIX
from yucode.session import Session, SessionSnapshotCodec, SessionSnapshotStore, local_timestamp
from yucode.skill import Skill, SkillLibrary
from yucode.workspace import WorktreeManager


@dataclass(frozen=True)
class AgentSpec:
    """根 Agent 或 `/agents run` 提交的一次委派请求。"""

    description: str
    prompt: str
    subagent_type: str = "general-purpose"
    model: str | None = None
    run_in_background: bool | None = None
    isolation: str | None = None
    context: str | None = None


@dataclass(frozen=True)
class AgentTaskView:
    """调用方可观察的任务快照；运行时内部锁和线程永不泄漏。"""

    task_id: str
    attempt: int
    status: str
    description: str
    profile: str
    model: str
    run_in_background: bool
    isolation: str
    context: str
    result: str = ""
    partial_result: str = ""
    stop_reason: str = ""
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    steps: int = 0
    tool_calls: int = 0
    usage: Json = field(default_factory=dict)
    elapsed_ms: int = 0
    workspace: Json = field(default_factory=dict)
    changed_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    delivery_state: str = "none"
    closed: bool = False
    pending_interaction: Json = field(default_factory=dict)
    revision: int = 0

    TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})

    @property
    def terminal(self) -> bool:
        return self.status in self.TERMINAL

    def to_json(self) -> Json:
        return asdict(self)


@dataclass
class _TaskRecord:
    task_id: str
    attempt: int
    status: str
    description: str
    prompt: str
    profile: AgentProfile
    provider: str
    model: str
    run_in_background: bool
    isolation: str
    context: str
    result: str = ""
    partial_result: str = ""
    stop_reason: str = ""
    error: str = ""
    created_at: str = field(default_factory=local_timestamp)
    started_at: str = ""
    finished_at: str = ""
    steps: int = 0
    tool_calls: int = 0
    usage: Json = field(default_factory=dict)
    elapsed_ms: int = 0
    workspace: Json = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    delivery_state: str = "none"
    closed: bool = False
    pending_interaction: Json = field(default_factory=dict)
    skills_snapshot: list[Json] = field(default_factory=list)
    memory_snapshot: str = ""
    provider_api: str = ""
    provider_url: str = ""
    activities: list[Json] = field(default_factory=list)
    revision: int = 0
    history: list[Json] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    agent: Agent | None = field(default=None, repr=False)
    child: Session | None = field(default=None, repr=False)
    timeout_triggered: bool = field(default=False, repr=False)
    started_monotonic: float = field(default=0.0, repr=False)
    provider_config: ProviderConfig | None = field(default=None, repr=False)

    def view(self) -> AgentTaskView:
        elapsed_ms = self.elapsed_ms
        if self.status not in AgentTaskView.TERMINAL and self.started_monotonic:
            elapsed_ms = max(elapsed_ms, int((time.monotonic() - self.started_monotonic) * 1000))
        return AgentTaskView(
            task_id=self.task_id,
            attempt=self.attempt,
            status=self.status,
            description=self.description,
            profile=self.profile.name,
            model=self.model,
            run_in_background=self.run_in_background,
            isolation=self.isolation,
            context=self.context,
            result=self.result,
            partial_result=self.partial_result,
            stop_reason=self.stop_reason,
            error=self.error,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            steps=self.steps,
            tool_calls=self.tool_calls,
            usage=dict(self.usage),
            elapsed_ms=elapsed_ms,
            workspace=dict(self.workspace),
            changed_files=tuple(self.changed_files),
            warnings=tuple(self.warnings),
            delivery_state=self.delivery_state,
            closed=self.closed,
            pending_interaction=dict(self.pending_interaction),
            revision=self.revision,
        )

    def to_json(self) -> Json:
        return {
            **self.view().to_json(),
            "prompt": self.prompt,
            "profile_snapshot": asdict(self.profile),
            "provider": self.provider,
            "provider_api": self.provider_api,
            "provider_url": self.provider_url,
            "skills_snapshot": list(self.skills_snapshot),
            "memory_snapshot": self.memory_snapshot,
            "activities": list(self.activities),
            "history": list(self.history),
        }

    @classmethod
    def from_json(cls, value: Json) -> _TaskRecord | None:
        try:
            raw_profile = value["profile_snapshot"]
            if not isinstance(raw_profile, dict):
                return None
            profile_data = dict(raw_profile)
            for key in ("tools", "disallowed_tools", "skills", "errors", "warnings"):
                profile_data[key] = tuple(profile_data.get(key) or ())
            profile = AgentProfile(**profile_data)
            return cls(
                task_id=str(value["task_id"]),
                attempt=int(value.get("attempt", 1)),
                status=str(value.get("status") or "interrupted"),
                description=str(value.get("description") or ""),
                prompt=str(value.get("prompt") or ""),
                profile=profile,
                provider=str(value.get("provider") or ""),
                model=str(value.get("model") or ""),
                run_in_background=bool(value.get("run_in_background")),
                isolation=str(value.get("isolation") or "shared"),
                context=str(value.get("context") or "fresh"),
                result=str(value.get("result") or ""),
                partial_result=str(value.get("partial_result") or ""),
                stop_reason=str(value.get("stop_reason") or ""),
                error=str(value.get("error") or ""),
                created_at=str(value.get("created_at") or ""),
                started_at=str(value.get("started_at") or ""),
                finished_at=str(value.get("finished_at") or ""),
                steps=int(value.get("steps", 0)),
                tool_calls=int(value.get("tool_calls", 0)),
                usage=dict(value.get("usage") or {}),
                elapsed_ms=int(value.get("elapsed_ms", 0)),
                workspace=dict(value.get("workspace") or {}),
                changed_files=[str(item) for item in value.get("changed_files") or ()],
                warnings=[str(item) for item in value.get("warnings") or ()],
                delivery_state=str(value.get("delivery_state") or "none"),
                closed=bool(value.get("closed")),
                pending_interaction=dict(value.get("pending_interaction") or {}),
                skills_snapshot=[dict(item) for item in value.get("skills_snapshot") or () if isinstance(item, dict)],
                memory_snapshot=str(value.get("memory_snapshot") or ""),
                provider_api=str(value.get("provider_api") or ""),
                provider_url=str(value.get("provider_url") or ""),
                activities=[dict(item) for item in value.get("activities") or () if isinstance(item, dict)][-20:],
                revision=int(value.get("revision", 0)),
                history=[dict(item) for item in value.get("history") or () if isinstance(item, dict)],
            )
        except (KeyError, TypeError, ValueError):
            return None


class SubagentStore:
    """以只追加 registry 记录任务事实，并给每个任务预留独立 transcript 路径。"""

    def __init__(self, root: Session):
        # Session 的相对 data_dir 以项目 cwd 为基准；先固定成绝对路径，避免 worktree/进程 cwd 改变落盘位置。
        project = SessionSnapshotStore.project_dir(root.data_path(), root.cwd)
        self.directory = os.path.join(project, root.uid + ".agents")
        self.registry_path = os.path.join(self.directory, "registry.jsonl")
        self._lock = threading.RLock()

    def transcript_path(self, task_id: str, attempt: int) -> str:
        del attempt  # 所有 attempt 追加到同一 sidechain，resume 才能保留完整对话。
        return os.path.join(self.directory, task_id + ".jsonl")

    def save(self, task: _TaskRecord, *, append_registry: bool = True) -> None:
        payload = task.to_json()
        with self._lock:
            os.makedirs(self.directory, exist_ok=True)
            if append_registry:
                SessionSnapshotStore.write_jsonl(self.registry_path, payload, mode="a")
            path = os.path.join(self.directory, task.task_id + ".meta.json")
            temp = path + ".tmp"
            SessionSnapshotStore.write_jsonl(temp, payload, mode="w")
            os.replace(temp, path)

    def load(self) -> dict[str, _TaskRecord]:
        tasks: dict[str, _TaskRecord] = {}
        try:
            with open(self.registry_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    task = _TaskRecord.from_json(value) if isinstance(value, dict) else None
                    if task is not None:
                        tasks[task.task_id] = task
        except OSError:
            pass
        # meta 由临时文件原子替换；registry 尾行在崩溃中损坏时，用 revision 更新的
        # sidecar 补回任务。若 registry 更靠后，则仍以只追加记录为准。
        try:
            names = tuple(os.listdir(self.directory))
        except OSError:
            names = ()
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            try:
                with open(os.path.join(self.directory, name), encoding="utf-8") as handle:
                    value = json.loads(handle.readline())
            except (OSError, json.JSONDecodeError):
                continue
            task = _TaskRecord.from_json(value) if isinstance(value, dict) else None
            if task is None:
                continue
            current = tasks.get(task.task_id)
            if current is None or task.revision > current.revision:
                tasks[task.task_id] = task
        return tasks


Executor = Callable[[Session, str], str | AgentOutcome]


@dataclass(frozen=True)
class SubagentEvent:
    """后台线程发布给主界面的有序事件。"""

    kind: str
    task_id: str
    attempt: int
    revision: int
    status: str
    text: str = ""


@dataclass
class _InteractionWaiter:
    event: threading.Event = field(default_factory=threading.Event)
    response: str = ""


class InteractionBroker:
    """把子 Agent 的 Ask 和工具审批转换为可持久化、可校验的请求。"""

    def __init__(self, runtime: SubagentRuntime):
        self.runtime = runtime
        self._waiters: dict[str, _InteractionWaiter] = {}

    def request(self, task_id: str, kind: str, payload: Json) -> str:
        if kind not in {"ask", "approval"}:
            raise ToolError("子 Agent 交互类型必须是 ask 或 approval")
        normalized = Text.value(payload)
        request_id = "interaction-" + uuid.uuid4().hex[:12]
        waiter = _InteractionWaiter()
        with self.runtime._lock:
            task = self.runtime._require(task_id)
            if task.status != "running":
                raise ToolError(f"任务 {task_id} 当前不能发起交互: {task.status}")
            digest_value = {"task_id": task_id, "attempt": task.attempt, "kind": kind, "payload": normalized}
            encoded = json.dumps(digest_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            arguments = normalized.get("arguments", {})
            bounded_arguments = self._bounded(arguments)
            preview = Text.clean(str(normalized.get("preview") or ""))[:4000]
            summary = Text.clean(str(normalized.get("content_summary") or preview))[:1000]
            if not summary and arguments:
                summary = json.dumps(bounded_arguments, ensure_ascii=False, sort_keys=True)[:1000]
            request = {
                "task_id": task_id,
                "attempt": task.attempt,
                "request_id": request_id,
                "digest": digest,
                "kind": kind,
                "tool_name": str(normalized.get("tool_name") or ("Ask" if kind == "ask" else "")),
                "arguments": bounded_arguments,
                "arguments_truncated": bounded_arguments != arguments,
                "cwd": str(normalized.get("cwd") or (task.child.cwd if task.child else self.runtime.root.cwd)),
                "preview": preview,
                "content_summary": summary,
                "question": Text.clean(str(normalized.get("question") or ""))[:4000],
                "choices": self._bounded(list(normalized.get("choices") or [])),
                "previews": self._bounded(list(normalized.get("previews") or [])),
                "recommended": normalized.get("recommended"),
                "position": Text.clean(str(normalized.get("position") or ""))[:1000],
                "status": "pending",
                "created_at": local_timestamp(),
            }
            task.pending_interaction = request
            task.status = "waiting_interaction"
            self._waiters[request_id] = waiter
            self.runtime._persist(task, "interaction_requested")
            handler = self.runtime.root.subagent_interaction_handler if not task.run_in_background else None
            channel_available = self.runtime.root.subagent_interaction_available

        if handler is not None:
            try:
                response = str(handler(dict(request)))
            except Exception as error:  # noqa: BLE001 - 交互 UI 故障按拒绝处理，不能悬挂 worker
                response = "n" if kind == "approval" else DISMISSED
                with self.runtime._lock:
                    task.warnings.append("交互处理失败: " + (str(error).strip() or error.__class__.__name__))
            self.respond(task_id, request_id, digest, response)
        elif not channel_available:
            self.respond(task_id, request_id, digest, "n" if kind == "approval" else DISMISSED)

        while not waiter.event.wait(0.1):
            with self.runtime._lock:
                task = self.runtime._require(task_id)
                if task.cancel_event.is_set() or task.status in AgentTaskView.TERMINAL:
                    self._answer_locked(task, request_id, "n" if kind == "approval" else DISMISSED, "cancelled")
        return waiter.response

    @classmethod
    def _bounded(cls, value, *, depth: int = 0):
        """给持久化/UI 副本设界，digest 仍覆盖完整规范化请求。"""

        if depth >= 8:
            return "<truncated>"
        if isinstance(value, str):
            return Text.clean(value)[:2000]
        if isinstance(value, dict):
            return {str(key)[:200]: cls._bounded(item, depth=depth + 1) for key, item in list(value.items())[:100]}
        if isinstance(value, list):
            return [cls._bounded(item, depth=depth + 1) for item in value[:100]]
        return value

    def respond(self, task_id: str, request_id: str, digest: str, response: str) -> AgentTaskView:
        with self.runtime._lock:
            task = self.runtime._require(task_id)
            pending = task.pending_interaction
            if pending.get("request_id") != request_id:
                raise ToolError("交互 request_id 已失效或不匹配")
            if pending.get("digest") != digest:
                raise ToolError("交互 digest 不匹配")
            if pending.get("status") != "pending":
                if pending.get("status") == "invalid":
                    raise ToolError("交互请求已失效；请 resume 任务后重新发起")
                return task.view()  # answered/cancelled 的重复响应保持幂等。
            self._answer_locked(task, request_id, Text.clean(response), "answered")
            return task.view()

    def _answer_locked(self, task: _TaskRecord, request_id: str, response: str, status: str) -> None:
        waiter = self._waiters.get(request_id)
        pending = dict(task.pending_interaction)
        pending["status"] = status
        pending["answered_at"] = local_timestamp()
        pending["response"] = Text.clean(response)[:1000]
        task.pending_interaction = pending
        if task.status == "waiting_interaction":
            task.status = "running"
        self.runtime._persist(task, "interaction_" + status)
        if waiter is not None:
            waiter.response = response
            waiter.event.set()
            self._waiters.pop(request_id, None)


class SubagentRuntime:
    """子 Agent 的唯一外部 seam；调用方不直接管理线程、会话或磁盘记录。"""

    ACTIVE = frozenset({"queued", "running", "waiting_interaction"})
    HARD_DENY = ("Agent", "AgentTask", "NextHints", "Memory")

    def __init__(self, root: Session, *, executor: Executor | None = None):
        self.root = root
        self.profiles = AgentProfileLibrary.load(root)
        self.store = SubagentStore(root)
        self._executor = executor or self._execute_with_agent
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self.events: queue.Queue[SubagentEvent] = queue.Queue(maxsize=1000)
        self.interactions = InteractionBroker(self)
        self.worktrees = WorktreeManager(root)
        self._running = 0
        self._foreground_waiting: list[str] = []
        self._worker_identity = threading.local()
        self._closing = False
        self._tasks = self.store.load()
        self._recover_interrupted_tasks()
        root.subagents = self

    def reload_profiles(self) -> AgentProfileLibrary:
        with self._lock:
            self.profiles = AgentProfileLibrary.load(self.root)
            return self.profiles

    def spawn(self, spec: AgentSpec, *, caller_task_id: str | None = None) -> AgentTaskView:
        launched = self.start(spec, caller_task_id=caller_task_id)
        return launched if launched.run_in_background else self.wait_foreground(launched.task_id)

    def start(self, spec: AgentSpec, *, caller_task_id: str | None = None) -> AgentTaskView:
        """先持久化并启动任务，不因前台模式阻塞调用方。"""

        ambient_caller = str(getattr(self._worker_identity, "task_id", ""))
        if caller_task_id is not None or ambient_caller:
            raise ToolError("子 Agent 不允许再启动子 Agent")
        description = spec.description.strip()
        prompt = spec.prompt.strip()
        if not description or not prompt:
            raise ToolError("Agent 需要非空的 description 和 prompt")
        profile = self.profiles.get(spec.subagent_type)
        if profile is None:
            raise ToolError(f"未知 subagent_type: {spec.subagent_type}")
        if not profile.valid:
            raise ToolError(f"Agent profile {profile.name} 无效: " + "; ".join(profile.errors))
        provider_name = self.root.config.active_provider
        provider_config = copy.deepcopy(self.root.config.providers[provider_name])
        model = self._model(spec.model or profile.model, provider_config)
        background = profile.background if spec.run_in_background is None else spec.run_in_background
        isolation = self._choice("isolation", spec.isolation or profile.isolation, {"shared", "worktree"})
        context = self._choice("context", spec.context or profile.context, {"fresh", "fork"})
        provider_config.model = model
        provider_contract = provider_config.resolve()
        with self._lock:
            if self._closing:
                raise ToolError("SubagentRuntime 正在关闭")
            active = sum(task.status in self.ACTIVE for task in self._tasks.values())
            capacity = self.root.settings.max_parallel_agents + self.root.settings.max_queued_agents
            if active >= capacity:
                raise ToolError(f"子 Agent 队列已满 ({active}/{capacity})")
            task_id = "agent-" + uuid.uuid4().hex[:12]
            task = _TaskRecord(
                task_id=task_id,
                attempt=1,
                status="queued",
                description=description,
                prompt=prompt,
                profile=profile,
                provider=provider_name,
                model=model,
                run_in_background=bool(background),
                isolation=isolation,
                context=context,
                warnings=list(profile.warnings),
                skills_snapshot=[asdict(skill) for skill in self.root.skills.all()] if self.root.skills is not None else [],
                memory_snapshot=self.root.memory.context() if self.root.memory is not None else "",
                provider_api=provider_contract.api,
                provider_url=provider_contract.base_url,
                provider_config=provider_config,
            )
            self._tasks[task_id] = task
            self._persist(task, "launched")  # 先记录 launch，再允许 worker 获得执行权。
            thread = threading.Thread(target=self._run_task, args=(task,), name=task_id, daemon=True)
            task.thread = thread
            try:
                thread.start()
            except Exception as error:  # noqa: BLE001 - worker 启动失败也必须成为可恢复任务事实
                task.thread = None
                task.error = str(error).strip() or error.__class__.__name__
                self._finish(task, "failed", "worker-start")
            return task.view()

    def list(self, *, include_closed: bool = False) -> list[AgentTaskView]:
        with self._lock:
            tasks = [task.view() for task in self._tasks.values() if include_closed or not task.closed]
        return sorted(tasks, key=lambda task: (task.created_at, task.task_id), reverse=True)

    def get(self, task_id: str) -> AgentTaskView:
        with self._lock:
            return self._require(task_id).view()

    def foreground_task(self) -> AgentTaskView | None:
        """返回根 Agent 当前真正阻塞等待的前台任务，供 TUI 决定是否展示 detach 提示。"""

        with self._lock:
            task = next(
                (
                    self._tasks[task_id]
                    for task_id in reversed(self._foreground_waiting)
                    if task_id in self._tasks and self._tasks[task_id].status in self.ACTIVE and not self._tasks[task_id].run_in_background
                ),
                None,
            )
            return task.view() if task is not None else None

    def details(self, task_id: str) -> Json:
        with self._lock:
            task = self._require(task_id)
            return {
                **task.view().to_json(),
                "prompt": task.prompt,
                "current_activity": task.partial_result,
                "recent_activity": list(task.activities),
                "attempts": [
                    *task.history,
                    {**task.view().to_json(), "prompt": task.prompt, "recent_activity": list(task.activities)},
                ],
            }

    def transcript(self, task_id: str) -> Json:
        """返回 child 已持久化的只读转录视图；活动任务先做一次线程安全快照。"""

        with self._lock:
            task = self._require(task_id)
            child = task.child
            path = self.store.transcript_path(task.task_id, task.attempt)
            header = {
                "task_id": task.task_id,
                "attempt": task.attempt,
                "status": task.status,
                "description": task.description,
                "prompt": task.prompt,
                "profile": task.profile.name,
                "model": task.model,
                "steps": task.steps,
                "tool_calls": task.tool_calls,
                "usage": dict(task.usage),
                "elapsed_ms": task.view().elapsed_ms,
            }
        if child is not None:
            # 查看器不能因为一次快照写入失败而打断正在执行的 child；下面仍可回退到内存视图。
            with contextlib.suppress(OSError, TypeError, ValueError, YucodeError):
                child.save_snapshot()
        try:
            data, _blobs, _snapshot_header = SessionSnapshotStore.read_merged(path)
            messages = SessionSnapshotCodec.persistable_messages(list(data.get("messages") or ()))
            records = list(data.get("tool_records") or ())
            errors = list(data.get("tool_errors") or ())
        except (OSError, TypeError, ValueError, YucodeError):
            if child is None:
                messages, records, errors = [], [], []
            else:
                messages = copy.deepcopy([*child.messages, *child._active_turn_messages])
                records = [asdict(record) for record in child.tool_records]
                errors = [asdict(error) for error in child.tool_errors]
        return {
            **header,
            "messages": [message for message in messages if not SessionSnapshotCodec.is_internal_message(message)],
            "tool_records": records,
            "tool_errors": errors,
        }

    def wait(self, task_id: str, timeout_seconds: float | None = None) -> AgentTaskView:
        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, min(120.0, timeout_seconds))
        with self._changed:
            task = self._require(task_id)
            while task.status in self.ACTIVE:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._changed.wait(remaining)
            return task.view()

    def wait_foreground(self, task_id: str, timeout_seconds: float | None = None) -> AgentTaskView:
        """等待前台任务结束；detach 会在不中断 worker 的情况下提前解除等待。"""

        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, min(120.0, timeout_seconds))
        with self._changed:
            task = self._require(task_id)
            self._foreground_waiting.append(task_id)
            try:
                while task.status in self.ACTIVE and not task.run_in_background:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        break
                    self._changed.wait(remaining)
                return task.view()
            finally:
                with contextlib.suppress(ValueError):
                    self._foreground_waiting.remove(task_id)

    def wait_change(self, task_id: str, timeout_seconds: float = 120) -> AgentTaskView:
        """等待任务 revision 变化，供 AgentTask wait 和界面刷新使用。"""

        timeout_seconds = max(0.0, min(120.0, timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        with self._changed:
            task = self._require(task_id)
            revision = task.revision
            while task.revision == revision and not task.view().terminal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            return task.view()

    def detach(self, task_id: str) -> AgentTaskView:
        with self._lock:
            task = self._require(task_id)
            if task.status not in self.ACTIVE:
                return task.view()
            if not task.run_in_background:
                task.run_in_background = True
                self._persist(task, "detached")
            return task.view()

    def detach_foreground(self) -> AgentTaskView | None:
        with self._lock:
            task = next(
                (self._tasks[task_id] for task_id in reversed(self._foreground_waiting) if task_id in self._tasks),
                None,
            )
            if task is None or task.status not in self.ACTIVE or task.run_in_background:
                return None
            task.run_in_background = True
            self._persist(task, "detached")
            return task.view()

    def steer(self, task_id: str, message: str) -> AgentTaskView:
        message = message.strip()
        if not message:
            raise ToolError("steer 需要非空 message")
        with self._lock:
            task = self._require(task_id)
            if task.status not in {"running", "waiting_interaction"} or task.child is None:
                raise ToolError(f"任务 {task_id} 当前不能 steer: {task.status}")
            task.child.enqueue_user_input(message)
            task.child.save_snapshot()  # 先保存 child 队列，进程重启后的 resume 才不会丢 steer。
            self._persist(task, "steered")
            return task.view()

    def stop(self, task_id: str) -> AgentTaskView:
        with self._lock:
            task = self._require(task_id)
            if task.status not in self.ACTIVE:
                return task.view()
            task.cancel_event.set()
            agent = task.agent
            child = task.child
            if task.status == "queued":
                self._finish(task, "cancelled", "stopped")
        if agent is not None:
            threading.Thread(target=agent.cancel, name=f"{task_id}-cancel", daemon=True).start()
        if child is not None:
            self._request_child_resource_cancel(child, task_id)
        with self._lock:
            return task.view()

    def stop_all(self) -> list[AgentTaskView]:
        with self._lock:
            task_ids = [task.task_id for task in self._tasks.values() if task.status in self.ACTIVE]
        return [self.stop(task_id) for task_id in task_ids]

    def respond_interaction(self, task_id: str, request_id: str, digest: str, response: str) -> AgentTaskView:
        return self.interactions.respond(task_id, request_id, digest, response)

    def claim_notifications(self) -> list[AgentTaskView]:
        """领取待注入的后台终态；未确认前重复领取会返回相同任务。"""

        with self._lock:
            claimed = []
            for task in self._tasks.values():
                if task.closed or task.delivery_state not in {"pending", "delivered"}:
                    continue
                if task.delivery_state == "pending":
                    task.delivery_state = "delivered"
                    self._persist(task, "notification_delivered")
                claimed.append(task.view())
            return sorted(claimed, key=lambda item: (item.finished_at, item.task_id))

    def acknowledge_notifications(self, task_ids: list[str]) -> None:
        """模型请求成功接收通知后确认，之后不再重复注入。"""

        with self._lock:
            for task_id in dict.fromkeys(task_ids):
                task = self._require(task_id)
                if task.delivery_state == "delivered":
                    task.delivery_state = "consumed"
                    self._persist(task, "notification_consumed")

    def release_notifications(self, task_ids: list[str]) -> None:
        """请求在送达前失败时释放领取状态，保留下一次注入机会。"""

        with self._lock:
            for task_id in dict.fromkeys(task_ids):
                task = self._require(task_id)
                if task.delivery_state == "delivered":
                    task.delivery_state = "pending"
                    self._persist(task, "notification_released")

    def drain_events(self) -> list[SubagentEvent]:
        events: list[SubagentEvent] = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    @staticmethod
    def notification_message(task: AgentTaskView) -> Json:
        """生成不可由子 Agent 正文伪造的根会话完成通知。"""

        envelope = task.to_json()
        return {
            "role": "user",
            "content": "--- SUBAGENT COMPLETION ---\n" + json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            "_session_event": "subagent_completion",
        }

    def resume(self, task_id: str, message: str) -> AgentTaskView:
        message = message.strip()
        if not message:
            raise ToolError("resume 需要非空 message")
        with self._lock:
            if self._closing:
                raise ToolError("SubagentRuntime 正在关闭")
            task = self._require(task_id)
            if task.status not in AgentTaskView.TERMINAL:
                raise ToolError(f"任务 {task_id} 当前不能 resume: {task.status}")
            active = sum(item.status in self.ACTIVE for item in self._tasks.values())
            capacity = self.root.settings.max_parallel_agents + self.root.settings.max_queued_agents
            if active >= capacity:
                raise ToolError(f"子 Agent 队列已满 ({active}/{capacity})")
            transcript = self.store.transcript_path(task.task_id, task.attempt)
            has_transcript = os.path.isfile(transcript)
            restart_fresh = not has_transcript and not task.started_at and task.context == "fresh"
            if not has_transcript and not restart_fresh:
                raise ToolError(f"任务 {task_id} 缺少 transcript，无法安全 resume")
            previous_prompt = task.prompt
            task.history.append({**task.view().to_json(), "prompt": previous_prompt, "recent_activity": list(task.activities)})
            task.attempt += 1
            task.status = "queued"
            task.prompt = message if has_transcript else previous_prompt + "\n\n追加要求：" + message
            task.result = ""
            task.partial_result = ""
            task.stop_reason = ""
            task.error = ""
            task.started_at = ""
            task.finished_at = ""
            task.steps = 0
            task.tool_calls = 0
            task.usage = {}
            task.elapsed_ms = 0
            task.delivery_state = "none"
            task.closed = False
            task.pending_interaction = {}
            task.activities = []
            task.cancel_event = threading.Event()
            task.timeout_triggered = False
            task.started_monotonic = 0.0
            task.agent = None
            task.child = None
            self._persist(task, "resumed")
            thread = threading.Thread(target=self._run_task, args=(task,), name=f"{task_id}-{task.attempt}", daemon=True)
            task.thread = thread
            try:
                thread.start()
            except Exception as error:  # noqa: BLE001 - 恢复启动失败也必须成为新 attempt 的终态事实
                task.thread = None
                task.error = str(error).strip() or error.__class__.__name__
                self._finish(task, "failed", "worker-start")
            launched = task.view()
        return launched if task.run_in_background else self.wait_foreground(task_id)

    def close_task(self, task_id: str) -> AgentTaskView:
        with self._lock:
            task = self._require(task_id)
            if task.status in self.ACTIVE:
                raise ToolError("运行中的任务不能 close；请先 stop")
            task.closed = True
            task.delivery_state = "consumed"
            self._persist(task, "closed")
            return task.view()

    def clean_task(self, task_id: str, *, confirmed: bool = False) -> AgentTaskView:
        with self._lock:
            task = self._require(task_id)
            if task.status in self.ACTIVE:
                raise ToolError("运行中的任务不能 clean")
            if task.isolation != "worktree":
                raise ToolError("shared 任务没有可清理的 worktree")
            if task.workspace.get("cleanup_state") != "cleaned" and not confirmed:
                raise ToolError("删除保留的 worktree 成果需要显式确认")
            task.workspace = self.worktrees.clean(task.workspace)
            self._persist(task, "workspace_cleaned")
            if task.workspace.get("cleanup_state") == "cleanup_failed":
                raise ToolError("worktree 清理失败: " + str(task.workspace.get("cleanup_error") or "未知错误"))
            return task.view()

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            active = [task for task in self._tasks.values() if task.status in self.ACTIVE]
            for task in active:
                task.cancel_event.set()
            self._changed.notify_all()
        for task in active:
            if task.agent is not None:
                threading.Thread(target=task.agent.cancel, name=f"{task.task_id}-cancel", daemon=True).start()
            if task.child is not None:
                self._request_child_resource_cancel(task.child, task.task_id)
        deadline = time.monotonic() + self.root.settings.agent_shutdown_grace_seconds
        for task in active:
            thread = task.thread
            if thread is not None and thread.ident is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            for task in active:
                if task.status in self.ACTIVE:
                    self._finish(task, "interrupted", "shutdown-timeout")

    def _run_task(self, task: _TaskRecord) -> None:
        self._worker_identity.task_id = task.task_id  # alias 工具即使遗漏显式 caller，runtime 也能识别 child worker。
        acquired = False
        writer_acquired = False
        terminal_status = "failed"
        terminal_reason = "error"
        terminal_result = ""
        terminal_partial = ""
        terminal_error = ""
        timer: threading.Timer | None = None
        try:
            if task.isolation == "shared" and self._needs_workspace_writer(task):
                assert self.root.workspace_lease is not None
                # 等租约的 writer 不占 worker 槽，避免一排 shared writer 饿死只读任务。
                self.root.workspace_lease.acquire(task.task_id, wait=True, cancelled=task.cancel_event)
                writer_acquired = True
            acquired = self._acquire_slot(task)
            if not acquired:
                with self._lock:
                    if task.status in self.ACTIVE:
                        status = "cancelled" if task.cancel_event.is_set() else "interrupted"
                        reason = "stopped" if task.cancel_event.is_set() else "shutdown"
                        self._finish(task, status, reason)
                return
            with self._lock:
                if task.cancel_event.is_set() or self._closing:
                    if task.status in self.ACTIVE:
                        status = "cancelled" if task.cancel_event.is_set() else "interrupted"
                        reason = "stopped" if task.cancel_event.is_set() else "shutdown"
                        self._finish(task, status, reason)
                    return
            if task.isolation == "worktree":
                workspace = self.worktrees.prepare(task.task_id, task.workspace)
            else:
                workspace = {"mode": "shared", "path": self.root.cwd, "cleanup_state": "not_applicable"}
            with self._lock:
                task.workspace = workspace
                if task.cancel_event.is_set() or self._closing:
                    if task.status in self.ACTIVE:
                        status = "cancelled" if task.cancel_event.is_set() else "interrupted"
                        reason = "stopped" if task.cancel_event.is_set() else "shutdown"
                        self._finish(task, status, reason)
                    return
                task.status = "running"
                task.started_at = local_timestamp()
                task.started_monotonic = time.monotonic()
                self._persist(task, "running")
            child = self._child_session(task)
            with self._lock:
                task.child = child
                if task.cancel_event.is_set() or self._closing:
                    raise KeyboardInterrupt
            timeout = task.profile.timeout_seconds
            if timeout > 0:
                timer = threading.Timer(timeout, self._timeout, args=(task,))
                timer.daemon = True
                timer.start()
            if child.mcp is not None:
                child.mcp.discover_auto()
            with self._lock:
                if task.cancel_event.is_set() or self._closing:
                    raise KeyboardInterrupt
            raw_outcome = self._executor(child, task.prompt)
            outcome = raw_outcome if isinstance(raw_outcome, AgentOutcome) else AgentOutcome("completed", "completed", str(raw_outcome).strip())
            terminal_status = outcome.status
            terminal_reason = outcome.stop_reason
            terminal_result = outcome.result.strip()
            terminal_partial = outcome.partial_result.strip()
            terminal_error = outcome.error.strip()
            if task.cancel_event.is_set():
                terminal_status = "cancelled"
                terminal_reason = "timeout" if task.timeout_triggered else "stopped"
        except KeyboardInterrupt:
            terminal_status = "cancelled"
            terminal_reason = "timeout" if task.timeout_triggered else "stopped"
        except Exception as error:  # noqa: BLE001 - worker 的每个错误都必须转换为任务终态
            terminal_status = "failed"
            terminal_reason = "error"
            terminal_error = str(error).strip() or error.__class__.__name__
        finally:
            if timer is not None:
                timer.cancel()
            self._cleanup_child(task)
            self._finalize_workspace(task)
            if writer_acquired and self.root.workspace_lease is not None:
                self.root.workspace_lease.release(task.task_id)
            with self._lock:
                if task.status in self.ACTIVE:
                    task.result = terminal_result
                    task.partial_result = terminal_partial or task.partial_result
                    task.error = terminal_error
                    if task.child is not None:
                        self._collect_metrics(task, task.child)
                    self._finish(task, terminal_status, terminal_reason)
            if acquired:
                self._release_slot()
            with contextlib.suppress(AttributeError):
                del self._worker_identity.task_id

    def _acquire_slot(self, task: _TaskRecord) -> bool:
        """按当前配置领取 worker 槽；运行中修改并发上限会作用于排队任务。"""

        with self._changed:
            while not task.cancel_event.is_set() and not self._closing:
                if self._running < self.root.settings.max_parallel_agents:
                    self._running += 1
                    return True
                self._changed.wait(0.1)
            return False

    def _release_slot(self) -> None:
        with self._changed:
            self._running = max(0, self._running - 1)
            self._changed.notify_all()

    def _child_session(self, task: _TaskRecord) -> Session:
        config = copy.deepcopy(self.root.config)
        config.data_dir = self.root.data_path()  # 相对 data_dir 不能在 worktree cwd 下二次解析。
        if task.provider not in config.providers:
            raise ToolError(f"任务启动时的 provider 已不存在: {task.provider}")
        config.active_provider = task.provider  # 排队期间根会话切换 provider 不能改变旧任务的凭据域。
        provider = copy.deepcopy(task.provider_config or config.provider)
        provider.model = task.model
        contract = provider.resolve()
        if task.provider_config is None and task.provider_api and (contract.api != task.provider_api or contract.base_url != task.provider_url):
            raise ToolError("原 provider 的 API 或 URL 已变更，不能安全回放子 Agent transcript")
        config.providers[task.provider] = provider
        max_steps = task.profile.max_steps or self.root.settings.max_subagent_steps
        settings = replace(self.root.settings, max_steps=max_steps, quick_hints=False)
        assert self.root.tool_catalog is not None
        child_safe = tuple(tool.NAME for tool in self.root.tool_catalog if tool.CHILD_SAFE)
        allowed = task.profile.tool_names(child_safe, self.HARD_DENY)
        catalog = self.root.tool_catalog.filtered(allow=allowed, deny=self.HARD_DENY)
        transcript = self.store.transcript_path(task.task_id, task.attempt)
        cwd = str(task.workspace.get("path") or self.root.cwd)
        has_transcript = os.path.isfile(transcript)
        if task.attempt > 1 and has_transcript:
            child = SessionSnapshotStore.load_path(transcript, config=config, settings=settings, cwd=cwd)
            child.messages = self._complete_messages(child.messages)
            pending = child.claim_user_inputs()
            if pending:
                # 旧 attempt 中已 steer 但未发送的跟进先于新 resume message 发生，回放顺序不能倒置。
                child.messages.extend(item.message(LIVE_FOLLOWUP_PREFIX) for item in pending)
                child.acknowledge_user_inputs(pending)
            child.skills = self._skill_library(task)
            child.memory = None
            child.tool_catalog = catalog
        else:
            if task.attempt > 1:
                previous = task.history[-1] if task.history else {}
                if previous.get("started_at") or task.context != "fresh":
                    raise ToolError(f"任务 {task.task_id} 缺少 transcript，无法恢复")
            forked = task.context == "fork"
            messages = copy.deepcopy([*self.root.messages, *self.root._active_turn_messages]) if forked else []
            fork_state = (
                {
                    "state": copy.deepcopy(self.root.state),
                    "tool_results": copy.deepcopy(self.root.tool_results),
                    "tool_records": copy.deepcopy(self.root.tool_records),
                    "tool_errors": copy.deepcopy(self.root.tool_errors),
                    "history": copy.deepcopy(self.root.history),
                    "tool_counter": self.root.tool_counter,
                }
                if forked
                else {}
            )
            child = Session(
                cwd=cwd,
                config=config,
                settings=settings,
                messages=self._complete_messages(messages),
                skills=self._skill_library(task),
                memory=None,
                tool_catalog=catalog,
                uid=task.task_id,
                **fork_state,
            )
        child.subagent_task_id = task.task_id
        child.cancellation_event = task.cancel_event
        child.memory = None  # Session 默认会创建 ProjectMemory，child 用持久化文本快照取代它。
        child.memory_context_snapshot = task.memory_snapshot
        child.authorization_settings = self.root.settings
        child.workspace_owner = task.task_id
        child.snapshot_path = transcript  # 图片物化之前先固定 sidechain 路径，避免写入默认 session assets。
        if task.context == "fork":
            child.images.fallback_asset_dirs = (self.root.images.assets_dir(),)
            child.images.materialize_messages(child.messages)
        if task.isolation == "shared":
            child.workspace_lease = self.root.workspace_lease
        child.system_prompt = self._system_prompt(task, child.skills)
        return child

    def _execute_with_agent(self, child: Session, prompt: str) -> str | AgentOutcome:
        task_id = child.uid
        with self._lock:
            task = self._require(task_id)
        agent = Agent(child, input_fn=lambda _prompt: "n", output_fn=lambda value: self._activity(task, value))
        agent.tools.confirmation_fn = lambda call, _tool, preview: self._confirm(task, call.name, call.args, preview)
        agent.tools.question_fn = lambda spec, position: self.interactions.request(
            task.task_id,
            "ask",
            {
                "tool_name": "Ask",
                "question": spec.question,
                "choices": list(spec.choices or ()),
                "previews": list(spec.previews or ()),
                "recommended": spec.recommended,
                "position": position,
                "cwd": child.cwd,
            },
        )
        with self._lock:
            task.agent = agent
            cancelled = task.cancel_event.is_set() or self._closing
        if cancelled:
            agent.cancel()
            return AgentOutcome("cancelled", "stopped")
        return agent.run_outcome(prompt)

    def _confirm(self, task: _TaskRecord, tool_name: str, arguments: list, preview: str) -> tuple[bool, str]:
        answer = self.interactions.request(
            task.task_id,
            "approval",
            {"tool_name": tool_name, "arguments": arguments, "cwd": task.child.cwd if task.child else self.root.cwd, "preview": preview},
        ).strip()
        lower = answer.lower()
        return (True, "") if lower in {"", "y", "yes"} else (False, "" if lower in {"n", "no"} else answer)

    def _activity(self, task: _TaskRecord, value: object) -> None:
        detail = Text.clean(str(value)).strip()
        preview = " ".join(detail.split())
        if not preview:
            return
        activity: Json = {
            "at": local_timestamp(),
            "kind": "tool" if isinstance(value, LogBlock) else "message",
            "text": preview[-1000:],
            "detail": detail[-8000:],
        }
        if isinstance(value, LogBlock):
            first = next(value.walk(), None)
            if first is not None:
                line, _level = first
                label = " ".join(part for part in (line.label, line.text, line.meta) if part).strip()
                if label:
                    activity["label"] = label[:1000]
        with self._lock:
            task.partial_result = str(activity["text"])
            task.activities.append(activity)
            task.activities = task.activities[-20:]
            if task.child is not None:
                self._collect_metrics(task, task.child)
            task.revision += 1
            # 活动频率高于状态迁移：只原子更新 meta，既保证崩溃后可见，又不让 append-only registry 快速膨胀。
            self.store.save(task, append_registry=False)
            self._event(task, "activity", task.partial_result)
            self._changed.notify_all()

    def _cleanup_child(self, task: _TaskRecord) -> None:
        child = task.child
        if child is None:
            return
        self._cancel_child_resources(child)
        with contextlib.suppress(Exception):
            child.save_snapshot()

    @staticmethod
    def _cancel_child_resources(child: Session | None) -> None:
        """主动终止 child 自有的后台资源；调用多次保持幂等。"""

        if child is None:
            return
        for job in list(child.jobs.values()):
            with contextlib.suppress(Exception):
                job.kill()
        if child.mcp is not None:
            with contextlib.suppress(Exception):
                child.mcp.close()

    @staticmethod
    def _request_child_resource_cancel(child: Session, task_id: str) -> None:
        """并行发起每项资源清理，单个阻塞的 close/kill 不能挡住其他资源。"""

        for index, job in enumerate(list(child.jobs.values())):
            threading.Thread(target=lambda job=job: SubagentRuntime._kill_job(job), name=f"{task_id}-job-{index}", daemon=True).start()
        if child.mcp is not None:
            threading.Thread(target=lambda: SubagentRuntime._close_mcp(child), name=f"{task_id}-mcp", daemon=True).start()

    @staticmethod
    def _kill_job(job) -> None:
        with contextlib.suppress(Exception):
            job.kill()

    @staticmethod
    def _close_mcp(child: Session) -> None:
        if child.mcp is not None:
            with contextlib.suppress(Exception):
                child.mcp.close()

    def _finish(self, task: _TaskRecord, status: str, reason: str) -> None:
        if task.status in AgentTaskView.TERMINAL:
            return
        if reason in {"restart", "shutdown-timeout"}:
            self._retain_interrupted_workspace(task, reason)
        task.status = status
        task.stop_reason = reason
        task.finished_at = local_timestamp()
        if task.started_monotonic:
            task.elapsed_ms = max(task.elapsed_ms, int((time.monotonic() - task.started_monotonic) * 1000))
        if task.run_in_background:
            task.delivery_state = "pending"
        self._persist(task, "terminal")  # 终态先落盘，通知方随后才能观察到。

    def _collect_metrics(self, task: _TaskRecord, child: Session) -> None:
        task.steps = child.state.turn_step
        task.tool_calls = child.executed_tool_calls
        task.usage = asdict(child.usage)
        if task.isolation == "shared":
            task.changed_files = list(dict.fromkeys(diff.path for diff in child.turn_diffs if diff.path))

    def _needs_workspace_writer(self, task: _TaskRecord) -> bool:
        assert self.root.tool_catalog is not None
        child_safe = tuple(tool.NAME for tool in self.root.tool_catalog if tool.CHILD_SAFE)
        allowed = set(task.profile.tool_names(child_safe, self.HARD_DENY))
        return any(tool.NAME in allowed and tool.WORKSPACE_MUTATES for tool in self.root.tool_catalog)

    def _finalize_workspace(self, task: _TaskRecord) -> None:
        if task.isolation != "worktree" or not task.workspace:
            return
        if task.stop_reason == "shutdown-timeout":
            with self._lock:
                self._retain_interrupted_workspace(task, "shutdown-timeout")
                if task.status in AgentTaskView.TERMINAL:
                    # shutdown 可能在 git worktree add 返回前先写下终态；延迟得到的路径也必须补写。
                    self._persist(task, "workspace_retained_after_terminal")
            return
        workspace, changed_files, warnings = self.worktrees.finalize(task.workspace)
        with self._lock:
            task.workspace = workspace
            task.changed_files = changed_files
            task.warnings = list(dict.fromkeys([*task.warnings, *warnings]))
            if task.status in AgentTaskView.TERMINAL:
                # close 可能在 Git 检查期间先写下终态；后到的收口结果必须再次落盘，
                # 否则磁盘会指向已经被清理掉的 retained 路径。
                self._persist(task, "workspace_finalized_after_terminal")

    def _recover_interrupted_tasks(self) -> None:
        with self._lock:
            for task in self._tasks.values():
                if task.status in self.ACTIVE:
                    self._retain_interrupted_workspace(task, "restart")
                    task.status = "interrupted"
                    task.stop_reason = "restart"
                    task.finished_at = local_timestamp()
                    task.delivery_state = "pending" if task.run_in_background else "none"
                    if task.pending_interaction:
                        task.pending_interaction = {
                            **task.pending_interaction,
                            "status": "invalid",
                            "invalid_reason": "restart",
                            "invalidated_at": local_timestamp(),
                        }
                    self._persist(task, "interrupted")
                elif task.delivery_state == "delivered":
                    task.delivery_state = "pending"
                    self._persist(task, "notification_recovered")

    @staticmethod
    def _retain_interrupted_workspace(task: _TaskRecord, reason: str) -> None:
        if task.isolation != "worktree" or not task.workspace.get("path"):
            return
        task.workspace = {**task.workspace, "cleanup_state": "retained"}
        warning = "shutdown 超时，worktree 已保留供恢复" if reason == "shutdown-timeout" else "进程重启中断执行，worktree 已保留供恢复"
        task.warnings = list(dict.fromkeys([*task.warnings, warning]))

    def _require(self, task_id: str) -> _TaskRecord:
        task = self._tasks.get(task_id)
        if task is None:
            raise ToolError(f"未知子 Agent 任务: {task_id}")
        return task

    def _model(self, value: str, provider: ProviderConfig | None = None) -> str:
        provider = provider or self.root.config.provider
        model = value.strip() if value and value != "inherit" else provider.model
        if not model:
            raise ToolError("当前 provider 未配置模型")
        available = {provider.model, *provider.available_models}
        if available and model not in available:
            raise ToolError(f"当前 provider 不提供模型: {model}")
        return model

    @staticmethod
    def _choice(field: str, value: str, choices: set[str]) -> str:
        value = value.strip().lower()
        if value not in choices:
            raise ToolError(f"{field} 必须是: " + ", ".join(sorted(choices)))
        return value

    def _timeout(self, task: _TaskRecord) -> None:
        with self._lock:
            if task.status not in self.ACTIVE:
                return
            task.timeout_triggered = True
            task.cancel_event.set()
            agent = task.agent
            child = task.child
            self._event(task, "timeout", "任务达到 profile timeout_seconds")
        if agent is not None:
            threading.Thread(target=agent.cancel, name=f"{task.task_id}-timeout-cancel", daemon=True).start()
        if child is not None:
            self._request_child_resource_cancel(child, task.task_id + "-timeout")

    def _persist(self, task: _TaskRecord, kind: str) -> None:
        """锁内推进 revision、落盘并唤醒观察方。"""

        task.revision += 1
        self.store.save(task)
        self._event(task, kind)
        self._changed.notify_all()

    def _event(self, task: _TaskRecord, kind: str, text: str = "") -> None:
        event = SubagentEvent(kind, task.task_id, task.attempt, task.revision, task.status, text)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self.events.get_nowait()
            with contextlib.suppress(queue.Full):
                self.events.put_nowait(event)

    @staticmethod
    def _complete_messages(messages: list[Json]) -> list[Json]:
        """移除悬空调用、孤立结果和不完整批次，保留其后的独立合法消息。"""

        safe: list[Json] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if isinstance(calls, list) and calls:
                call_ids = [str(call.get("id") or "") for call in calls if isinstance(call, dict)]
                results = messages[index + 1 : index + 1 + len(call_ids)]
                result_ids = [str(result.get("tool_call_id") or "") for result in results]
                complete = (
                    len(call_ids) == len(calls)
                    and all(call_ids)
                    and len(set(call_ids)) == len(call_ids)
                    and len(result_ids) == len(call_ids)
                    and all(result.get("role") == "tool" for result in results)
                    and set(result_ids) == set(call_ids)
                )
                if complete:
                    safe.extend([message, *results])
                    index += 1 + len(results)
                    continue
                index += 1
                while index < len(messages) and messages[index].get("role") == "tool":
                    index += 1  # 不完整批次已有的部分结果也一并丢弃。
                continue
            if message.get("role") == "tool":
                index += 1  # 没有所属调用的 tool result 一律丢弃。
                continue
            safe.append(message)
            index += 1
        return safe

    @staticmethod
    def _skill_library(task: _TaskRecord) -> SkillLibrary:
        """从 launch 时持久化的定义重建独立 SkillLibrary，禁止后续 reload 渗入。"""

        skills: dict[str, Skill] = {}
        for value in task.skills_snapshot:
            try:
                skill = Skill(
                    name=str(value["name"]),
                    description=str(value.get("description") or ""),
                    body=str(value.get("body") or ""),
                    dir=str(value.get("dir") or ""),
                    source=str(value.get("source") or ""),
                )
            except (KeyError, TypeError):
                continue
            skills[skill.name] = skill
        return SkillLibrary(skills)  # 空目录也是快照；不能让 Session 自动重载后来新增的 skills。

    def _system_prompt(self, task: _TaskRecord, skills: SkillLibrary | None) -> str:
        blocks = [
            "You are a yucode sub-agent. Work only on the delegated objective, do not attempt to create other agents, and return concise evidence.",
            task.profile.prompt,
            self._execution_context_prompt(task),
        ]
        if skills is not None:
            for name in task.profile.skills:
                if skill := skills.get(name):
                    blocks.append(f"[{skill.name}] {skill.description}\n{skills.expand(skill)}")
        return "\n\n".join(block for block in blocks if block).strip()

    def _execution_context_prompt(self, task: _TaskRecord) -> str:
        """把 fork/worktree 的现有运行事实解释给 child，避免沿用父路径和过期内容。"""

        rows: list[str] = []
        if task.context == "fork":
            rows.extend(
                [
                    "--- Fork context ---",
                    "The conversation above is a frozen snapshot taken when the task was launched. It is detached from the parent and will not receive later parent changes.",
                    "Treat inherited conclusions and file contents as potentially stale: re-read current files before relying on or editing them, and follow the delegated prompt as the active scope.",
                ]
            )
        if task.isolation != "worktree":
            return "\n".join(rows)

        workspace = task.workspace
        parent = json.dumps(os.path.abspath(self.root.cwd), ensure_ascii=False)
        child = json.dumps(str(workspace.get("path") or ""), ensure_ascii=False)
        branch = json.dumps(str(workspace.get("branch") or ""), ensure_ascii=False)
        base_ref = json.dumps(str(workspace.get("base_ref") or ""), ensure_ascii=False)
        base_commit = json.dumps(str(workspace.get("base_commit") or ""), ensure_ascii=False)
        dirty = "The parent checkout was dirty when this worktree was created; its tracked and untracked changes are absent here."
        if not workspace.get("parent_was_dirty"):
            dirty = "Parent dirty content is excluded by design; no dirty parent content was detected when this worktree was created."
        rows.extend(
            [
                "--- Worktree context ---",
                f"parent_checkout: {parent}",
                f"child_worktree: {child}",
                f"branch: {branch}",
                f"base_ref: {base_ref}",
                f"base_commit: {base_commit}",
                "You are operating in the child worktree, which is a separate working copy of the same repository.",
                "Absolute paths in the delegated prompt or inherited conversation may point at the parent checkout. Translate them to the child worktree root and never edit the parent path.",
                dirty,
                "Re-read files in the child worktree before editing. Changes made here stay isolated and are not automatically copied, merged, or committed into the parent checkout.",
            ]
        )
        return "\n".join(rows)
