import os
import subprocess
import threading
from pathlib import Path

import pytest
from agent_harness import call, session

from yucode.base import ToolError
from yucode.context import ContextManager
from yucode.runner import ToolRunner
from yucode.subagent import AgentSpec, SubagentRuntime


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repository(tmp_path):
    remote = tmp_path / "remote.git"
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("main\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "初始化")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "remote", "set-head", "origin", "-a")
    return root


def test_shared_writer_holds_attempt_lease_and_root_write_fails_busy(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.settings.yolo = True
    (tmp_path / "dirty.txt").write_text("父目录脏内容\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    seen = []

    def execute(child, _prompt):
        seen.append((child.cwd, (tmp_path / "dirty.txt").read_text(encoding="utf-8")))
        started.set()
        release.wait(timeout=2)
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("共享写入", "执行", run_in_background=True))
    assert started.wait(timeout=1)
    runner = ToolRunner(root, ContextManager(root), output_fn=lambda _text: None)

    blocked = runner.run([call("Edit", ["root.txt", [{"op": "create", "content": "根写入\n"}]])])

    assert "WorkspaceBusy" in blocked[0]["content"]
    assert not (tmp_path / "root.txt").exists()
    assert seen == [(str(tmp_path), "父目录脏内容\n")]
    release.set()
    runtime.wait(task.task_id, timeout_seconds=2)
    allowed = runner.run([call("Edit", ["root.txt", [{"op": "create", "content": "根写入\n"}]])])
    assert "status: failed" not in allowed[0]["content"]
    runtime.close()


def test_read_only_shared_profile_does_not_take_writer_lease(tmp_path):
    root = session(tmp_path)
    root.config.provider.model = "test-model"
    root.settings.yolo = True
    started = threading.Event()
    release = threading.Event()

    def execute(_child, _prompt):
        started.set()
        release.wait(timeout=2)
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("只读", "执行", subagent_type="explore", run_in_background=True))
    assert started.wait(timeout=1)
    result = ToolRunner(root, ContextManager(root), output_fn=lambda _text: None).run([call("Edit", ["root.txt", [{"op": "create", "content": "允许\n"}]])])

    assert "status: failed" not in result[0]["content"]
    release.set()
    runtime.wait(task.task_id, timeout_seconds=2)
    runtime.close()


def test_worktree_uses_origin_default_and_preserves_committed_changes(tmp_path):
    repository = _repository(tmp_path)
    _git(repository, "checkout", "-b", "feature")
    (repository / "tracked.txt").write_text("feature dirty\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("父未跟踪\n", encoding="utf-8")
    root = session(repository)
    root.config.data_dir = str(tmp_path / "data")
    root.config.provider.model = "test-model"
    seen = []

    def execute(child, _prompt):
        seen.append((child.cwd, (os.path.join(child.cwd, "tracked.txt"))))
        assert Path(child.cwd, "tracked.txt").read_text(encoding="utf-8") == "main\n"
        assert not os.path.exists(os.path.join(child.cwd, "untracked.txt"))
        with open(os.path.join(child.cwd, "agent.txt"), "w", encoding="utf-8") as handle:
            handle.write("子成果\n")
        _git(child.cwd, "add", "agent.txt")
        _git(child.cwd, "commit", "-m", "子提交")
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    task = runtime.spawn(AgentSpec("隔离", "执行", isolation="worktree"))

    assert task.status == "completed"
    assert task.workspace["mode"] == "worktree"
    assert task.workspace["base_ref"] == "origin/main"
    assert task.workspace["base_commit"] == _git(repository, "rev-parse", "origin/main")
    assert task.workspace["parent_dirty_excluded"] is True
    assert task.workspace["cleanup_state"] == "retained"
    assert os.path.isdir(task.workspace["path"])
    assert "agent.txt" in task.changed_files
    assert not (repository / "agent.txt").exists()
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "feature dirty\n"
    runtime.close()


def test_clean_worktree_is_removed_and_changed_worktree_needs_confirmed_clean(tmp_path):
    repository = _repository(tmp_path)
    root = session(repository)
    root.config.data_dir = str(tmp_path / "data")
    root.config.provider.model = "test-model"
    runtime = SubagentRuntime(root, executor=lambda _child, _prompt: "只读完成")

    clean = runtime.spawn(AgentSpec("干净", "执行", isolation="worktree"))

    assert clean.workspace["cleanup_state"] == "cleaned"
    assert not os.path.exists(clean.workspace["path"])

    def change(child, _prompt):
        with open(os.path.join(child.cwd, "result.txt"), "w", encoding="utf-8") as handle:
            handle.write("结果\n")
        child.messages.append({"role": "assistant", "content": "已写入"})
        return "完成"

    runtime._executor = change
    changed = runtime.spawn(AgentSpec("保留", "执行", isolation="worktree"))
    with pytest.raises(ToolError, match="确认"):
        runtime.clean_task(changed.task_id)
    cleaned = runtime.clean_task(changed.task_id, confirmed=True)

    assert cleaned.workspace["cleanup_state"] == "cleaned"
    assert not os.path.exists(changed.workspace["path"])
    runtime.close()


def test_resume_reuses_a_retained_worktree(tmp_path):
    repository = _repository(tmp_path)
    root = session(repository)
    root.config.data_dir = str(tmp_path / "data")
    root.config.provider.model = "test-model"
    paths = []

    def execute(child, prompt):
        paths.append(child.cwd)
        if prompt == "第一次":
            with open(os.path.join(child.cwd, "resume.txt"), "w", encoding="utf-8") as handle:
                handle.write("第一阶段\n")
            child.messages.append({"role": "assistant", "content": "第一阶段"})
        else:
            assert Path(child.cwd, "resume.txt").read_text(encoding="utf-8") == "第一阶段\n"
            child.messages.append({"role": "assistant", "content": "第二阶段"})
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.spawn(AgentSpec("恢复", "第一次", isolation="worktree"))
    second = runtime.resume(first.task_id, "第二次")

    assert paths == [first.workspace["path"], first.workspace["path"]]
    assert second.attempt == 2
    assert second.workspace["cleanup_state"] == "retained"
    runtime.close()


def test_worktree_tasks_write_in_parallel_without_taking_parent_lease(tmp_path):
    repository = _repository(tmp_path)
    root = session(repository)
    root.config.data_dir = str(tmp_path / "data")
    root.config.provider.model = "test-model"
    root.settings.yolo = True
    rendezvous = threading.Barrier(2)

    def execute(_child, _prompt):
        rendezvous.wait(timeout=2)
        return "完成"

    runtime = SubagentRuntime(root, executor=execute)
    first = runtime.spawn(AgentSpec("甲", "执行", isolation="worktree", run_in_background=True))
    second = runtime.spawn(AgentSpec("乙", "执行", isolation="worktree", run_in_background=True))
    parent_write = ToolRunner(root, ContextManager(root), output_fn=lambda _text: None).run(
        [call("Edit", ["parent.txt", [{"op": "create", "content": "父写入\n"}]])]
    )

    assert "status: failed" not in parent_write[0]["content"]
    assert runtime.wait(first.task_id, timeout_seconds=3).status == "completed"
    assert runtime.wait(second.task_id, timeout_seconds=3).status == "completed"
    runtime.close()


def test_failed_explicit_worktree_cleanup_keeps_recovery_metadata(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    root = session(repository)
    root.config.data_dir = str(tmp_path / "data")
    root.config.provider.model = "test-model"

    def change(child, _prompt):
        with open(os.path.join(child.cwd, "result.txt"), "w", encoding="utf-8") as handle:
            handle.write("结果\n")
        return "完成"

    runtime = SubagentRuntime(root, executor=change)
    task = runtime.spawn(AgentSpec("保留", "执行", isolation="worktree"))
    metadata = dict(task.workspace)
    monkeypatch.setattr(runtime.worktrees, "_remove", lambda *_args, **_kwargs: (False, "模拟删除失败"))

    with pytest.raises(ToolError, match="模拟删除失败"):
        runtime.clean_task(task.task_id, confirmed=True)
    failed = runtime.get(task.task_id)

    assert failed.workspace["cleanup_state"] == "cleanup_failed"
    assert failed.workspace["path"] == metadata["path"]
    assert failed.workspace["branch"] == metadata["branch"]
    assert failed.workspace["base_commit"] == metadata["base_commit"]
    runtime.close()
