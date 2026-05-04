"""Shell 工具:前台命令与后台任务。"""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar, cast

from yucode.base import Json, ToolArgs, ToolError
from yucode.session import BackgroundJob, Session
from yucode.tools.base import Tool


class BashTool(Tool):
    """在工作区运行一次 bash 调用,并流式输出结果。

    默认需要确认,只读白名单是窄例外的存在——它之所以存在,是因为本工具取代了专门的
    列出与搜索工具,若每次 `ls` 都要确认将无法使用。自动批准必须覆盖整条命令而非首词:
    管道或 `&&` 链的每一段都必须独立地只读,重定向到真实路径或命令替换会使其失去资格,
    能隐藏执行的包装命令永远不会被批准。这是提示启发式,而非沙箱;确认仍是真正的边界。

    进程拥有自己的会话,因此取消会杀死整个进程组,而不是让已退出 shell 的子进程变成孤儿。
    输出按流增量解码,跨读取被拆开的多字节字符也能存活。若它在前台等待超时后仍在运行,
    同一进程会注册为后台任务,其有界输出尾部可通过 `Job` 获取。
    """

    NAME = "Bash"
    _DEV_NULL_REDIRECT_RE: ClassVar[re.Pattern] = re.compile(r"(?:\d*>>?|&>|<)\s*/dev/null(?![\w./])")
    _BACKGROUND_AMP_RE: ClassVar[re.Pattern] = re.compile(r"(?<!&)&(?!&)")
    _CONTROL_OPERATOR_RE: ClassVar[re.Pattern] = re.compile(r"&&|\|\||[|;\n]")
    LOG_LEXER = "bash"
    DESCRIPTION = (
        "Run one bash shell invocation starting in the workspace; returns exit_code/stdout/stderr and shows live output. Compose several steps into one "
        "invocation with `&&`, `||`, `|`, and `;` rather than issuing separate calls. Avoid unbounded output; "
        "limit noisy commands with head/tail/sed/rg filters or command-specific limits, and inspect large outputs in chunks. "
        "Never use it to read or print secrets (private keys, credentials, tokens, `.env`)."
    )
    MUTATES = True
    live_output: Callable[[str, str], None] | None = None

    def __init__(self, session: Session, args: ToolArgs):
        super().__init__(session, args)
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def cancel(self) -> None:
        with self._process_lock:
            proc = self._process
        if proc is not None and proc.poll() is None:
            self.kill_process_group(proc)

    # 仅检查文件系统/仓库的只读可执行程序。只由它们(以及安全的 git 子命令)构成的命令
    # 在非 yolo 模式下无需确认即可自动运行,取代了为 Bash 而移除的专用
    # List/Find/LineCount/只读-Git 工具。
    # fmt: off
    SAFE_COMMANDS: ClassVar[frozenset[str]] = frozenset(
        {
            # 常见只读检查命令。明显的写文件形式(`sort -o`、`uniq IN OUT`、`sed -i`、
            # `tree -o`)在下方被拦下;我们不追求 exotic 路径如 sed 的 `w` 命令——
            # 常识优先于穷举式安全。
            "ls", "cat", "head", "tail", "wc", "find", "grep", "egrep", "fgrep", "rg", "sort", "uniq",
            "sed", "tree", "cut", "tr", "nl", "comm", "column", "fold", "paste", "join", "echo", "printf", "pwd",
            "stat", "file", "basename", "dirname", "realpath", "readlink", "which", "type",
            "diff", "cmp", "date", "printenv", "du", "df", "jq", "true", "test", "uname", "hostname",
            # 模型经常前置的无害内建命令(cd 只改变子 shell 的目录)。
            "cd",
        }
    )
    SAFE_GIT_SUBCOMMANDS: ClassVar[frozenset[str]] = frozenset(
        {"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "blame", "describe",
         "shortlog", "cat-file", "ls-tree", "rev-list", "for-each-ref", "diff-tree"}
    )
    # fmt: on

    def needs_confirmation(self) -> bool:
        try:
            return not self.is_readonly(self.command())
        except ToolError:
            return True

    @classmethod
    def is_readonly(cls, command: str) -> bool:
        """保守地把命令归类为可自动运行。强烈偏向 False:误判"安全"会在未同意时运行
        变更性命令,而误判"不安全"只多一次确认提示。拒绝任何能写入、执行任意代码
        或转入后台的命令。"""
        command = command.strip()
        if not command:
            return False
        # 规范化掉普遍存在的无害重定向——丢弃输出到 /dev/null、合并 stderr/stdout——
        # 使常见的 `cmd 2>/dev/null` / `cmd >/dev/null 2>&1` 形式不被当作文件写入。
        scan = cls._DEV_NULL_REDIRECT_RE.sub(" ", command)
        scan = scan.replace("2>&1", " ").replace(">&2", " ")
        # 任何仍重定向到/自真实路径、或进行命令替换的内容都可能写入或执行任意代码。
        if any(ch in scan for ch in (">", "<", "`")) or "$(" in scan:
            return False
        # 拒绝孤立的后台 &(它会脱离进程);&& 与 || 是允许的序列运算符。
        if cls._BACKGROUND_AMP_RE.search(scan):
            return False
        # 按每个控制运算符(&& || | ; 换行)切分,并要求每一段都是安全的只读命令——
        # 这样 `git log && rm x` 不会因为 `git log` 而整体自动批准。
        return all(cls._safe_segment(part) for part in cls._CONTROL_OPERATOR_RE.split(scan) if part.strip())

    @classmethod
    def _safe_segment(cls, segment: str) -> bool:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            return False
        cmd = tokens[0]
        # 环境变量赋值与包装命令可能隐藏任意执行——绝不自动批准。
        # fmt: off
        if "=" in cmd or cmd in {"env", "sudo", "eval", "exec", "command", "xargs", "nohup", "time",
                                 "watch", "bash", "sh", "zsh", "tee", "awk", "python", "python3"}:
            return False
        # fmt: on
        if cmd == "git":
            return cls._safe_git(tokens)
        if cmd not in cls.SAFE_COMMANDS:
            return False
        # 能把只读命令变成写入者的标志/参数。
        if cmd == "find" and any(t in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"} for t in tokens):
            return False
        if cmd == "sed" and any(t.startswith(("-i", "--in-place")) for t in tokens):
            return False
        if cmd == "tree" and any(t.startswith(("-o", "--output")) for t in tokens):
            return False  # `tree -o FILE` 会把清单写入文件
        if cmd == "sort" and any(t.startswith(("-o", "--output")) for t in tokens):
            return False  # `sort -o FILE` / `--output=FILE` 会写入文件
        # `uniq INPUT OUTPUT` 会写入第二个文件操作数。
        return not (cmd == "uniq" and cls._uniq_writes(tokens))

    @staticmethod
    def _uniq_writes(tokens: list[str]) -> bool:
        # uniq 只在双操作数形式 `uniq [OPTS] INPUT OUTPUT` 下写入。统计位置操作数,
        # 跳过跟在取值短标志后的数值参数。
        value_flags = {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}
        operands = 0
        skip_next = False
        for token in tokens[1:]:
            if skip_next:
                skip_next = False
            elif token in value_flags:
                skip_next = True
            elif not token.startswith("-"):
                operands += 1
        return operands >= 2

    @classmethod
    def _safe_git(cls, tokens: list[str]) -> bool:
        index = 1
        while index < len(tokens) and tokens[index] == "--no-pager":
            index += 1
        if index >= len(tokens):
            return False
        sub = tokens[index]
        if sub not in cls.SAFE_GIT_SUBCOMMANDS:
            return False
        args = tokens[index + 1 :]
        if any(t == "--output" or t.startswith("--output=") for t in args):
            return False
        return not (sub == "grep" and any(t.startswith(("-O", "--open-files-in-pager")) for t in args))

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        return cls.object_schema({
            "command": {"type": "string", "minLength": 1, "pattern": "^.*\\S.*$", "description": "Bash command to run starting in the workspace; filter noisy output with head/tail/rg"},
        }, ["command"])
        # fmt: on

    @classmethod
    def payload_args(cls, payload: Json) -> ToolArgs:
        command = str(payload.get("command") or "")
        if not command.strip():
            raise ToolError("Bash command must be non-empty")
        return [command]

    def command(self) -> str:
        command = self.strings(min_count=1, max_count=1)[0]
        if not command.strip():
            raise ToolError("Bash command must be non-empty")
        return command

    def short_args(self) -> list[str]:
        return [self.command()]

    def call(self) -> str:
        command = self.command()
        bash = shutil.which("bash") or "bash"
        proc = None
        try:
            proc = subprocess.Popen(
                [bash, "-lc", command], cwd=self.session.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
            )
            with self._process_lock:
                self._process = proc
            assert proc.stdout is not None and proc.stderr is not None
            return self.stream_process(proc)
        except KeyboardInterrupt:
            self.kill_and_collect(proc)
            raise
        finally:
            with self._process_lock:
                if self._process is proc:
                    self._process = None
            if self.live_output is not None:
                self.live_output("", "")

    def stream_process(self, proc: subprocess.Popen[bytes]) -> str:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        # 每路流使用增量解码器,跨两次 4096 字节读取拆开的多字节 UTF-8 字符
        # 在完整后立即解码,而不是被破坏成替换字符。
        self._decoders = {"stdout": codecs.getincrementaldecoder("utf-8")("replace"), "stderr": codecs.getincrementaldecoder("utf-8")("replace")}
        selector = selectors.DefaultSelector()
        stdout, stderr = proc.stdout, proc.stderr
        assert stdout is not None and stderr is not None
        selector.register(stdout, selectors.EVENT_READ, "stdout")
        selector.register(stderr, selectors.EVENT_READ, "stderr")
        timed_out = False
        started = time.monotonic()
        shell_deadline = started + self.session.settings.shell_timeout
        wait_budget = self.session.settings.bash_wait_timeout
        # 自动提升:若命令在 bash_wait_timeout 内未退出,把仍在运行的进程交给后台任务注册表,
        # 并带着部分输出载荷把控制权还给模型。该设置置 0 或等待预算已 >= shell_timeout 时
        # 禁用(此时反正也会在同一截止时间杀掉)。
        promote_deadline = started + wait_budget if wait_budget and wait_budget < self.session.settings.shell_timeout else None
        try:
            while selector.get_map() or proc.poll() is None:
                now = time.monotonic()
                if promote_deadline is not None and now >= promote_deadline and proc.poll() is None:
                    # 这里不要排空:drain_selector 执行的是阻塞式 os.read,
                    # 会一直等到 bash 产生更多输出(或退出)——那将违背提升的初衷。
                    # 流式循环已读到的数据就是部分载荷;仍在途的数据成为排空线程的首次读取。
                    return self.promote_to_job(proc, selector, stdout_parts, stderr_parts)
                remaining = shell_deadline - now
                if remaining <= 0:
                    timed_out = True
                    self.kill_process_group(proc)
                    proc.wait()
                    self.drain_selector(selector, stdout_parts, stderr_parts)
                    break
                wait = min(0.2, remaining, promote_deadline - now if promote_deadline is not None else remaining)
                if selector.get_map():
                    for key, _ in selector.select(max(0.0, wait)):
                        self.read_stream_chunk(selector, key, stdout_parts, stderr_parts)
                else:
                    time.sleep(max(0.0, wait))
            if proc.returncode is None:
                proc.wait()
        finally:
            selector.close()
        stdout, stderr = "".join(stdout_parts), "".join(stderr_parts)
        if timed_out:
            stderr += ("\n" if stderr else "") + "timeout"
            return self.process_result("BashToolResult", -1, stdout, stderr)
        return self.process_result("BashToolResult", proc.returncode or 0, stdout, stderr)

    def promote_to_job(
        self,
        proc: subprocess.Popen[bytes],
        selector: selectors.BaseSelector,
        stdout_parts: list[str],
        stderr_parts: list[str],
    ) -> str:
        """把仍在运行的 Bash 进程移交给后台任务注册表。关闭流式 selector,
        启动一个排空线程持续把 proc.stdout/stderr 读入有界的内存尾部缓冲,
        并为模型返回部分输出载荷。"""
        # 在关闭 selector 前取走管道句柄,让排空线程能继续读取。
        stdout_pipe, stderr_pipe = proc.stdout, proc.stderr
        with contextlib.suppress(OSError):
            selector.close()
        self.session.job_counter += 1
        job_id = f"job.{self.session.job_counter}"
        buffer: list[str] = []
        buffer_lock = threading.Lock()
        job = BackgroundJob(
            id=job_id,
            command=self.command(),
            process=proc,
            log_path="",
            started_at=time.monotonic() - self.session.settings.bash_wait_timeout,
            stream_buffer=buffer,
            stream_lock=buffer_lock,
        )
        self.session.jobs[job_id] = job

        def drain_pipe(pipe: Any) -> None:
            if pipe is None:
                return
            try:
                # read1 立即返回当前可用的数据(行缓冲的生产者每次调用送出一行),
                # 因此缓慢滴出的输出能及时进入尾部缓冲,而不用阻塞到攒满 4KB。
                for chunk in iter(lambda: pipe.read1(4096), b""):
                    text = chunk.decode("utf-8", errors="replace")
                    with buffer_lock:
                        buffer.append(text)
                        # 超过上限后从前端裁剪,保持尾部完整。
                        total = sum(len(part) for part in buffer)
                        while total > BackgroundJob.BUFFER_LIMIT and len(buffer) > 1:
                            total -= len(buffer.pop(0))
            except (OSError, ValueError):
                return

        threading.Thread(target=drain_pipe, args=(stdout_pipe,), daemon=True).start()
        threading.Thread(target=drain_pipe, args=(stderr_pipe,), daemon=True).start()
        partial_stdout = "".join(stdout_parts)
        partial_stderr = "".join(stderr_parts)
        note = (
            f'backgrounded after {self.session.settings.bash_wait_timeout}s; still running as {job_id}. Use Job(action="wait"|"status"|"kill", job="{job_id}").'
        )
        partial_stderr = partial_stderr + ("\n" if partial_stderr else "") + note
        return self.process_result("BashToolResult", -1, partial_stdout, partial_stderr)

    def drain_selector(self, selector: selectors.BaseSelector, stdout_parts: list[str], stderr_parts: list[str]) -> None:
        for key in list(selector.get_map().values()):
            while self.read_stream_chunk(selector, key, stdout_parts, stderr_parts):
                pass

    def read_stream_chunk(
        self,
        selector: selectors.BaseSelector,
        key: selectors.SelectorKey,
        stdout_parts: list[str],
        stderr_parts: list[str],
    ) -> bool:
        try:
            data = os.read(cast(Any, key.fileobj).fileno(), 4096)
        except OSError:
            data = b""
        eof = not data
        if eof:
            with contextlib.suppress(Exception):
                selector.unregister(key.fileobj)
            with contextlib.suppress(Exception):
                cast(Any, key.fileobj).close()
        # EOF 时 final=True 会冲刷解码器中仍缓冲的字节(例如被截断的尾字符),
        # 使它们不会被静默丢弃。
        text = self._decoders[key.data].decode(data, final=eof)
        if text:
            (stdout_parts if key.data == "stdout" else stderr_parts).append(text)
            if self.live_output is not None:
                self.live_output(str(key.data), text)
        return not eof

    @staticmethod
    def kill_process_group(proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(OSError):
                proc.kill()

    @classmethod
    def kill_and_collect(cls, proc: subprocess.Popen[bytes] | None) -> tuple[str, str]:
        if proc is None:
            return "", ""
        cls.kill_process_group(proc)
        stdout, stderr = proc.communicate()

        def decode(value: bytes | str | None) -> str:
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""

        return decode(stdout), decode(stderr)


class JobTool(Tool):
    NAME = "Job"
    DESCRIPTION = "Start, monitor, wait for, list, and kill background shell jobs. Processes run in their own process group and do not block the agent."
    MUTATES = True
    ACTIONS: ClassVar[tuple[str, ...]] = ("start", "status", "wait", "list", "kill")
    MAX_JOBS: ClassVar[int] = 8
    DEFAULT_LIMIT: ClassVar[int] = 4096

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        return cls.object_schema({
            "action": {"type": "string", "enum": list(cls.ACTIONS), "description": "Operation to perform"},
            "command": {"type": "string", "minLength": 1, "description": "Shell command to run for action=start"},
            "job": {"type": "string", "description": "Job id for action=status, wait, or kill"},
            "timeout": {"type": "integer", "minimum": 0, "description": "Seconds to wait for action=wait (0 means block until the process exits)"},
            "limit": {"type": "integer", "minimum": 1, "description": "Max characters of stdout/stderr to return; default 4096"},
        }, ["action"])
        # fmt: on

    def payload(self) -> Json:
        return self.single_dict_arg("Job requires a single object argument")

    def resolved_action(self, payload: Json) -> str:
        action = str(payload.get("action") or "").strip()
        if action not in self.ACTIONS:
            raise ToolError(f"unknown action: {action!r}")
        return action

    def needs_confirmation(self) -> bool:
        return self.resolved_action(self.payload()) in {"start", "kill", "wait"}

    def short_args(self) -> list[str]:
        payload = self.payload()
        action = self.resolved_action(payload)
        if action == "start":
            return [str(payload.get("command") or "")]
        if action == "list":
            return ["list"]
        return [action, str(payload.get("job") or "")]

    @classmethod
    def log_lexer(cls, args: ToolArgs) -> str:
        payload = args[0] if len(args) == 1 and isinstance(args[0], dict) else {}
        return "bash" if payload.get("action") == "start" else cls.LOG_LEXER

    def call(self) -> str:
        payload = self.payload()
        action = self.resolved_action(payload)
        if action == "start":
            return self._start(payload)
        if action == "status":
            return self._status(payload)
        if action == "wait":
            return self._wait(payload)
        if action == "list":
            return self._list()
        if action == "kill":
            return self._kill(payload)
        raise ToolError(f"unhandled action: {action!r}")

    def _start(self, payload: Json) -> str:
        command = str(payload.get("command") or "").strip()
        if not command:
            raise ToolError("start requires a non-empty command")
        active = len(self.session.running_jobs())
        if active >= self.MAX_JOBS:
            raise ToolError(f"too many active jobs ({active}/{self.MAX_JOBS}); kill or wait for one first")
        self.session.job_counter += 1
        job_id = f"job.{self.session.job_counter}"
        # 记录到磁盘(stdout+stderr 合并),无需线程排空器就能避免子进程的 OS 级管道缓冲填满。
        # 命令用 `{ ...; }` 分组包裹,使重定向捕获复合命令的每一段而非仅最后一段
        # (否则 `a; b && c` 的早期段会泄漏到继承的 stdout)。
        # `start_new_session` 使该 shell 成为自己的进程组组长,命令继承该组,
        # 因此 killpg(pid) 能覆盖命令及其子进程;直接运行(不 `exec`)让 `cd` 等内建命令保持可用。
        fd, log_path = tempfile.mkstemp(prefix=f"nc-{job_id}-", suffix=".log")
        os.close(fd)
        proc = subprocess.Popen(
            ["bash", "-lc", f"{{ {command}; }} > {shlex.quote(log_path)} 2>&1"],
            cwd=self.session.cwd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.session.jobs[job_id] = BackgroundJob(id=job_id, command=command, process=proc, log_path=log_path, started_at=time.monotonic())
        return f"Started {job_id}: {command}"

    def _status(self, payload: Json) -> str:
        job = self._resolve_job(payload)
        timeout = int(payload.get("timeout") or 0)
        if timeout > 0:
            with contextlib.suppress(subprocess.TimeoutExpired):
                job.process.wait(timeout=timeout)
        job.update_status()
        return self._format(job, payload)

    def _wait(self, payload: Json) -> str:
        job = self._resolve_job(payload)
        timeout = payload.get("timeout")
        with contextlib.suppress(subprocess.TimeoutExpired):
            # timeout 省略或为 0 表示阻塞到进程退出(按 schema 定义)。
            job.process.wait(timeout=None if not timeout else max(1, int(timeout)))
        job.update_status()
        return self._format(job, payload)

    def _list(self) -> str:
        if not self.session.jobs:
            return "No jobs."
        self.session.running_jobs()
        rows = []
        for job in self.session.jobs.values():
            exit_code = job.exit_code if job.status != "running" else "-"
            rows.append(f"| {job.id} | {job.status} | {exit_code} | {job.command[:60]} |")
        return "Jobs:\n| id | status | exit | command |\n|---|---|---|---|\n" + "\n".join(rows)

    def _kill(self, payload: Json) -> str:
        job = self._resolve_job(payload)
        job.kill()
        return f"Killed {job.id} (status={job.status}, exit_code={job.exit_code})"

    def _resolve_job(self, payload: Json) -> BackgroundJob:
        job_id = str(payload.get("job") or "").strip()
        if not job_id:
            raise ToolError("job id required")
        # 允许裸数字 ID 作为规范 "job.N" 形式的简写。
        if job_id not in self.session.jobs and not job_id.startswith("job.") and job_id.isdigit():
            job_id = f"job.{job_id}"
        job = self.session.jobs.get(job_id)
        if job is None:
            raise ToolError(f"unknown job: {job_id!r}")
        job.update_status()
        return job

    def _format(self, job: BackgroundJob, payload: Json) -> str:
        limit = max(1, int(payload.get("limit") or self.DEFAULT_LIMIT))
        output = job.tail(limit)
        lines = [
            f"Job: {job.id}",
            f"Status: {job.status}",
            f"Command: {job.command}",
            f"Elapsed: {job.elapsed():.1f}s",
        ]
        if job.exit_code is not None:
            lines.append(f"Exit code: {job.exit_code}")
        if output:
            lines.extend(["--- output ---", output])
        return "\n".join(lines)
