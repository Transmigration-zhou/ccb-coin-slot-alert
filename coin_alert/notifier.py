"""macOS 系统通知（terminal-notifier）与响铃（afplay）。

terminal-notifier 按名称在 PATH 中查找；launchd / cron 下需要自己把 Homebrew 的 bin 目录加进 PATH，见 README.md。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

TERMINAL_NOTIFIER = "terminal-notifier"
AFPLAY = "/usr/bin/afplay"                       # macOS 自带
ALARM_SOUND = "/System/Library/Sounds/Glass.aiff"
NOTIFY_TIMEOUT = 5

_warned_missing = False


def _resolve(binary: str) -> Optional[str]:
    if "/" in binary:
        return binary if os.access(binary, os.X_OK) else None
    return shutil.which(binary)


def available(binary: str = TERMINAL_NOTIFIER) -> bool:
    """启动时检查通知程序是否可用，避免等到第一次告警才发现发不出去。"""
    return _resolve(binary) is not None


def notify(title: str, message: str, sound: Optional[str] = None, binary: str = TERMINAL_NOTIFIER) -> bool:
    """发一条系统通知。失败只记日志、不抛异常。"""
    global _warned_missing
    path = _resolve(binary)
    if path is None:
        if not _warned_missing:
            _warned_missing = True
            logger.warning("未找到 %s，将不会发送系统通知（安装：brew install terminal-notifier）", binary)
        return False
    args = [path, "-title", title, "-message", message]
    if sound:
        args += ["-sound", sound]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("发送通知失败：%s", e)
        return False
    if result.returncode != 0:
        logger.warning("发送通知失败（退出码 %s）：%s", result.returncode, result.stderr.strip())
        return False
    return True


class Alarm:
    """后台循环播放提示音，持续 seconds 秒或直到 stop()。正在响时再次 start() 不叠加。"""

    def __init__(self, seconds: float = 60, sound: str = ALARM_SOUND, player: str = AFPLAY):
        self._seconds = seconds
        self._sound = sound
        self._player = player
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

    def is_ringing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_ringing():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self, stop: threading.Event) -> None:
        deadline = time.monotonic() + self._seconds
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                with self._lock:
                    if stop.is_set():
                        break
                    self._proc = subprocess.Popen([self._player, self._sound])
                code = self._proc.wait()
            except OSError as e:
                logger.warning("响铃失败：%s", e)
                break
            if code != 0 and not stop.is_set():
                logger.warning("响铃程序退出码 %s，停止响铃", code)
                break
            stop.wait(0.05)
        self._proc = None

