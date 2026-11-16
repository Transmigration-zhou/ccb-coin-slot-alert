import logging
import os
import time

from coin_alert import notifier
from coin_alert.notifier import Alarm, notify


def script(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def test_notify_missing_binary_warns_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(notifier, "_warned_missing", False)
    with caplog.at_level(logging.WARNING):
        assert notify("t", "m", binary=str(tmp_path / "nope")) is False
        assert notify("t", "m", binary=str(tmp_path / "nope")) is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "brew install terminal-notifier" in warnings[0].getMessage()


def test_notify_finds_binary_on_path(tmp_path, monkeypatch):
    out = tmp_path / "args"
    script(tmp_path / "fake-notifier", f'echo "$@" > {out}')
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert notify("标题", "正文", sound="default", binary="fake-notifier") is True
    args = out.read_text()
    assert "-title 标题" in args and "-message 正文" in args and "-sound default" in args


def test_notify_nonzero_exit_returns_false(tmp_path):
    assert notify("t", "m", binary=script(tmp_path / "bad", "exit 3")) is False


def wait_until(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_alarm_stop_terminates_child(tmp_path):
    pidfile = tmp_path / "pid"
    player = script(tmp_path / "player", f"echo $$ > {pidfile}; exec sleep 30")
    alarm = Alarm(seconds=60, sound="x", player=player)
    alarm.start()
    assert wait_until(pidfile.exists) and alarm.is_ringing()
    pid = int(pidfile.read_text())
    alarm.stop()
    assert wait_until(lambda: not alarm.is_ringing(), 1.0)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_alarm_does_not_stack(tmp_path):
    log = tmp_path / "starts"
    player = script(tmp_path / "player", f"echo x >> {log}; exec sleep 30")
    alarm = Alarm(seconds=60, sound="x", player=player)
    alarm.start()
    alarm.start()
    assert wait_until(log.exists)
    time.sleep(0.2)
    alarm.stop()
    assert log.read_text().count("x") == 1


def test_alarm_ends_after_seconds(tmp_path):
    player = script(tmp_path / "player", "exit 0")
    alarm = Alarm(seconds=0.3, sound="x", player=player)
    alarm.start()
    assert wait_until(lambda: not alarm.is_ringing(), 2.0)
