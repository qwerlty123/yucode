"""yucode 入口:命令行参数解析与分发。

通过 ``yucode`` 控制台脚本或 ``python -m yucode`` 调用。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import threading

from yucode.base import Config, ConfigError, ConfigFile, RuntimeSettings, UpdateStatus, YucodeError, __version__
from yucode.engine import Agent
from yucode.loop import CommandLoop
from yucode.render import Theme
from yucode.session import Session
from yucode.update import UpdateChecker


def run_update() -> int:
    """检查 GitHub 上是否有更新的 yucode,并通过检测到的包管理器升级。"""
    print(f"yucode {__version__}")
    try:
        latest = UpdateChecker.fetch_latest()
    except Exception as error:  # noqa: BLE001 - 任何网络/后端层的更新失败都统一报告。
        print(f"Error: could not check the latest version: {error}", file=sys.stderr)
        return 1
    if not UpdateStatus(latest=latest).newer_than(__version__):
        print(f"already up to date ({__version__})")
        return 0
    command = UpdateChecker.upgrade_command()
    print(f"updating {__version__} -> {latest}: {' '.join(command)}")
    try:
        return subprocess.call(command)
    except OSError as error:
        print(f"Error: could not run the upgrade command: {error}", file=sys.stderr)
        return 1


def warm_provider_sdks() -> None:
    """在主线程之外导入供应商 SDK,让提示符能立即接受输入。

    ModelClient 延迟导入它们,因为它们耗时约 0.8 秒,而这正是新提示符回显按键前的全部延迟。
    在这里后台加载可以保持提示符即时响应,又不必把这笔开销转移到首次请求上:
    用户输入第一条消息所花的时间远多于导入完成所需的时间。

    让该线程与请求路径竞争是安全的,而且是有意为之:

    - CPython 按模块加锁导入(`importlib._bootstrap._ModuleLock`),因此请求路径上落在
      预热期间的 `from openai import OpenAI` 会阻塞在该模块的锁上,然后从 `sys.modules`
      读取已完成的模块。它不可能观察到半初始化的模块,两个线程因此绑定到同一个类对象。
    - 按模块加锁只可能在两个线程同时进入导入环时死锁。`anthropic` 与 `openai` 互不导入,
      且它们共享的依赖构成 DAG,所以锁等待图没有环。若这一点将来不再成立,
      `_DeadlockError` 检测是兜底。
    - 该线程是守护线程,因为预热绝不能拖延退出。CPython 在终结阶段冻结守护线程,
      而不是让它们对着已拆除的导入系统运行,因此导入中途退出时静默无痕。

    经压力测试验证:栅栏同步的四路竞争与反复的立即退出运行均无死锁、无异常、无 stderr 噪音。
    """

    def load() -> None:
        # 预热只是优化;此处未捕获的失败会在活动提示符上打印线程回溯。
        # 任何真实问题都会在请求路径上再次浮现,那里导入相同的模块并向用户报告失败。
        with contextlib.suppress(Exception):
            import anthropic  # noqa: F401 - 导入以产生填充 sys.modules 的副作用
            import openai  # noqa: F401

    threading.Thread(target=load, name="sdk-warmup", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        print("Error: yucode does not support native Windows; use WSL instead.", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(prog="yucode")
    parser.add_argument("--config", default=None, help="Path to config TOML")
    parser.add_argument("--init-config", action="store_true", help="Create a default config file")
    parser.add_argument("--yolo", action="store_true", help="Skip confirmations for mutating tools")
    parser.add_argument(
        "--theme", choices=["auto", "light", "dark"], default="", help="Color theme (defaults to runtime.theme, then auto-detect via COLORFGBG)"
    )
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument(
        "--resume",
        default="",
        nargs="?",
        const="latest",
        help='Resume a session by UID, uid prefix, or name, or "latest"/"last" for this project\'s most recent',
    )
    resume.add_argument("-c", "--last", "--latest", dest="continue_project", action="store_true", help="Resume the latest session in the current project")
    parser.add_argument("-v", "--version", action="store_true", help="Show version")
    parser.add_argument(
        "command", nargs="?", choices=["update", "upgrade"], default=None, help="Maintenance command: update/upgrade yucode to the latest version"
    )
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.command in {"update", "upgrade"}:
        return run_update()
    try:
        if args.init_config:
            path, created = ConfigFile.init(args.config)
            print(("Created" if created else "Exists") + " config: " + path)
            return 0
        # 切换会话是结束一次运行并开始下一次,而不是把存活的对象图重指向另一个 Session:
        # 下面的一切都围绕单个 Session 构建,而此刻是唯一没有任何运行中的时刻。
        # 清理逻辑仍留在现有的 `finally` 中。
        resume = args.resume or ("latest" if args.continue_project else "")
        while True:
            if resume:
                data = ConfigFile.load(args.config)
                config = Config.from_dict(data)
                session = Session.load_snapshot(
                    resume,
                    config=config,
                    settings=RuntimeSettings.from_dict(data, yolo=args.yolo, theme=args.theme),
                    cwd=os.getcwd(),
                )
            else:
                session = Session.from_config_file(path=args.config, yolo=args.yolo, theme=args.theme)
            Theme.set_mode(Theme.resolve(session.settings.theme))
            warm_provider_sdks()
            command_loop = CommandLoop(Agent(session))
            try:
                code = command_loop.run()
            finally:
                command_loop.close_background_output()
                if session.mcp is not None:
                    session.mcp.close()
            resume = command_loop.resume_request
            if not resume:
                return code
    except ConfigError as error:
        print("ConfigError: " + str(error), file=sys.stderr)
        return 2
    except YucodeError as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
