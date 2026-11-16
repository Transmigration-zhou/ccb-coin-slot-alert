"""决定"什么时候该提醒用户"。纯逻辑，不直接发通知，由 monitor 把返回的 Alert 交给 notifier。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .parser import Bank

MAX_MESSAGE_LEN = 1000
SOUND_GOOD = "default"
SOUND_BAD = "Basso"


@dataclass(frozen=True)
class Alert:
    title: str
    message: str
    sound: str


class StockTracker:
    """有货去重：只有出现新网点或数量增加时才需要提醒。"""

    def __init__(self) -> None:
        self._last: Dict[str, Dict[str, int]] = {}

    def update(self, stock: Dict[str, Tuple[Bank, ...]], failed: Set[str]) -> List[Tuple[str, Bank]]:
        """stock：本轮查询成功的区县 → 有货网点；failed 中的区县沿用上次库存，不视为"网点消失"。"""
        new: List[Tuple[str, Bank]] = []
        for district, banks in stock.items():
            if district in failed:
                continue
            before = self._last.get(district, {})
            for bank in banks:
                if bank.count > before.get(bank.name, 0):
                    new.append((district, bank))
            self._last[district] = {bk.name: bk.count for bk in banks if bk.count > 0}
        return sorted(new, key=lambda item: (item[0], item[1].name))


def format_stock_alert(city: str, items: List[Tuple[str, Bank]]) -> Alert:
    lines = [f"{d} {bk.name} 数量:{bk.count} 电话:{bk.phone or '未知'}" for d, bk in items]
    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_LEN:
        suffix = f"…等 {len(items)} 个网点"
        kept: List[str] = []
        for line in lines:
            if len("\n".join(kept + [line])) + 1 + len(suffix) > MAX_MESSAGE_LEN:
                break
            kept.append(line)
        message = "\n".join(kept + [suffix])
    return Alert(title=f"建行「{city}」纪念币可用提醒", message=message, sound=SOUND_GOOD)


class HealthTracker:
    """监控健康状态。每类问题在恢复前只告警一次；恢复时发一次"已恢复"。"""

    def __init__(self, threshold: int):
        self._threshold = max(1, threshold)
        self._session_alerted = False
        self._fail_counts: Dict[str, int] = {}
        self._district_alerted: Set[str] = set()
        self._parse_alerted: Set[str] = set()

    def session_invalid(self, detail: str) -> Optional[Alert]:
        if self._session_alerted:
            return None
        self._session_alerted = True
        return Alert(
            title="建行纪念币监控：会话可能已失效",
            message=f"{detail}\n请重新抓 Cookie 粘贴到 cookie.txt，程序会自动加载",
            sound=SOUND_BAD,
        )

    def session_ok(self) -> Optional[Alert]:
        if not self._session_alerted:
            return None
        self._session_alerted = False
        return Alert(title="建行纪念币监控已恢复", message="会话已恢复正常，继续监控", sound=SOUND_GOOD)

    def district_failed(self, district: str, reason: str) -> Optional[Alert]:
        n = self._fail_counts.get(district, 0) + 1
        self._fail_counts[district] = n
        if n != self._threshold:
            return None
        self._district_alerted.add(district)
        return Alert(
            title="建行纪念币监控：查询连续失败",
            message=f"「{district}」已连续 {n} 轮查询失败：{reason}",
            sound=SOUND_BAD,
        )

    def district_ok(self, district: str) -> Optional[Alert]:
        self._fail_counts.pop(district, None)
        if district not in self._district_alerted:
            return None
        self._district_alerted.discard(district)
        return Alert(title="建行纪念币监控已恢复", message=f"「{district}」查询已恢复正常", sound=SOUND_GOOD)

    def parse_errors(self, district: str, snippets: Tuple[str, ...]) -> Optional[Alert]:
        if district in self._parse_alerted:
            return None
        self._parse_alerted.add(district)
        sample = "\n".join(snippets[:3])
        return Alert(
            title="建行纪念币监控：解析异常",
            message=f"「{district}」有 {len(snippets)} 条网点数据无法解析，可能漏报：\n{sample}",
            sound=SOUND_BAD,
        )

    def parse_ok(self, district: str) -> Optional[Alert]:
        if district not in self._parse_alerted:
            return None
        self._parse_alerted.discard(district)
        return Alert(title="建行纪念币监控已恢复", message=f"「{district}」网点数据解析已恢复正常", sound=SOUND_GOOD)
