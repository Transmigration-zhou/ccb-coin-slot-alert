"""监控主循环：按节奏轮询各区县，处理有货提醒、失效检测、退避和 cookie.txt 热加载。

一轮 = 按顺序查询配置中的全部区县。规则：
- 个别区县失败不影响其他区县，一轮总是跑完；但遇到会话失效（ErrMsg / 3xx）立即结束本轮，
  因为后面的请求必然同样失败，继续请求只会增加风控风险。
- 区县各自累计连续失败次数，达到阈值单独告警。
- 只有一轮内全部区县都失败才退避（间隔翻倍，上限 MAX_BACKOFF）；会话失效后每 INVALID_PROBE 秒探测一次。
- 任何状态下 cookie.txt 被修改都会立即重新加载并马上开始新一轮。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests

from .alerter import SOUND_BAD, SOUND_GOOD, Alert, HealthTracker, StockTracker, combine_alerts, format_stock_alert
from .config import Config, ConfigError
from .log import RedactFilter
from .parser import Bank, HttpError, Ok, Redirected, Rejected, Unrecognized, parse_response

logger = logging.getLogger(__name__)

NORMAL, INVALID = "normal", "invalid"
MAX_BACKOFF = 300
INVALID_PROBE = 600
TICK = 1.0
LOOP = "监控循环"      # HealthTracker 里给"循环本身出错"用的伪区县名


@dataclass
class RoundOutcome:
    stock: Dict[str, Tuple[Bank, ...]] = field(default_factory=dict)
    failed: Set[str] = field(default_factory=set)
    session_invalid: bool = False
    all_failed: bool = False


class Monitor:
    def __init__(self, config: Config, session, notify: Callable[[Alert], None], alarm,
                 redact: Optional[RedactFilter] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[float, float], float] = random.uniform):
        self._config = config
        self._session = session
        self._notify = notify
        self._alarm = alarm
        self._redact = redact
        self._sleep = sleep
        self._clock = clock
        self._rand = rand
        self._stock = StockTracker()
        self._health = HealthTracker(config.failure_alert_threshold)
        self._backoff = config.interval_seconds
        self._bad_mtime: Optional[float] = None
        self._pending: List[Alert] = []
        self.state = NORMAL

    # ---- 一轮 ----

    def run_round(self, stop: Optional[threading.Event] = None) -> RoundOutcome:
        out = RoundOutcome()
        any_ok = False
        gap_lo, gap_hi = self._config.district_gap_seconds
        for i, (name, code) in enumerate(self._config.districts):
            if stop is not None and stop.is_set():
                self._flush()
                return out          # 正在退出：不再发请求，也不再响铃
            if i > 0 and gap_hi > 0:
                self._sleep(self._rand(gap_lo, gap_hi))
            reason = self._query_district(name, code, out)
            if out.session_invalid:
                break
            if reason is None:
                any_ok = True
            else:
                out.failed.add(name)
                self._emit(self._health.district_failed(name, reason))

        if out.session_invalid:
            self.state = INVALID
        elif any_ok:
            self.state = NORMAL
            self._emit(self._health.session_ok())
        out.all_failed = not out.session_invalid and not any_ok and bool(self._config.districts)

        self._flush()
        new = self._stock.update(out.stock, out.failed)
        if new:
            logger.info("发现纪念币！\n%s", "\n".join(f"{d} {b.name} 数量:{b.count} 电话:{b.phone}" for d, b in new))
            self._send(format_stock_alert(self._config.city, new))
            self._alarm.start()
        return out

    def _query_district(self, name: str, code: str, out: RoundOutcome) -> Optional[str]:
        """查询一个区县；成功返回 None，失败返回原因。会话失效时设置 out.session_invalid。"""
        try:
            status, text = self._session.query(code)
            result = parse_response(status, text)
        except requests.RequestException as e:
            logger.warning("「%s」请求失败：%s", name, e)
            return f"网络错误（{type(e).__name__}）"
        except Exception:
            logger.exception("「%s」查询时出现未预期的异常", name)
            return "程序异常，详见日志"

        if isinstance(result, (Rejected, Redirected)):
            detail = (f"服务端返回：{result.message}" if isinstance(result, Rejected)
                      else f"服务端返回跳转（HTTP {result.status}），通常是登录已失效")
            logger.error("「%s」%s", name, detail)
            out.session_invalid = True
            self._emit(self._health.session_invalid(detail))
            return detail
        if isinstance(result, HttpError):
            logger.warning("「%s」HTTP %s", name, result.status)
            return f"HTTP {result.status}"
        if isinstance(result, Unrecognized):
            logger.warning("「%s」响应中没有网点数据：%s", name, result.snippet[:200])
            return "响应中没有网点数据（可能是登录页或拦截页）"

        assert isinstance(result, Ok)
        self._emit(self._health.district_ok(name))
        if result.parse_errors:
            logger.warning("「%s」%d 条网点数据无法解析：%s", name, result.parse_errors, result.error_snippets)
            self._emit(self._health.parse_errors(name, result.error_snippets))
        else:
            self._emit(self._health.parse_ok(name))
        available = tuple(b for b in result.banks if b.count > 0)
        out.stock[name] = available
        logger.debug("「%s」网点：%s", name, result.banks)
        logger.info("「%s」%d 个网点，%s", name, len(result.banks),
                    f"{len(available)} 个有货" if available else "均无货")
        return None

    def _emit(self, alert: Optional[Alert]) -> None:
        """本轮内的状态告警先攒着，一轮结束时合并成一条发出。"""
        if alert is not None:
            self._log_alert(alert)
            self._pending.append(alert)

    def _flush(self) -> None:
        if self._pending:
            alerts, self._pending = self._pending, []
            self._safe_notify(combine_alerts(alerts))

    def _send(self, alert: Optional[Alert]) -> None:
        if alert is not None:
            self._log_alert(alert)
            self._safe_notify(alert)

    def _safe_notify(self, alert: Alert) -> None:
        try:
            self._notify(alert)
        except Exception:
            logger.exception("发送通知时出现异常")

    @staticmethod
    def _log_alert(alert: Alert) -> None:
        # 通知发不出去时（例如没装 terminal-notifier），日志里至少还有记录
        level = logging.ERROR if alert.sound == SOUND_BAD else logging.INFO
        logger.log(level, "【%s】%s", alert.title, alert.message)

    # ---- 节奏 ----

    def next_delay(self, outcome: RoundOutcome) -> float:
        interval = self._config.interval_seconds
        if self.state == INVALID:
            self._backoff = interval
            delay = INVALID_PROBE
        elif outcome.all_failed:
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            delay = self._backoff
        else:
            self._backoff = interval
            delay = interval
        if self._config.jitter_seconds > 0:
            delay += self._rand(0, self._config.jitter_seconds)
        return delay

    def run_forever(self, stop: threading.Event) -> None:
        cfg = self._config
        logger.info("开始监控「%s」：%s", cfg.city, "、".join(n for n, _ in cfg.districts))
        logger.info("每轮结束后等待 %s 秒；有货时发送系统通知并响铃；按 Ctrl+C 退出", cfg.interval_seconds)
        logger.warning("抓取 Cookie 后请勿在浏览器继续使用同一登录，否则令牌会被互相作废")
        while not stop.is_set():
            try:
                outcome = self.run_round(stop)
                if stop.is_set():
                    break
                delay = self.next_delay(outcome)
                if self.state == INVALID:
                    logger.info("会话失效，%d 秒后再探测；更新 cookie.txt 会立即恢复", delay)
                elif outcome.all_failed:
                    logger.info("本轮全部失败，%d 秒后重试", delay)
                self._wait(delay, stop)
                self._send(self._health.district_ok(LOOP))
            except Exception:
                # 兜底：任何意外都不能让监控进程悄悄退出；连续出现时按阈值告警
                logger.exception("监控循环出现未预期的异常")
                self._send(self._health.district_failed(LOOP, "程序异常，详见日志"))
                self._sleep(self._config.interval_seconds)

    def _wait(self, delay: float, stop: threading.Event) -> None:
        """短睡眠等待，每秒检查一次停止信号和 cookie.txt 是否被修改。"""
        end = self._clock() + delay
        while not stop.is_set():
            if self._cookie_changed() and self._reload_cookies():
                return
            remaining = end - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(TICK, remaining))

    def _cookie_changed(self) -> bool:
        mtime = self._session.cookie_file_mtime()
        return mtime != self._session.loaded_mtime and mtime != self._bad_mtime

    def _reload_cookies(self) -> bool:
        try:
            reloaded = self._session.reload_from_cookie_file()
        except ConfigError as e:
            self._bad_mtime = self._session.cookie_file_mtime()
            self._send(Alert(title="建行纪念币监控：cookie.txt 无法加载", message=str(e), sound=SOUND_BAD))
            return False
        self._bad_mtime = None
        if reloaded is False:      # 只是重新保存，内容没变
            return False
        self._health.session_reset()
        self._send(Alert(title="建行纪念币监控：已加载新 Cookie", message="立即开始新一轮查询", sound=SOUND_GOOD))
        self._backoff = self._config.interval_seconds
        self.state = NORMAL
        if self._redact is not None:
            self._redact.set_secrets([self._config.crdt_no] + self._session.cookie_values())
        logger.info("检测到 cookie.txt 更新，已重新加载，立即开始新一轮查询")
        return True
