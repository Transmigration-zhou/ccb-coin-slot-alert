import os
import signal
import stat
import subprocess
import sys
import time

import pytest

from coin_alert.lock import LockHeld, ProcessLock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_second_lock_in_same_process_fails(tmp_path):
    with ProcessLock(tmp_path / ".monitor.lock"):
        with pytest.raises(LockHeld) as e:
            ProcessLock(tmp_path / ".monitor.lock").acquire()
        assert e.value.pid == os.getpid()
        assert "已有进程" in str(e.value) and str(os.getpid()) in str(e.value)


def test_released_after_with(tmp_path):
    with ProcessLock(tmp_path / ".monitor.lock"):
        pass
    lock = ProcessLock(tmp_path / ".monitor.lock")
    lock.acquire()
    lock.release()


def test_lock_freed_when_holder_killed(tmp_path):
    path = tmp_path / ".monitor.lock"
    code = ("import sys,time; sys.path.insert(0, sys.argv[1]); from coin_alert.lock import ProcessLock; "
            "l=ProcessLock(sys.argv[2]); l.acquire(); print('ok', flush=True); time.sleep(30)")
    child = subprocess.Popen([sys.executable, "-c", code, ROOT, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ok"
        with pytest.raises(LockHeld):
            ProcessLock(path).acquire()
        child.send_signal(signal.SIGKILL)
        child.wait(5)
        lock = ProcessLock(path)
        lock.acquire()
        lock.release()
    finally:
        if child.poll() is None:
            child.kill()


def test_lock_file_mode(tmp_path):
    with ProcessLock(tmp_path / ".monitor.lock"):
        assert stat.S_IMODE((tmp_path / ".monitor.lock").stat().st_mode) == 0o600
