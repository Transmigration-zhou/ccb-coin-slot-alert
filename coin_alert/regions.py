"""城市 → 区县代码表。新增城市或区县只需改这里。

区县代码即请求参数 CntyAndDstc_Cd，等同国家行政区划代码（GB/T 2260 县级 6 位）。
获取方法：在 jinianbi.ccb.com 选择对应区县查询，
F12 → Network 中找到 TXCODE=NYB004 的请求，看它的 CntyAndDstc_Cd 参数。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

REGIONS: Dict[str, Dict[str, str]] = {
    "杭州": {
        "上城区": "330102",
        "拱墅区": "330105",
        "西湖区": "330106",
        "滨江区": "330108",
        "萧山区": "330109",
        "余杭区": "330110",
        "钱塘区": "330114",
    },
}


class UnknownRegionError(ValueError):
    """城市或区县不在 REGIONS 中。"""


def cities() -> List[str]:
    return list(REGIONS)


def districts_of(city: str) -> List[Tuple[str, str]]:
    """返回某城市的 [(区县名, 代码)]，顺序与 REGIONS 中一致。"""
    try:
        return list(REGIONS[city].items())
    except KeyError:
        raise UnknownRegionError(
            f"不支持的城市「{city}」，可选城市：{'、'.join(cities())}") from None


def resolve_district(city: str, key: str) -> Tuple[str, str]:
    """按区县名称或代码查找，返回 (名称, 代码)。"""
    pairs = districts_of(city)
    for name, code in pairs:
        if key in (name, code):
            return name, code
    raise UnknownRegionError(
        f"「{city}」没有区县「{key}」，可选：{'、'.join(n for n, _ in pairs)}")
