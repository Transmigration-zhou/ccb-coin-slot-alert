"""单进程锁：同一组会话只能被一个进程使用，否则多个进程会抢滚动令牌、互相把对方作废。

用 fcntl.flock 持有锁文件：进程退出（包括 kill -9）时内核自动释放，
不会留下"陈旧的锁"，也不受 PID 复用影响。文件里写的 PID 只是给人看的。
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional


class LockHeld(Exception):
    def __init__(self, pid: Optional[int]):
        self.pid = pid
        who = f"PID {pid}" if pid else "PID 未知"
        super().__init__(f"已有进程在使用该会话（{who}），本次退出。监控和单次查询不能同时运行。")


class ProcessLock:
    def __init__(self, path: Path):
        self._path = str(path)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = _read_pid(fd)
            os.close(fd)
            raise LockHeld(pid) from None
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _read_pid(fd: int) -> Optional[int]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return int(os.read(fd, 32).decode().strip())
    except (OSError, ValueError):
        return None
