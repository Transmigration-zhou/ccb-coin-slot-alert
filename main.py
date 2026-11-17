"""建行纪念币预约余量监控。

用法：
    python main.py                 # 持续监控（默认）
    python main.py once [区县]     # 单次查询；不带参数查配置中的全部区县；区县可写名称或代码
    python main.py list [城市]     # 列出支持的城市 / 某城市的区县代码
选项：
    -c, --config 路径   配置文件，默认与 main.py 同目录的 config.json
    -v, --verbose       输出 DEBUG 日志（包括每轮完整的网点列表）

首次使用：复制 config.example.json 为 config.json 并填写，再把浏览器 Cookie 整行粘贴到 cookie.txt。
字段与 Cookie 获取方法见 coin_alert/config.py 顶部说明。
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional

import requests

from coin_alert import notifier
from coin_alert.alerter import Alert
from coin_alert.config import Config, ConfigError, ensure_private, load_config
from coin_alert.lock import LockHeld, ProcessLock
from coin_alert.log import setup_logging
from coin_alert.monitor import Monitor
from coin_alert.parser import HttpError, Ok, Redirected, Rejected, Unrecognized, parse_response
from coin_alert.regions import (UnknownRegionError, canonical_city, cities_by_province, districts_of,
                                resolve_district)
from coin_alert.session import CcbSession

logger = logging.getLogger("coin_alert.main")

EXIT_OK, EXIT_FAIL, EXIT_SESSION_INVALID = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="建行纪念币预约余量监控")
    p.add_argument("-c", "--config", type=Path, default=Path(__file__).resolve().parent / "config.json",
                   help="配置文件路径（默认：main.py 同目录的 config.json）")
    p.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    sub = p.add_subparsers(dest="command")
    once = sub.add_parser("once", help="单次查询")
    once.add_argument("district", nargs="?", help="区县名称或代码；省略则查配置中的全部区县")
    lst = sub.add_parser("list", help="列出支持的城市 / 区县")
    lst.add_argument("city", nargs="?", help="城市名")
    return p


def cmd_list(city: Optional[str]) -> int:
    if city is None:
        print("支持的城市（按省份）：")
        for prov, names in cities_by_province().items():
            print(f"  {prov}：{'、'.join(names)}")
        return EXIT_OK
    pairs = districts_of(city)
    print(f"{canonical_city(city)} 的区县代码：")
    for name, code in pairs:
        print(f"  {name}: {code}")
    return EXIT_OK


def prepare(config_path: Path, verbose: bool):
    """加载配置、收紧文件权限、初始化日志。返回 (config, redact)。"""
    config = load_config(config_path)
    redact = setup_logging(config.log_file, verbose)
    for path in (config_path, config.cookie_file):
        if ensure_private(path):
            logger.info("已把 %s 的权限收紧为 600（只有你自己能读）", path)
    return config, redact


def cmd_once(config: Config, redact, district: Optional[str]) -> int:
    targets = [resolve_district(config.city, district)] if district else list(config.districts)
    with ProcessLock(config.lock_file):
        session = CcbSession(config)
        redact.set_secrets([config.crdt_no] + session.cookie_values())
        code = EXIT_OK
        for name, dcode in targets:
            try:
                result = parse_response(*session.query(dcode))
            except requests.RequestException as e:
                logger.warning("「%s」请求失败：%s", name, e)
                code = max(code, EXIT_FAIL)
                continue
            if isinstance(result, (Rejected, Redirected)):
                why = result.message if isinstance(result, Rejected) else f"HTTP {result.status} 跳转"
                print(f"「{name}」会话可能已失效：{why}。请重新抓 Cookie 粘贴到 cookie.txt")
                return EXIT_SESSION_INVALID
            if isinstance(result, HttpError):
                print(f"「{name}」查询失败：HTTP {result.status}")
                code = max(code, EXIT_FAIL)
            elif isinstance(result, Unrecognized):
                print(f"「{name}」响应中没有网点数据（可能是登录页或拦截页）：{result.snippet[:200]}")
                code = max(code, EXIT_FAIL)
            elif isinstance(result, Ok):
                available = [b for b in result.banks if b.count > 0]
                print(f"「{name}」共 {len(result.banks)} 个网点，" + (f"{len(available)} 个有货：" if available else "均无货"))
                for b in available:
                    print(f"    {b.name}  数量:{b.count}  电话:{b.phone or '未知'}")
                if result.parse_errors:
                    print(f"    注意：{result.parse_errors} 条网点数据无法解析，可能漏报：{result.error_snippets[:3]}")
        return code


def install_signal_handlers(stop: threading.Event) -> None:
    """SIGINT / SIGTERM / SIGHUP 都改为"设置 stop、跑完清理再退出"。
    - 第二次 Ctrl+C 直接抛 KeyboardInterrupt，不用等当前请求结束；
    - 用 nohup 启动时 SIGHUP 已被忽略，保持忽略，关掉终端不会让监控退出。"""
    def handle(signum, _frame):
        if stop.is_set() and signum == signal.SIGINT:
            raise KeyboardInterrupt
        logger.info("收到退出信号（%s），正在退出…（再按一次 Ctrl+C 立即退出）", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if sig == signal.SIGHUP and signal.getsignal(sig) == signal.SIG_IGN:
            continue
        signal.signal(sig, handle)


def send(alert) -> bool:
    return notifier.notify(alert.title, alert.message, alert.sound)


def cmd_monitor(config: Config, redact) -> int:
    stop = threading.Event()
    install_signal_handlers(stop)
    if not notifier.available():
        logger.error("未找到 %s：有货和失效提醒都无法弹出系统通知，只会写日志！安装：brew install terminal-notifier",
                     notifier.TERMINAL_NOTIFIER)

    alarm = notifier.Alarm()
    with ProcessLock(config.lock_file):
        session = CcbSession(config)
        redact.set_secrets([config.crdt_no] + session.cookie_values())
        monitor = Monitor(config, session, notify=send, alarm=alarm, redact=redact)
        try:
            monitor.run_forever(stop)
        except BaseException as e:
            if not isinstance(e, KeyboardInterrupt):
                logger.exception("监控意外退出")
                send(Alert(title="建行纪念币监控已停止", message=f"监控意外退出：{type(e).__name__}，请查看日志并重新启动",
                           sound="Basso"))
            raise
        finally:
            alarm.stop()
    logger.info("程序已退出")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list(args.city)
        config, redact = prepare(args.config, args.verbose)
        if args.command == "once":
            return cmd_once(config, redact, args.district)
        return cmd_monitor(config, redact)
    except (ConfigError, LockHeld, UnknownRegionError) as e:
        print(str(e), file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("\n已强制退出", file=sys.stderr)
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
