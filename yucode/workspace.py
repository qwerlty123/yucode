"""子 Agent 的共享写租约与 Git worktree 生命周期。"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING

from yucode.base import Json, ToolError
from yucode.session import SessionSnapshotStore

if TYPE_CHECKING:
    from yucode.session import Session


class WorkspaceLease:
    """进程内单写者租约；同一 owner 可重入，根工具遇到占用时立即失败。"""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._owner = ""
        self._depth = 0

    @property
    def owner(self) -> str:
        with self._condition:
            return self._owner

    def acquire(self, owner: str, *, wait: bool, cancelled: threading.Event | None = None) -> None:
        with self._condition:
            while self._owner and self._owner != owner:
                if not wait:
                    raise ToolError(f"WorkspaceBusy: shared workspace writer is {self._owner}")
                if cancelled is not None and cancelled.is_set():
                    raise KeyboardInterrupt
                self._condition.wait(0.1)
            if cancelled is not None and cancelled.is_set():
                raise KeyboardInterrupt
            self._owner = owner
            self._depth += 1

    def release(self, owner: str) -> None:
        with self._condition:
            if self._owner != owner:
                return
            self._depth -= 1
            if self._depth <= 0:
                self._owner = ""
                self._depth = 0
                self._condition.notify_all()

    @contextlib.contextmanager
    def hold(self, owner: str, *, wait: bool, cancelled: threading.Event | None = None) -> Iterator[None]:
        self.acquire(owner, wait=wait, cancelled=cancelled)
        try:
            yield
        finally:
            self.release(owner)


class WorktreeManager:
    """创建、检查、保留和显式清理单个任务的 Git worktree。"""

    def __init__(self, root: Session):
        self.root = root

    def prepare(self, task_id: str, previous: Json | None = None) -> Json:
        previous = dict(previous or {})
        if previous.get("path") and previous.get("cleanup_state") in {"active", "retained", "cleanup_failed"}:
            path = str(previous["path"])
            if self._valid_worktree(path):
                warnings = list(previous.get("warnings") or ())
                if previous.get("cleanup_state") == "active":
                    warnings.append("检测到未收口的 worktree 状态，已按保留成果恢复")
                return {**previous, "mode": "worktree", "cleanup_state": "active", "warnings": list(dict.fromkeys(warnings))}
            raise ToolError(f"保留的 worktree 已不可用: {path}")

        repository = str(previous.get("repository") or self.canonical_repository())
        base_ref = str(previous.get("base_ref") or "")
        base_commit = str(previous.get("base_commit") or "")
        warnings = list(previous.get("warnings") or ())
        if not base_commit:
            base_ref, base_commit, warning = self.base(repository)
            if warning:
                warnings.append(warning)
        elif not self._commit_exists(repository, base_commit):
            raise ToolError(f"记录的 worktree base_commit 已不存在: {base_commit}")

        branch = str(previous.get("branch") or ("yucode-agent-" + task_id.removeprefix("agent-")))
        project = SessionSnapshotStore.project_slug(repository)
        path = str(previous.get("path") or self.root.data_path("worktrees", project, task_id))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and (not os.path.isdir(path) or os.listdir(path)):
            current_branch = self._git(path, "branch", "--show-current", check=False).stdout.strip() if self._valid_worktree(path) else ""
            if not previous and current_branch == branch:
                # git worktree add 已成功、task metadata 尚未落盘时进程可能退出；任务分支
                # 此时还没有执行 Agent，故当前 HEAD 就是可信的原始基准。
                base_commit = self._git(path, "rev-parse", "HEAD").stdout.strip()
                warnings.append("检测到 worktree 创建后的崩溃窗口，已从任务分支恢复")
                parent_dirty = bool(self._git(self.root.cwd, "status", "--porcelain", "--untracked-files=all", check=False).stdout.strip())
                return {
                    "mode": "worktree",
                    "path": path,
                    "branch": branch,
                    "base_ref": base_ref,
                    "base_commit": base_commit,
                    "repository": repository,
                    "cleanup_state": "active",
                    "parent_dirty_excluded": True,
                    "parent_was_dirty": parent_dirty,
                    "warnings": warnings,
                }
            raise ToolError(f"worktree 目标路径不是空目录: {path}")
        if self._branch_exists(repository, branch):
            self._git(repository, "worktree", "add", path, branch)
        else:
            self._git(repository, "worktree", "add", "-b", branch, path, base_commit)
        parent_dirty = bool(self._git(self.root.cwd, "status", "--porcelain", "--untracked-files=all", check=False).stdout.strip())
        return {
            "mode": "worktree",
            "path": path,
            "branch": branch,
            "base_ref": base_ref,
            "base_commit": base_commit,
            "repository": repository,
            "cleanup_state": "active",
            "parent_dirty_excluded": True,
            "parent_was_dirty": parent_dirty,
            "warnings": warnings,
        }

    def finalize(self, workspace: Json) -> tuple[Json, list[str], list[str]]:
        workspace = dict(workspace)
        warnings = [str(item) for item in workspace.get("warnings") or ()]
        path = str(workspace.get("path") or "")
        repository = str(workspace.get("repository") or "")
        base_commit = str(workspace.get("base_commit") or "")
        try:
            status = self._git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
            committed = self._git(path, "diff", "--name-only", "-z", f"{base_commit}..HEAD").stdout
            commit_count = int(self._git(path, "rev-list", "--count", f"{base_commit}..HEAD").stdout.strip() or "0")
            changed = self._changed_files(status, committed)
            workspace["commit_count"] = commit_count
        except (OSError, ToolError, ValueError) as error:
            workspace["cleanup_state"] = "retained"
            workspace["inspection_error"] = str(error).strip() or error.__class__.__name__
            warnings.append("worktree 状态检查失败，已保留")
            return workspace, [], warnings
        if status.strip() or commit_count:
            workspace["cleanup_state"] = "retained"
            return workspace, changed, warnings
        cleaned, error = self._remove(repository, path, str(workspace.get("branch") or ""), force=False)
        workspace["cleanup_state"] = "cleaned" if cleaned else "cleanup_failed"
        if error:
            workspace["cleanup_error"] = error
            warnings.append("worktree 自动清理失败，已保留元数据")
        return workspace, changed, warnings

    def clean(self, workspace: Json) -> Json:
        workspace = dict(workspace)
        if workspace.get("cleanup_state") == "cleaned":
            return workspace
        cleaned, error = self._remove(
            str(workspace.get("repository") or ""),
            str(workspace.get("path") or ""),
            str(workspace.get("branch") or ""),
            force=True,
        )
        workspace["cleanup_state"] = "cleaned" if cleaned else "cleanup_failed"
        if error:
            workspace["cleanup_error"] = error
        else:
            workspace.pop("cleanup_error", None)
        return workspace

    def canonical_repository(self) -> str:
        top = self._git(self.root.cwd, "rev-parse", "--show-toplevel").stdout.strip()
        listing = self._git(top, "worktree", "list", "--porcelain").stdout.splitlines()
        first = next((line.removeprefix("worktree ") for line in listing if line.startswith("worktree ")), top)
        return os.path.realpath(first)

    def base(self, repository: str) -> tuple[str, str, str]:
        remote_ref = self._origin_default(repository)
        warning = ""
        if not remote_ref and self._has_origin(repository):
            fetched = self._git(repository, "fetch", "origin", check=False)
            remote_ref = self._origin_default(repository)
            if fetched.returncode != 0:
                warning = "origin fetch 失败，worktree 基准已回退当前 HEAD"
        base_ref = remote_ref or "HEAD"
        if not remote_ref and not warning:
            warning = "没有可用的 origin 默认分支，worktree 基准已回退当前 HEAD"
        return base_ref, self._git(repository, "rev-parse", base_ref).stdout.strip(), warning

    def _origin_default(self, repository: str) -> str:
        symbolic = self._git(repository, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            return symbolic.stdout.strip()
        refs = self._git(repository, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", check=False)
        available = [line.strip() for line in refs.stdout.splitlines() if line.strip() and line.strip() != "origin/HEAD"]
        for candidate in ("origin/main", "origin/master"):
            if candidate in available:
                return candidate
        return available[0] if len(available) == 1 else ""

    def _remove(self, repository: str, path: str, branch: str, *, force: bool) -> tuple[bool, str]:
        errors: list[str] = []
        if path and os.path.exists(path):
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            result = self._git(repository, *args, path, check=False)
            if result.returncode != 0:
                errors.append(result.stderr.strip() or result.stdout.strip() or "git worktree remove 失败")
        if not errors and branch and self._branch_exists(repository, branch):
            result = self._git(repository, "branch", "-D", branch, check=False)
            if result.returncode != 0:
                errors.append(result.stderr.strip() or result.stdout.strip() or "git branch -D 失败")
        return not errors, "; ".join(errors)

    def _valid_worktree(self, path: str) -> bool:
        return os.path.isdir(path) and self._git(path, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0

    def _commit_exists(self, repository: str, commit: str) -> bool:
        return self._git(repository, "cat-file", "-e", commit + "^{commit}", check=False).returncode == 0

    def _branch_exists(self, repository: str, branch: str) -> bool:
        return self._git(repository, "show-ref", "--verify", "--quiet", "refs/heads/" + branch, check=False).returncode == 0

    def _has_origin(self, repository: str) -> bool:
        result = self._git(repository, "remote", "get-url", "origin", check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    @staticmethod
    def _changed_files(status: str, committed: str) -> list[str]:
        paths: list[str] = []
        entries = status.split("\0")
        index = 0
        while index < len(entries):
            record = entries[index]
            index += 1
            if len(record) < 4:
                continue
            code, path = record[:2], record[3:]
            if path:
                paths.append(path)
            if ("R" in code or "C" in code) and index < len(entries):
                source = entries[index]  # porcelain -z 的 rename/copy 紧跟第二个 NUL 路径。
                index += 1
                if source:
                    paths.append(source)
        paths.extend(path for path in committed.split("\0") if path)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolError(f"git {' '.join(args)} 失败: {error}") from error
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise ToolError(f"git {' '.join(args)} 失败: {message}")
        return result
