"""yucode MCP:Model Context Protocol 服务器集成。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from yucode.base import Config, Json, Text, ToolError
from yucode.session import Session

if TYPE_CHECKING:
    from fastmcp.client import Client
    from fastmcp.client.auth import OAuth
    from fastmcp.client.client import CallToolResult
    from fastmcp.client.tasks import ResourceTask, ToolTask
    from fastmcp.client.transports import ClientTransport
    from mcp.types import BlobResourceContents, Resource, TextResourceContents, Tool

_MCPResultT = TypeVar("_MCPResultT")


@dataclass
class MCPServerConfig:
    name: str
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    auth: str = ""
    bearer_token_env_var: str = ""
    env_http_headers: dict[str, str] = field(default_factory=dict)
    auto_connect: bool = False
    error: str = ""


class MCPFileTokenStore:
    DEFAULT_COLLECTION = "default_collection"
    _locks: ClassVar[dict[str, threading.Lock]] = {}
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))
        with self._locks_guard:
            self.lock = self._locks.setdefault(self.path, threading.Lock())

    def token_key(self, server_url: str, suffix: str) -> str:
        return server_url.rstrip("/") + suffix

    def has_server_tokens(self, server_url: str) -> bool:
        key = self.token_key(server_url, "/tokens")
        collection = "mcp-oauth-token"
        with self.lock:
            entry = self.load().get(collection, {}).get(key)
            return bool(entry and not self.expired(entry))

    def clear_server(self, server_url: str) -> None:
        with self.lock:
            data = self.load()
            for collection, key in (
                ("mcp-oauth-token", self.token_key(server_url, "/tokens")),
                ("mcp-oauth-client-info", self.token_key(server_url, "/client_info")),
                ("mcp-oauth-token-expiry", self.token_key(server_url, "/token_expiry")),
            ):
                data.get(collection, {}).pop(key, None)
            self.save(data)

    async def get(self, key: str, *, collection: str | None = None) -> Json | None:
        collection = collection or self.DEFAULT_COLLECTION
        with self.lock:
            data = self.load()
            entry = data.get(collection, {}).get(key)
            if entry is None:
                return None
            if self.expired(entry):
                data.get(collection, {}).pop(key, None)
                self.save(data)
                return None
            value = entry.get("value")
            return dict(value) if isinstance(value, dict) else None

    # 该方法由 MCP OAuth token 存储协议动态调用,静态调用图分析不会发现它。
    async def put(self, key: str, value: Json, *, collection: str | None = None, ttl: float | None = None) -> None:
        collection = collection or self.DEFAULT_COLLECTION
        expires_at = time.time() + float(ttl) if ttl is not None else None
        with self.lock:
            data = self.load()
            data.setdefault(collection, {})[key] = {"value": dict(value), "expires_at": expires_at}
            self.save(data)

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        collection = collection or self.DEFAULT_COLLECTION
        with self.lock:
            data = self.load()
            removed = data.get(collection, {}).pop(key, None) is not None
            if removed:
                self.save(data)
            return removed

    @staticmethod
    def expired(entry: Json) -> bool:
        expires_at = entry.get("expires_at")
        return isinstance(expires_at, int | float) and expires_at <= time.time()

    def load(self) -> dict[str, dict[str, Json]]:
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, dict[str, Json]]) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            try:
                file = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                # os.fdopen 失败时不会关闭 fd;这里手动关闭,避免描述符泄漏。
                os.close(fd)
                raise
            with file:
                json.dump(data, file, ensure_ascii=False, sort_keys=True)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        os.replace(tmp, self.path)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)


@dataclass
class MCPToolInfo:
    server: str
    name: str
    description: str
    input_schema: Json
    annotations: Json = field(default_factory=dict)


@dataclass
class MCPResourceInfo:
    server: str
    uri: str
    name: str
    description: str
    mime_type: str = ""


class MCPManager:
    """管理已配置的 MCP 服务器,并对外暴露有界(有限大小)的模型侧与用户侧视图。

    服务器是外部系统,因此这里的一切都不得成为关键路径。发现过程在后台并发执行;
    缓慢、损坏或未授权的服务器只会记录原因并从索引中退出,而不会让会话失败。
    因此连接状态是用于展示的信息,而不是要抛出的错误。

    只有目录的有界摘要会到达模型:schema 与描述按单个工具和整体双重设限,
    否则一个冗长的服务器仅凭存在就会每轮耗尽上下文预算。完整 schema 仍可通过 describe 按需获取。
    同一份规范化目录也用于生成命令列表和连接状态,命令循环无需理解 MCP schema 或失败状态。

    每个操作都打开自己的短生命周期客户端,因此连接不是持久状态。代价是每次 stdio 调用都要
    启动一个进程,这也是 DESIGN.md 中生命周期重构列上路线图的原因。发现过程及其 asyncio 循环
    运行在主线程之外,因此目录与状态都由锁保护。
    """

    RAW_OUTPUT_LIMIT: ClassVar[int] = 200_000
    DISCOVERY_TIMEOUT: ClassVar[int] = 10
    MAX_DISCOVERY_WORKERS: ClassVar[int] = 8
    DESCRIBE_DESCRIPTION_LIMIT: ClassVar[int] = 1_000
    DESCRIBE_ARGUMENT_LIMIT: ClassVar[int] = 50
    DESCRIBE_ARGUMENT_DESCRIPTION_LIMIT: ClassVar[int] = 160
    INDEX_SCHEMA_LIMIT: ClassVar[int] = 700  # 早期(缓存)工具索引中每个工具的 schema 上限
    INDEX_TOTAL_LIMIT: ClassVar[int] = 16_000  # 工具索引块的总体上限
    STATUS_MARKER: ClassVar[str] = "●"
    AUTH_STATUS_RE: ClassVar[re.Pattern] = re.compile(r"\b(?:401|403)\b")

    def __init__(self, session: Session):
        self.session = session
        self.tools: dict[str, list[MCPToolInfo]] = {}
        self.resources: dict[str, list[MCPResourceInfo]] = {}
        self._auto_read_done: set[tuple[str, str]] = set()
        self.server_errors: dict[str, str] = {}
        self.server_skips: dict[str, str] = {}
        self.lock = threading.Lock()
        self.discovery_status: str = "stale"  # 取值:stale | discovering | ready | error
        self.index_truncated: bool = False  # 由 render_tools_index 设置:仅名称都溢出上限时为真
        self._configs_cache: list[MCPServerConfig] | None = None
        self._oauth_token_store = MCPFileTokenStore(self.session.data_path("mcp-oauth", "tokens.json"))
        self._oauth_lock = threading.Lock()
        self._discovering_servers: dict[str, int] = {}
        self._discovery_failed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._closed = False

    def parse_configs(self) -> list[MCPServerConfig]:
        # 配置与选择器在整个会话内不可变,因此解析一次后复用。
        if self._configs_cache is None:
            self._configs_cache = self._parse_configs()
        return self._configs_cache

    def _parse_configs(self) -> list[MCPServerConfig]:
        mcp_config = self.session.config.mcp
        if not isinstance(mcp_config, dict):
            return []
        configs = [self._parse_config(str(name), raw) for name, raw in mcp_config.items() if isinstance(raw, dict)]
        return configs

    def _parse_config(self, name: str, raw: Json) -> MCPServerConfig:
        config = MCPServerConfig(
            name=name,
            url=Config.str(raw, "url"),
            command=Config.str(raw, "command"),
            auth=Config.str(raw, "auth").lower(),
            bearer_token_env_var=Config.str(raw, "bearer_token_env_var"),
            auto_connect=Config.bool(raw, "auto_connect", False),
        )

        def config_error(message: str) -> None:
            if not config.error:
                config.error = message

        def string_list(value: object) -> tuple[str, ...] | None:
            return tuple(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else None

        def string_map(value: object) -> dict[str, str] | None:
            return dict(value) if isinstance(value, dict) and all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()) else None

        def read_field(key: str, parse: Callable[[object], object | None], error: str) -> None:
            if (value := raw.get(key)) is None:
                return
            parsed = parse(value)
            if parsed is None:
                config_error(error)
            else:
                setattr(config, key, parsed)

        read_field("args", string_list, "args must be a string list")
        read_field("env", string_map, "env must be a string map")
        read_field("env_http_headers", string_map, "env_http_headers must be a string map")
        if bool(config.url) == bool(config.command):
            config_error("exactly one of url or command is required")
        elif config.command and (config.auth or config.bearer_token_env_var or raw.get("env_http_headers")):
            config_error("command (stdio) servers cannot use auth/bearer_token_env_var/env_http_headers")
        if config.auth not in {"", "oauth"}:
            config_error("auth must be oauth")
        if config.auth == "oauth" and config.bearer_token_env_var:
            config_error("auth=oauth conflicts with bearer_token_env_var")
        if config.auth == "oauth" and self._has_header(config.env_http_headers, "authorization"):
            config_error("auth=oauth conflicts with env_http_headers.Authorization")
        return config

    @staticmethod
    def _has_header(headers: dict[str, str], name: str) -> bool:
        return any(header.lower() == name.lower() for header in headers)

    def find_config(self, name: str) -> MCPServerConfig | None:
        return next((config for config in self.parse_configs() if config.name == name), None)

    @contextlib.contextmanager
    def _discovery(self, names: tuple[str, ...]):
        with self.lock:
            if not self._discovering_servers:
                self._discovery_failed = False
            for name in names:
                self._discovering_servers[name] = self._discovering_servers.get(name, 0) + 1
            self.discovery_status = "discovering"
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            with self.lock:
                self._discovery_failed |= failed
                for name in names:
                    remaining = self._discovering_servers.get(name, 0) - 1
                    if remaining > 0:
                        self._discovering_servers[name] = remaining
                    else:
                        self._discovering_servers.pop(name, None)
                if not self._discovering_servers:
                    self.discovery_status = "error" if self._discovery_failed else "ready"

    def discovering(self, name: str) -> bool:
        with self.lock:
            return name in self._discovering_servers

    def discovery_progress(self) -> tuple[int, int]:
        with self.lock:
            connected = self.tools.keys() | self.resources.keys()
            pending = sum(name not in connected for name in self._discovering_servers)
            automatic = sum(config.auto_connect for config in self.parse_configs())
            return len(connected), max(automatic, len(connected) + pending)

    def _forget_locked(self, name: str) -> None:
        self.tools.pop(name, None)
        self.resources.pop(name, None)
        self._auto_read_done = {entry for entry in self._auto_read_done if entry[0] != name}
        self.server_errors.pop(name, None)
        self.server_skips.pop(name, None)

    def discover_auto(self) -> None:
        configs = self.parse_configs()
        discoverable = [config for config in configs if config.auto_connect]
        names = tuple(config.name for config in discoverable)
        try:
            with self._discovery(names):
                configured = {config.name for config in configs}
                with self.lock:
                    for name in list(self.tools.keys() | self.resources.keys()):
                        if name not in configured:
                            self._forget_locked(name)
                if discoverable:
                    workers = min(self.MAX_DISCOVERY_WORKERS, len(discoverable))
                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-discover") as executor:
                        futures = [executor.submit(self._discover_one, config) for config in discoverable]
                        for future in as_completed(futures):
                            future.result()
        except Exception as error:  # noqa: BLE001 - 发现过程汇总任意 MCP 传输层产生的失败
            with self.lock:
                self.server_errors["-"] = str(error)

    def discover_server(self, name: str) -> None:
        config = self.find_config(name)
        if config is None:
            with self.lock:
                self._forget_locked(name)
                self.server_errors[name] = "server not found"
            return
        names = (name,)
        with self._discovery(names):
            self._discover_one(config)

    def disconnect_server(self, name: str) -> str:
        config = self.find_config(name)
        if config is None:
            return "MCP server not found: " + name
        if config.auth == "oauth" and config.url:
            self._oauth_token_store.clear_server(config.url)
        with self.lock:
            self._forget_locked(name)
        return "MCP server disconnected: " + name

    def connected(self, name: str) -> bool:
        return name in self.tools or name in self.resources

    def connect_server(
        self,
        name: str,
        *,
        interactive: bool = False,
        notify: Callable[[str], None] | None = None,
        _compact: bool = False,
    ) -> str:
        config = self.find_config(name)
        if config is None:
            return self._compact_line("error", name, "server not found") if _compact else "MCP server not found: " + name
        if not config.error and config.auth == "oauth":
            has_tokens = self._oauth_token_store.has_server_tokens(config.url)
            if not interactive and not has_tokens:
                message = f"authentication required; run `/mcp connect {name}` interactively"
                if _compact:
                    return self._compact_line("error", name, message)
                return f"MCP server authentication required: {name}; run /mcp connect {name} interactively"
            if interactive:
                if has_tokens:
                    self.discover_server(name)
                    if not self._oauth_reauthorization_required(name):
                        return self._connect_result(name, compact=_compact)
                with self._oauth_lock:
                    # token 与已注册的 OAuth 客户端构成一套凭据。只要其一被拒绝,
                    # 就同时丢弃两者,使新的随机回调端口能与替换后的 token 一起注册。
                    self._oauth_token_store.clear_server(config.url)
                    if error := self._authenticate_oauth(config, notify=notify):
                        if _compact:
                            prefix = f"MCP OAuth authentication failed for {name}: "
                            return self._compact_line("error", name, error.removeprefix(prefix))
                        return error
        self.discover_server(name)
        return self._connect_result(name, compact=_compact)

    def _compact_line(self, kind: str, name: str, detail: str) -> str:
        """批量连接/管理界面使用的一行服务器状态:'● kind  `name` — detail'。"""
        return f"{self.STATUS_MARKER} {kind}  `{name}` — {detail}"

    def _oauth_reauthorization_required(self, name: str) -> bool:
        issue = self.server_issue(name)
        if issue is None or issue[0] != "error":
            return False
        message = issue[1].lower()
        markers = ("authentication required", "unauthorized", "invalid token", "invalid_token", "invalid_request", "invalid client")
        return any(marker in message for marker in markers) or MCPManager.AUTH_STATUS_RE.search(message) is not None

    def _connect_result(self, name: str, *, compact: bool = False) -> str:
        if issue := self.server_issue(name):
            kind, message = issue
            if compact:
                return self._compact_line(kind, name, message)
            return f"MCP server {kind}: {name}: {message}"
        tool_count = len(self.tools.get(name, []))
        resource_count = len(self.resources.get(name, []))
        if compact:
            assets = f"{tool_count} tool" + ("" if tool_count == 1 else "s")
            if resource_count:
                assets += f", {resource_count} resource" + ("" if resource_count == 1 else "s")
            return self._compact_line("connected", name, assets)
        return f"MCP server connected: {name}; tools={tool_count}; resources={resource_count}"

    def connect_servers(
        self,
        names: list[str],
        *,
        interactive: bool = False,
        notify: Callable[[str], None] | None = None,
    ) -> str:
        """并发连接一批去重后的服务器,同时保持结果顺序。"""
        selected = list(dict.fromkeys(names))
        if len(selected) == 1:
            return self.connect_server(selected[0], interactive=interactive, notify=notify)

        results: dict[str, str] = {}
        workers = min(self.MAX_DISCOVERY_WORKERS, len(selected))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-connect") as executor:
            futures = {name: executor.submit(self.connect_server, name, interactive=interactive, notify=notify, _compact=True) for name in selected}
            for name, future in futures.items():
                results[name] = future.result()
        items = ("- " + results[name].replace("\n", "\n    ") for name in selected)
        return "MCP connection results:\n\n" + "\n".join(items)

    def _discover_one(self, config: MCPServerConfig) -> None:
        if config.error:
            self.set_server_error(config.name, config.error)
            return
        headers = self._build_mcp_headers(config)
        if isinstance(headers, str):
            if self.can_skip_auth_error(headers):
                self.set_server_skip(config.name, headers)
            else:
                self.set_server_error(config.name, headers)
            return

        if config.auth == "oauth" and not self._oauth_token_store.has_server_tokens(config.url):
            self.set_server_error(config.name, "authentication required; run /mcp connect " + config.name)
            return
        try:
            tools, resources = self.run_async(self._gather_assets(config, headers))
            with self.lock:
                self.tools[config.name] = self._tools_info(config.name, tools)
                self.resources[config.name] = self._resources_info(config.name, resources)
                self.server_errors.pop(config.name, None)
                self.server_skips.pop(config.name, None)
        except BaseException as error:
            if self.is_cancelled_error(error):
                with self.lock:
                    self.server_errors.pop(config.name, None)
                return
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            self.set_server_error(config.name, self.error_text(error, timeout=self.discovery_timeout()))

    async def _gather_assets(self, config: MCPServerConfig, headers: dict[str, str]) -> tuple[list[Tool], list[Resource]]:
        """并发获取工具与资源。工具失败会中止发现;资源则尽力而为。"""
        tools_co = self._list_tools(config, headers)
        resources_co = self._list_resources(config, headers)
        tools, resources = await asyncio.gather(tools_co, resources_co, return_exceptions=True)
        if isinstance(tools, BaseException):
            raise tools
        if isinstance(resources, BaseException):
            resources = []
        return tools, resources

    def set_server_error(self, name: str, error: str) -> None:
        with self.lock:
            self._forget_locked(name)
            self.server_errors[name] = error

    def set_server_skip(self, name: str, reason: str) -> None:
        with self.lock:
            self._forget_locked(name)
            self.server_skips[name] = reason

    @classmethod
    def is_cancelled_error(cls, error: BaseException) -> bool:
        seen: set[int] = set()

        def visit(item: BaseException) -> bool:
            identity = id(item)
            if identity in seen:
                return False
            seen.add(identity)
            if type(item).__name__ == "CancelledError":
                return True
            nested = getattr(item, "exceptions", ())
            if nested:
                return all(isinstance(child, BaseException) and visit(child) for child in nested)
            cause = item.__cause__ or item.__context__
            return isinstance(cause, BaseException) and visit(cause)

        return visit(error)

    @staticmethod
    def can_skip_auth_error(error: str) -> bool:
        return error.startswith("missing environment variable ")

    def call_timeout(self) -> int:
        return max(1, self.session.settings.shell_timeout)

    def discovery_timeout(self) -> int:
        return min(self.call_timeout(), self.DISCOVERY_TIMEOUT)

    def error_text(self, error: BaseException, *, timeout: int | None = None) -> str:
        if isinstance(error, TimeoutError):
            return f"timeout after {timeout or self.call_timeout()}s"
        text = str(error).strip()
        return text or error.__class__.__name__

    def _tools_info(self, server: str, tools: list[Tool]) -> list[MCPToolInfo]:
        return [
            MCPToolInfo(
                server=server,
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema,
                annotations=self.tool_annotations(t),
            )
            for t in tools
        ]

    def _resources_info(self, server: str, resources: list[Resource]) -> list[MCPResourceInfo]:
        infos: list[MCPResourceInfo] = []
        for r in resources or []:
            uri = str(getattr(r, "uri", "") or "")
            if not uri:
                continue
            infos.append(
                MCPResourceInfo(
                    server=server,
                    uri=uri,
                    name=str(getattr(r, "name", "") or ""),
                    description=str(getattr(r, "description", "") or ""),
                    mime_type=str(getattr(r, "mimeType", "") or ""),
                )
            )
        return infos

    @staticmethod
    def tool_annotations(tool: Tool) -> Json:
        annotations = getattr(tool, "annotations", None)
        if annotations is None:
            return {}
        if isinstance(annotations, dict):
            return annotations
        if hasattr(annotations, "model_dump"):
            data = annotations.model_dump(mode="json", exclude_none=True)
            return data if isinstance(data, dict) else {}
        return {}

    def tool_needs_confirmation(self, server: str, tool_name: str) -> bool:
        info = self.tool_info(server, tool_name)
        if info is None:
            return True
        annotations = info.annotations
        if annotations.get("readOnlyHint") is True:
            return False
        return annotations.get("destructiveHint") is not False

    def tool_info(self, server: str, tool_name: str) -> MCPToolInfo | None:
        return next((tool for tool in self.tools.get(server, []) if tool.name == tool_name), None)

    def _async_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._closed:
                raise ToolError("MCP manager is closed")
            if self._loop is not None and self._loop.is_running() and self._loop_thread is not None and self._loop_thread.is_alive():
                return self._loop
            # 之前的线程已退出或事件循环已停止;重置并重建。
            self._loop = None
            self._loop_thread = None
            ready = threading.Event()
            holder: dict[str, asyncio.AbstractEventLoop] = {}

            def run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder["loop"] = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    with contextlib.suppress(Exception):
                        loop.close()

            self._loop_thread = threading.Thread(target=run, name="mcp-async", daemon=True)
            self._loop_thread.start()
            ready.wait()
            self._loop = holder["loop"]
            return self._loop

    def run_async(self, coroutine: Coroutine[Any, Any, _MCPResultT], *, timeout: int | None = None) -> _MCPResultT:
        if timeout is None:
            timeout = self.call_timeout()
        loop = self._async_loop()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise ToolError(f"MCP call timed out after {timeout}s") from error
        except concurrent.futures.CancelledError as error:
            raise ToolError("MCP call was cancelled") from error

    def close(self) -> None:
        # 在解释器拆除默认执行器之前停止并 join 后台事件循环。否则正在进行的客户端清理
        # (HTTP 会话终止、经 run_in_executor 的 DNS 查询)会与 concurrent.futures 的
        # atexit 关停竞争,并打印 "cannot schedule new futures after shutdown"。
        with self._loop_lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._loop_thread
            self._loop = None
            self._loop_thread = None
        if loop is None or thread is None:
            return

        async def _shutdown() -> None:
            pending = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(BaseException):
                    await task

        if loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=5)
            except concurrent.futures.TimeoutError:
                pass
            except Exception:  # noqa: BLE001, S110 - 取消后的关停是尽力而为。
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001, S110 - 事件循环可能已经关闭。
                pass
        if thread.is_alive():
            thread.join(timeout=5)

    def oauth_client(self, config: MCPServerConfig, *, interactive: bool = False, notify: Callable[[str], None] | None = None) -> OAuth:
        from fastmcp.client.auth import OAuth

        class YucodeOAuth(OAuth):
            async def redirect_handler(self, authorization_url: str) -> None:
                if not interactive:
                    raise RuntimeError("authentication required; run /mcp connect " + config.name)
                if notify:
                    notify("Open this URL to authorize MCP server `" + config.name + "`:\n" + authorization_url)
                await super().redirect_handler(authorization_url)

        return YucodeOAuth(
            # FastMCP 将其类型标注为完整的 AsyncKeyValue 协议,尽管 TokenStorageAdapter
            # 只调用 get/put/delete。MCPFileTokenStore 刻意只实现被用到的这个子集。
            token_storage=self._oauth_token_store,  # pyright: ignore[reportArgumentType]
            client_name="yucode",
            callback_timeout=self.session.settings.shell_timeout,
        )

    def _transport(self, config: MCPServerConfig, headers: dict[str, str]) -> ClientTransport:
        from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

        if config.command:
            # 设置 env 时 MCP SDK 会替换(而非合并)子进程环境,
            # 因此把配置的变量叠加在继承的环境之上,以保留 PATH 等。
            env = {**os.environ, **config.env} if config.env else None
            return StdioTransport(command=config.command, args=list(config.args), env=env)
        return StreamableHttpTransport(config.url, headers=headers)

    async def _run_op(
        self,
        config: MCPServerConfig,
        headers: dict[str, str],
        operation: Callable[[Client], Awaitable[_MCPResultT]],
        *,
        long_timeout: bool = False,
        interactive: bool = False,
        notify: Callable[[str], None] | None = None,
    ) -> _MCPResultT:
        """进入一个 fastmcp Client(若 config.auth=='oauth' 则带 OAuth),等待一次操作完成。"""
        from fastmcp.client import Client

        timeout = self.call_timeout() if long_timeout or interactive else self.discovery_timeout()
        auth = self.oauth_client(config, interactive=interactive, notify=notify) if config.auth == "oauth" else None
        async with Client(self._transport(config, headers), auth=auth, timeout=timeout, init_timeout=timeout) as client:
            return await asyncio.wait_for(operation(client), timeout=timeout)

    async def _list_tools(self, config: MCPServerConfig, headers: dict[str, str]) -> list[Tool]:
        return await self._run_op(config, headers, lambda client: client.list_tools())

    async def _list_resources(self, config: MCPServerConfig, headers: dict[str, str]) -> list[Resource]:
        return await self._run_op(config, headers, lambda client: client.list_resources())

    async def _call_tool(self, config: MCPServerConfig, headers: dict[str, str], name: str, arguments: Json) -> CallToolResult | ToolTask:
        return await self._run_op(config, headers, lambda client: client.call_tool(name, arguments), long_timeout=True)

    async def _read_resource(
        self, config: MCPServerConfig, headers: dict[str, str], uri: str
    ) -> list[TextResourceContents | BlobResourceContents] | ResourceTask:
        return await self._run_op(config, headers, lambda client: client.read_resource(uri), long_timeout=True)

    def _build_mcp_headers(self, config: MCPServerConfig) -> dict[str, str] | str:
        headers: dict[str, str] = {}
        if config.bearer_token_env_var:
            token = os.environ.get(config.bearer_token_env_var)
            if not token:
                return f"missing environment variable {config.bearer_token_env_var}"
            headers["Authorization"] = f"Bearer {token}"
        if config.env_http_headers:
            for header_name, env_var in config.env_http_headers.items():
                value = os.environ.get(env_var)
                if not value:
                    return f"missing environment variable {env_var}"
                if header_name.lower() == "authorization":
                    if config.auth == "oauth":
                        return "conflicting Authorization header; use auth=oauth instead"
                    if self._has_header(headers, "authorization"):
                        return "conflicting Authorization header; use only one authorization source"
                headers[header_name] = value
        return headers

    def _resolve_server(self, server: str) -> tuple[MCPServerConfig, dict[str, str]]:
        """查找已配置的服务器并构造请求头;当服务器缺失、出错或未认证时抛出带用户可读
        信息的 ToolError。工具调用与资源调用共用。"""
        config = self.find_config(server)
        if config is None:
            raise ToolError(f"MCP server '{server}' not found")
        if config.error:
            raise ToolError(config.error)
        headers = self._build_mcp_headers(config)
        if isinstance(headers, str):
            raise ToolError(headers)
        if config.auth == "oauth" and not self._oauth_token_store.has_server_tokens(config.url):
            raise ToolError(f"MCP server '{server}' requires authentication; run /mcp connect {server}")
        self._require_available(server)
        return config, headers

    def _require_available(self, server: str) -> None:
        """当已配置的服务器处于故障状态或未连接时抛出 ToolError。"""
        if issue := self.server_issue(server):
            raise ToolError(f"MCP server '{server}' {issue[0]}: {issue[1]}")
        if not self.connected(server):
            raise ToolError(f"MCP server '{server}' is not connected; run /mcp connect {server}")

    def call_tool(self, server: str, tool_name: str, arguments: Json) -> str:
        config, headers = self._resolve_server(server)

        try:
            result = self.run_async(self._call_tool(config, headers, tool_name, arguments))
        except Exception as e:  # noqa: BLE001 - 把任意的 MCP 传输错误规范为 ToolError。
            raise ToolError("MCP call failed: " + self.error_text(e))

        text = self.normalize_result(result)
        return f"<MCPCall server={json.dumps(server)} tool={json.dumps(tool_name)}>\n{text}\n</MCPCall>"

    def list_resources(self, server: str) -> str:
        self._resolve_server(server)
        resources = self.resources.get(server, [])
        lines = [f"<MCPResources server={json.dumps(server)}>"]
        if resources:
            lines.extend(self._format_resource_line(res) for res in resources)
        else:
            lines.append("(no resources advertised by this server)")
        lines.append("</MCPResources>")
        return "\n".join(lines)

    def read_resource(self, server: str, uri: str) -> str:
        if not uri:
            raise ToolError("MCP read_resource requires a uri")
        config, headers = self._resolve_server(server)
        try:
            result = self.run_async(self._read_resource(config, headers, uri))
        except Exception as e:  # noqa: BLE001 - 把任意的 MCP 传输错误规范为 ToolError。
            raise ToolError("MCP resource read failed: " + self.error_text(e))
        text = self.normalize_resource(result)
        return f"<MCPResource server={json.dumps(server)} uri={json.dumps(uri)}>\n{text}\n</MCPResource>"

    AUTO_READ_LIMIT: ClassVar[int] = 6_000  # 首次工具调用时自动注入的每个资源文档的大小上限

    def auto_read_prefix(self, server: str, tool_name: str) -> str:
        """在首次调用描述中引用了资源文档的工具时,获取该文档一次。

        返回要附加到该次调用结果上的块(使语法提示在首次尝试时就到达模型并进入缓存历史),
        若无新内容可注入则返回 ""。尽力而为:失败会被吞掉,且同一 uri 不会重试。
        """
        info = self.tool_info(server, tool_name)
        if info is None:
            return ""
        advertised = {res.uri for res in self.resources.get(server, [])}
        blocks: list[str] = []
        for uri in self._extract_uris(info.description):
            if (server, uri) in self._auto_read_done:
                continue
            scheme = uri.split("://", 1)[0].lower()
            # 只获取能通过 MCP 真正读到的内容:已公布(advertised)的资源或自定义(非 web)scheme。
            # 普通 http(s) 链接留给模型自行显式读取。
            if uri not in advertised and scheme in ("http", "https"):
                continue
            self._auto_read_done.add((server, uri))  # 先标记再获取,失败也不会重试
            try:
                blocks.append(self.read_resource(server, uri)[: self.AUTO_READ_LIMIT])
            except Exception:  # noqa: BLE001, S112 - 被引用的资源尽力注入,失败即跳过。
                continue
        if not blocks:
            return ""
        body = "\n".join(blocks)
        return f'<MCPAutoResources note="docs referenced by {server}.{tool_name}; injected once">\n{body}\n</MCPAutoResources>\n'

    @staticmethod
    def _dump_object(item: Any) -> str:
        """渲染非 str/dict 的 MCP 条目:有 pydantic 风格 model_dump 的转成 JSON,否则用 str()。"""
        if hasattr(item, "model_dump"):
            return json.dumps(item.model_dump(mode="json"), ensure_ascii=False, indent=2)
        return str(item)

    def normalize_resource(self, result: Any) -> str:
        items = result if isinstance(result, list) else [result]
        parts: list[str] = []
        for item in items:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
                continue
            blob = getattr(item, "blob", None)
            if blob is not None:
                mime = str(getattr(item, "mimeType", "") or "application/octet-stream")
                parts.append(f"<binary mimeType={json.dumps(mime)} bytes={len(blob)}/>")
                continue
            parts.append(self._dump_object(item))
        return self._join_bounded(parts)

    def _format_resource_line(self, info: MCPResourceInfo) -> str:
        desc = " ".join((info.description or "").split())
        if len(desc) > 100:
            desc = desc[:97] + "..."
        mime = f" [{info.mime_type}]" if info.mime_type else ""
        label = f"{info.uri}{mime}"
        return f"- {label} - {desc}" if desc else f"- {label}"

    def _join_bounded(self, parts: list[str]) -> str:
        """连接非空部分,截断到 RAW_OUTPUT_LIMIT 并附加截断标记。"""
        text = "\n".join(part for part in parts if part).strip()
        if len(text) > self.RAW_OUTPUT_LIMIT:
            text = text[: self.RAW_OUTPUT_LIMIT] + f"\n<MCPOutputTruncated chars={json.dumps(len(text))}/>"
        return text

    @staticmethod
    def _schema_props_required(schema: Json) -> tuple[Json, list[Any]]:
        """提取 JSON-Schema 对象的 `properties` 字典与 `required` 列表,对类型错误保持宽容。"""
        props = schema.get("properties", {})
        required = schema.get("required", [])
        return (props if isinstance(props, dict) else {}, required if isinstance(required, list) else [])

    def normalize_result(self, result: Any) -> str:
        parts: list[str] = []
        content = getattr(result, "content", result)
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    parts.append(str(item.get("text") or ""))
                elif item_type == "resource":
                    parts.append(json.dumps(item.get("resource"), ensure_ascii=False, indent=2))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, indent=2))
                continue
            item_type = getattr(item, "type", "")
            if item_type == "text":
                parts.append(str(getattr(item, "text", "") or ""))
            elif item_type == "resource":
                parts.append(str(getattr(item, "resource", "") or ""))
            else:
                parts.append(self._dump_object(item))
        return self._join_bounded(parts)

    def _authenticate_oauth(self, config: MCPServerConfig, notify: Callable[[str], None] | None = None) -> str | None:
        """校验缓存的 OAuth 凭据,或完成交互式授权。"""
        headers = self._build_mcp_headers(config)
        if isinstance(headers, str):
            return headers
        try:
            self.run_async(self._run_op(config, headers, lambda c: c.list_tools(), interactive=True, notify=notify))
        except Exception as error:  # noqa: BLE001 - OAuth 探测会跨越第三方 MCP 传输层。
            text = self.error_text(error, timeout=self.call_timeout())
            self.set_server_error(config.name, text)
            return self.oauth_auth_failure(config, text)
        with self.lock:
            self.server_errors.pop(config.name, None)
        return None

    @staticmethod
    def oauth_auth_failure(config: MCPServerConfig, error: str) -> str:
        return "\n".join(
            [
                "MCP OAuth authentication failed for " + config.name + ": " + error,
                "No authorization URL was provided by the server.",
                "Open MCP URL: " + config.url,
            ]
        )

    def describe_tool(self, server: str, tool_name: str) -> str:
        if self.find_config(server) is None:
            raise ToolError(f"MCP server '{server}' not found")
        self._require_available(server)

        info = self.tool_info(server, tool_name)
        if info is None:
            raise ToolError(f"MCP tool '{tool_name}' not found on server '{server}'")

        return self._render_describe(server, info)

    def _render_describe(self, server: str, info: MCPToolInfo) -> str:
        from yucode.tools import Tool  # 局部导入:tools 构建在 mcp 之上

        schema = info.input_schema or {}
        lines = [f"<MCPDescribe server={json.dumps(server)} tool={json.dumps(info.name)}>"]
        if info.description:
            lines.append("<description>")
            lines.append(Tool.compact(info.description, self.DESCRIBE_DESCRIPTION_LIMIT))
            lines.append("</description>")
        lines.append("<arguments>")
        props, required = self._schema_props_required(schema)
        for index, (name, prop) in enumerate(props.items()):
            if index >= self.DESCRIBE_ARGUMENT_LIMIT:
                lines.append(f"... {len(props) - self.DESCRIBE_ARGUMENT_LIMIT} more arguments omitted")
                break
            req = "required" if name in required else "optional"
            prop = prop if isinstance(prop, dict) else {}
            typ = prop.get("type", "any")
            desc = Tool.compact(str(prop.get("description", "") or ""), self.DESCRIBE_ARGUMENT_DESCRIPTION_LIMIT)
            lines.append(f"- {name} {req} {typ}: {desc}")
        lines.append("</arguments>")
        if isinstance(schema, dict) and schema:
            lines.append("<schema>")
            lines.append(json.dumps(schema, ensure_ascii=False, indent=2))
            lines.append("</schema>")
        lines.append("</MCPDescribe>")
        return "\n".join(lines)

    def render_tools_index(self) -> str:
        """渲染注入每一轮模型对话(缓存前缀中)的 MCP 工具块。

        该块以 INDEX_TOTAL_LIMIT 为上限,不会撑大每次请求。将要溢出时,降级策略是削减
        *细节*而非*实体*:模型随时可以通过 `describe` 重新获取被丢弃的 schema,但它永远无法
        调用一个从未被告知存在的服务器或工具。因此这里依次尝试更廉价的渲染方式,输出能装下
        的最丰富的一种:

            tier 1 "schema" — 内联每个工具的完整 JSON schema(常规情况)
            tier 2 "args"   — 丢弃 schema,每个工具只保留名称 + 参数摘要
            tier 3 "names"  — 仅名称,按服务器分组
            tier 4          — 硬截断(仅在工具数量上千、16KB 物理上装不下时);
                              服务器标题排在最前,模型仍能看到大多数服务器存在。

        第 1-3 层保证每个已连接的服务器与工具名都可见。各细节层的渲染方式见 _index_body,
        相关保证见 test_mcp.TestToolIndexBudget。
        """
        activated = self.tools.keys() | self.resources.keys()
        configs = [config for config in self.parse_configs() if config.name in activated]
        if not configs:
            return ""

        intro = [
            "--- MCP TOOLS ---",
            'Use MCP(action="call", server, tool, arguments) for external MCP server tools.',
            'Use MCP(action="describe", server, tool) for the full schema when one is truncated below; the result stays in the conversation, so do not describe the same tool again once its schema is shown — just call it.',
            'Use MCP(action="read_resource", server, uri) to read a listed resource (e.g. docs describing how to build a tool\'s arguments). Read relevant resources before calling.',
            "Format: server.tool(req: type; opt: type) - description",
            "        schema: <JSON Schema for the arguments object>",
            "",
        ]

        # 备注告诉模型丢了什么(以及 describe 可以找回),避免它误以为某个工具没有参数。
        # 第 1 层("schema")无需备注;第 4 层复用下面第 3 层的文本。
        notes = {
            "args": ['Schemas omitted to fit; use MCP(action="describe", server, tool) for a tool\'s arguments.', ""],
            "names": ['Only tool names shown to fit; use MCP(action="describe", server, tool) before calling.', ""],
        }
        for detail in ("schema", "args", "names"):
            body = self._index_body(configs, detail=detail)
            text = "\n".join(intro + notes.get(detail, []) + body)
            if len(text) <= self.INDEX_TOTAL_LIMIT:
                self.index_truncated = False
                return text

        # 第 4 层:连仅名称都会溢出,因此一些工具被整体丢弃(不只是细节)。
        # 标记该状态以便 CLI 提醒用户——与第 1-3 层不同,在索引能装下之前
        # 这些工具不可调用(可减少服务器数量,或查阅 /mcp tools)。
        self.index_truncated = True
        return text[: self.INDEX_TOTAL_LIMIT - 10] + "\n... MCP tools truncated; use /mcp tools for full list."

    def _resources_block(self, server: str, resources: list[MCPResourceInfo]) -> list[str]:
        """'resources (N) — read with ...' 标题加每个资源一行,无资源时返回 []。"""
        if not resources:
            return []
        header = f'resources ({len(resources)}) — read with MCP(action="read_resource", server={json.dumps(server)}, uri=...):'
        return [header, *(self._format_resource_line(res) for res in resources)]

    def _server_lines(self, server: str, tools: list[MCPToolInfo], resources: list[MCPResourceInfo], *, include_schema: bool = True) -> list[str]:
        """服务器的标题、工具行与资源块——工具索引与提及(@)渲染共用。"""
        lines = [f"[{server}] {server.capitalize()}"]
        lines.extend(line for info in tools if (line := self._format_tool_line(server, info, include_schema=include_schema)))
        lines.extend(self._resources_block(server, resources))
        return lines

    def _index_body(self, configs: list[MCPServerConfig], *, detail: str = "schema") -> list[str]:
        """按某一细节级别渲染工具索引中每个服务器的正文行。

        detail 控制每个工具输出多少内容(从最丰富到最简略):
            "schema" — 经 _format_tool_line 的完整行,含内联 JSON schema
            "args"   — 同样的行但不带 schema(名称 + 参数摘要 + 描述)
            "names"  — 每个服务器一行 "tools: a, b, c",仅名称

        无论细节级别如何,每个已连接的服务器都会出现。
        """
        lines: list[str] = []
        pending: list[str] = []
        for config in configs:
            tools = self.tools.get(config.name, [])
            resources = self.resources.get(config.name, [])
            if not tools and not resources:
                pending.append(f"- {config.name}: {self._pending_status(config.name)}")
                continue
            if detail == "names":
                lines.append(f"[{config.name}] {config.name.capitalize()}")
                if tools:
                    lines.append("tools: " + ", ".join(tool.name for tool in tools))
                lines.extend(self._resources_block(config.name, resources))
            else:
                lines.extend(self._server_lines(config.name, tools, resources, include_schema=detail == "schema"))
            lines.append("")

        if pending:
            lines.append("Configured servers not yet available (they exist — do not assume otherwise):")
            lines.extend(pending)
            lines.append("")
        return lines

    def server_issue(self, name: str) -> tuple[str, str] | None:
        """把服务器的故障状态归类为 (kind, message);error 优先于 skip。"""
        if (error := self.server_errors.get(name)) is not None:
            return "error", error
        if (skip := self.server_skips.get(name)) is not None:
            return "skipped", skip
        return None

    def _pending_status(self, name: str) -> str:
        if issue := self.server_issue(name):
            kind, message = issue
            return message if kind == "error" else "skipped: " + message
        if self.discovering(name):
            return "discovering — tools not loaded yet; retry shortly"
        if self.connected(name):
            return "connected; no tools or resources advertised"
        return "not connected"

    MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_-]+))?")

    def resolve_mentions(self, text: str) -> str:
        configs = {config.name: config for config in self.parse_configs()}
        if not configs:
            return ""
        lower = {name.lower(): name for name in configs}
        seen: set[tuple[str, str]] = set()
        blocks: list[str] = []
        for raw_server, raw_tool in self.MENTION_PATTERN.findall(text):
            name = raw_server if raw_server in configs else lower.get(raw_server.lower())
            if name is None:  # 不是已配置的服务器——原样保留 @token 字面量
                continue
            key = (name, raw_tool)
            if key in seen:
                continue
            seen.add(key)
            blocks.append(self._mention_block(name, raw_tool))
        if not blocks:
            return ""
        header = [
            "--- MCP MENTIONS ---",
            'The user explicitly referenced these MCP servers/tools. Prefer them via MCP(action="call", ...) unless clearly irrelevant.',
            "",
        ]
        return "\n".join(header + blocks).strip()

    def _mention_block(self, server: str, tool: str) -> str:
        if not self.connected(server) and not self.discovering(server):
            self.discover_server(server)
        if issue := self.server_issue(server):
            kind, message = issue
            return f"[{server}] {'unavailable' if kind == 'error' else 'skipped'}: {message}"
        tools = self.tools.get(server, [])
        resources = self.resources.get(server, [])
        if not tools and not resources:
            return f"[{server}] {self._pending_status(server)}"
        if tool:
            info = self.tool_info(server, tool)
            if info is not None:
                return self._render_describe(server, info)
            available = ", ".join(t.name for t in tools) or "(none)"
            return f"[{server}] tool '{tool}' not found; available: {available}"
        return "\n".join(self._server_lines(server, tools, resources))

    def _format_tool_line(self, server: str, info: MCPToolInfo, *, include_schema: bool = True) -> str:
        args_str = self._tool_args_summary(info)
        desc = (info.description or "").split("\n")[0].strip()
        desc = " ".join(desc.split())
        if len(desc) > 80:
            desc = desc[:77] + "..."

        line = f"{server}.{info.name}{args_str} - {desc}"
        if len(line) > 200:
            line = line[:197] + "..."
        # 完整描述(往往点名了含参数语法的资源文档)在上方被截断,
        # 因此把其中提到的类资源 URI 显式列出来。
        uris = self._extract_uris(info.description)
        if uris:
            line += '\n  refs (read with MCP action="read_resource"): ' + ", ".join(uris)
        if include_schema:
            schema = self._schema_json(info.input_schema, self.INDEX_SCHEMA_LIMIT)
            if schema:
                line += f"\n  schema: {schema}"
        return line

    URI_PATTERN: ClassVar[re.Pattern] = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s'\"<>)\]}]+")

    @classmethod
    def _extract_uris(cls, text: str, limit: int = 5) -> list[str]:
        """从自由文本中提取类资源 URI,去重并轻量去除标点。"""
        seen: list[str] = []
        for match in cls.URI_PATTERN.findall(text or ""):
            uri = match.rstrip(".,;:")
            if uri not in seen:
                seen.append(uri)
            if len(seen) >= limit:
                break
        return seen

    @staticmethod
    def _schema_json(schema: Json, limit: int) -> str:
        """把远端工具的输入 schema 渲染为紧凑 JSON,上限 `limit` 字符(0 表示不限)。"""
        if not isinstance(schema, dict) or not schema:
            return ""
        text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        if limit and len(text) > limit:
            text = text[: limit - 1].rstrip() + "… (truncated; MCP describe for full schema)"
        return text

    def _tool_args_summary(self, info: MCPToolInfo) -> str:
        schema = info.input_schema or {}
        props, required = self._schema_props_required(schema)

        def _fmt(name: str) -> str:
            t = props.get(name, {}).get("type", "")
            return f"{name}: {t}" if t else name

        req_args = [_fmt(k) for k in required if k in props]
        opt_args = [_fmt(k) for k in props if k not in required]

        if len(req_args) > 8:
            req_args = req_args[:8] + ["..."]
        if len(opt_args) > 8:
            opt_args = opt_args[:8] + ["..."]

        parts = []
        if req_args:
            parts.append("(" + ", ".join(req_args))
        else:
            parts.append("(")
        if opt_args:
            parts.append("; " + ", ".join(opt_args))
        parts.append(")")
        return "".join(parts)

    def render_tool_listing(self, server: str | None = None) -> str:
        from yucode.tools import Tool  # 局部导入:tools 构建在 mcp 之上

        sections: list[str] = []
        configs = self.parse_configs()
        if server:
            config = self.find_config(server)
            if config is None:
                return f"MCP server not found: {server}"
            if not self.connected(server):
                return f"MCP server '{server}' is not connected; run /mcp connect {server}"
            configs = [config]
        elif not configs:
            return "(no MCP servers configured)"
        else:
            configs = [config for config in configs if self.connected(config.name)]
        for config in configs:
            lines = [f"### `{config.name}`", "", "| tool | args | description |", "| --- | --- | --- |"]
            tools = self.tools.get(config.name, [])
            if not tools:
                lines.append("| (none) |  | no tools discovered |")
            else:
                for tool in tools:
                    args_str = self._tool_args_summary(tool)
                    desc = Tool.compact((tool.description or "").split("\n")[0].strip(), 80)
                    lines.append(
                        "| `" + self.markdown_cell(tool.name) + "` | `" + self.markdown_cell(args_str) + "` | " + self.markdown_cell(desc or "-") + " |"
                    )
            resources = self.resources.get(config.name, [])
            if resources:
                lines.extend(["", "| resource | description |", "| --- | --- |"])
                for resource in resources:
                    lines.append("| `" + self.markdown_cell(resource.uri) + "` | " + self.markdown_cell(resource.description or "-") + " |")
            sections.append("\n".join(lines))
        return "\n\n".join(sections) if sections else "(no connected MCP servers)"

    def render_server_status(self) -> str:
        headers = ("server", "mode", "status", "tools", "auth")
        rows: list[tuple[str, ...]] = []
        configs = self.parse_configs()
        for config in configs:
            tools = ""
            if issue := self.server_issue(config.name):
                kind, message = issue
                status = self.STATUS_MARKER + " " + kind + ": " + message
            else:
                if self.connected(config.name):
                    status = self.STATUS_MARKER + " connected"
                    tools = str(len(self.tools.get(config.name, [])))
                else:
                    status = self.STATUS_MARKER + " disconnected"
            auth = []
            if config.auth:
                auth.append(config.auth)
            if config.bearer_token_env_var:
                auth.append("bearer_token_env_var(" + config.bearer_token_env_var + ")")
            auth.extend("env_header(" + name + ")" for name in config.env_http_headers)
            rows.append(
                (
                    "`" + self.markdown_cell(config.name) + "`",
                    "auto" if config.auto_connect else "manual",
                    self.markdown_cell(status),
                    self.markdown_cell(tools or "-"),
                    self.markdown_cell(", ".join(auth) or "-"),
                )
            )
        if not rows:
            return "(no MCP servers configured)"
        widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(len(headers))]

        def table_row(cells: tuple[str, ...]) -> str:
            return "| " + " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)) + " |"

        separators = tuple("-" * (width - 1) + (":" if index == 3 else "-") for index, width in enumerate(widths))
        lines = [table_row(headers), table_row(separators), *(table_row(row) for row in rows)]
        lines.extend(["", "Manage in the TUI with `/mcp`; fallback: `/mcp connect|disconnect NAME`. Mention `@NAME` to connect on demand."])
        return "\n".join(lines)

    @staticmethod
    def markdown_cell(text: str) -> str:
        return Text.clean(str(text)).replace("\n", " ").replace("|", "\\|")
