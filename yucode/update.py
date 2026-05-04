"""yucode 更新检查:后台 PyPI 版本探测及其缓存状态。"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from urllib.request import Request, urlopen

from yucode.base import (
    HTTP_USER_AGENT,
    Text,
    UpdateStatus,
    YucodeError,
    __version__,
)
from yucode.session import Session


class UpdateChecker:
    PYPI_URL = "https://pypi.org/pypi/yucode/json"
    CACHE_FILE = "update.json"
    TIMEOUT = 5
    INTERVAL_SECONDS = 24 * 3600  # 两次探测之间的最小间隔:24 小时

    def __init__(self, session: Session):
        self.session = session
        self.cache_path = session.data_path(self.CACHE_FILE)

    def start(self) -> None:
        cached_at, cached_latest = self._load()
        self.session.update.latest = cached_latest
        if self.session.update.checking or time.time() - cached_at < self.INTERVAL_SECONDS:
            return
        self.session.update.checking = True
        threading.Thread(target=self.check, daemon=True).start()

    def check(self) -> None:
        try:
            self.session.update.latest = self.fetch_latest()
            self.session.update.error = ""
        except Exception as error:  # noqa: BLE001 - 后台更新失败不得逃出工作线程。
            self.session.update.error = Text.clean(str(error))
        finally:
            self.session.update.checking = False
            self._save()

    def _load(self) -> tuple[float, str]:
        with contextlib.suppress(Exception):
            with open(self.cache_path, encoding="utf-8") as file:
                data = json.load(file)
            latest = str(data.get("latest") or "")
            if UpdateStatus.version_tuple(latest):
                return float(data.get("checked_at") or 0), latest
        return 0.0, ""

    def _save(self) -> None:
        with contextlib.suppress(Exception):
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as file:
                json.dump({"checked_at": time.time(), "latest": self.session.update.latest}, file)

    @staticmethod
    def fetch_latest() -> str:
        request = Request(UpdateChecker.PYPI_URL, headers={"Accept": "application/json", "User-Agent": HTTP_USER_AGENT})
        with urlopen(request, timeout=UpdateChecker.TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        version = data.get("info", {}).get("version") if isinstance(data, dict) else ""
        if not isinstance(version, str) or not UpdateStatus.version_tuple(version):
            raise YucodeError("invalid PyPI version response")
        return version

    def status_line(self) -> str:
        update = self.session.update
        if update.checking:
            return "update: checking"
        if update.newer_than(__version__):
            return f"update: {__version__} -> {update.latest}"
        if update.error:
            return "update: error"
        return "update: current" if update.latest else "update: unknown"

    @staticmethod
    def upgrade_command() -> list[str]:
        """尽力给出升级 yucode 的包管理器命令,依据其安装方式推断。"""
        executable = os.path.realpath(sys.executable).replace(os.sep, "/")
        if "/uv/tools/" in executable:
            return ["uv", "tool", "upgrade", "yucode"]
        if "/pipx/venvs/" in executable:
            return ["pipx", "upgrade", "yucode"]
        return [sys.executable, "-m", "pip", "install", "--upgrade", "yucode"]
