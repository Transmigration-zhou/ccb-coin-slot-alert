"""macOS 系统通知：直接调用 terminal-notifier（https://github.com/julienXX/terminal-notifier）。

用绝对路径查找程序，不依赖 PATH，在 cron / launchd 等精简环境下也能用。
安装：brew install terminal-notifier
"""
import os
import shutil
import subprocess

_CANDIDATES = (
    "/opt/homebrew/bin/terminal-notifier",  # Apple Silicon 上的 Homebrew
    "/usr/local/bin/terminal-notifier",  # Intel 上的 Homebrew
)


def _find_bin():
    found = shutil.which("terminal-notifier")
    if found:
        return found
    return next((p for p in _CANDIDATES if os.access(p, os.X_OK)), None)


TERMINAL_NOTIFIER = _find_bin()
NOTIFIER_AVAILABLE = TERMINAL_NOTIFIER is not None

if not NOTIFIER_AVAILABLE:
    print("未找到 terminal-notifier，将不会发送系统通知（安装：brew install terminal-notifier）")


def notify(message, title="", sound=None):
    """发送一条系统通知。失败只打印原因，不抛异常，不影响调用方继续运行。"""
    if not NOTIFIER_AVAILABLE:
        return False
    args = [TERMINAL_NOTIFIER, "-message", str(message)]
    if title:
        args += ["-title", str(title)]
    if sound:
        args += ["-sound", str(sound)]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"发送通知失败: {e}")
        return False
    if result.returncode != 0:
        print(f"发送通知失败（退出码 {result.returncode}）: {result.stderr.strip()}")
        return False
    return True
