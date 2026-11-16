"""读取本地配置。两个文件都放在 config.json 所在目录，且都被 .gitignore 忽略：

cookie.txt —— 浏览器 Cookie 整行。获取方法：
    浏览器登录 https://jinianbi.ccb.com 后按 F12 → Network → 勾选 Fetch/XHR，
    找到 WCCMainPlatV5_2?...TXCODE=NYB004 这条请求，在 Headers 里复制 Cookie 整行粘贴进来
    （带不带开头的 "Cookie: " 都可以；# 开头的行视为注释）。
    更新该文件后，运行中的监控会自动重新加载，无需重启。
    注意：抓完 Cookie 后不要在同一浏览器会话里继续操作，否则令牌会被互相作废。

config.json —— 字段说明（以 _ 开头的键会被忽略，可用来写备注）：
    crdt_no                  必填  预约人身份证号，须与登录账号一致
    product_id               必填  纪念币批次 ID，每期不同，取自上面那条请求的 PRODUCT_ID 参数
    city                     必填  城市名，须是 coin_alert/regions.py 中 REGIONS 的键
    districts                可选  要监控的区县名列表，省略则监控该城市全部区县
    interval_seconds         可选  一轮结束后等待多少秒开始下一轮，默认 10
    jitter_seconds           可选  每轮间隔额外随机增加 0~N 秒，默认 0（关闭）
    district_gap_seconds     可选  同一轮内区县之间的随机停顿 [最小, 最大] 秒，默认 [0, 0]
    failure_alert_threshold  可选  某区县连续失败多少轮后发告警，默认 3
    log_file                 可选  日志文件路径（相对 config.json 所在目录），默认 logs/monitor.log；null 表示只输出到终端
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from .regions import UnknownRegionError, districts_of, resolve_district

REQUIRED = ("crdt_no", "product_id", "city")


class ConfigError(Exception):
    """配置缺失或不合法；消息直接展示给用户。"""


@dataclass(frozen=True)
class Config:
    crdt_no: str
    product_id: str
    city: str
    districts: Tuple[Tuple[str, str], ...]
    cookie_file: Path
    state_file: Path
    lock_file: Path
    log_file: Optional[Path]
    interval_seconds: float = 10
    jitter_seconds: float = 0
    district_gap_seconds: Tuple[float, float] = (0, 0)
    failure_alert_threshold: int = 3


def load_config(path: Path) -> Config:
    path = Path(path)
    base = path.parent
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"找不到配置文件 {path}，请复制 config.example.json 为 config.json 并填写") from None
    except (OSError, ValueError) as e:
        raise ConfigError(f"无法读取配置文件 {path}：{e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件 {path} 顶层必须是 JSON 对象")
    data = {k: v for k, v in raw.items() if not k.startswith("_")}

    missing = [k for k in REQUIRED if not str(data.get(k) or "").strip()]
    if missing:
        raise ConfigError(f"配置文件 {path} 缺少必填项：{', '.join(missing)}")

    city = str(data["city"])
    try:
        if data.get("districts"):
            districts = tuple(resolve_district(city, str(d)) for d in data["districts"])
        else:
            districts = tuple(districts_of(city))
    except UnknownRegionError as e:
        raise ConfigError(str(e)) from None

    log_file: Optional[Path] = base / "logs" / "monitor.log"
    if "log_file" in data:
        log_file = None if data["log_file"] is None else base / str(data["log_file"])

    try:
        gap = tuple(float(x) for x in data.get("district_gap_seconds", (0, 0)))
        if len(gap) != 2:
            raise ValueError("district_gap_seconds 必须是 [最小, 最大]")
        return Config(
            crdt_no=str(data["crdt_no"]).strip(),
            product_id=str(data["product_id"]).strip(),
            city=city,
            districts=districts,
            cookie_file=base / "cookie.txt",
            state_file=base / ".cookie_state.json",
            lock_file=base / ".monitor.lock",
            log_file=log_file,
            interval_seconds=float(data.get("interval_seconds", 10)),
            jitter_seconds=float(data.get("jitter_seconds", 0)),
            district_gap_seconds=(gap[0], gap[1]),
            failure_alert_threshold=int(data.get("failure_alert_threshold", 3)),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"配置文件 {path} 中的数值不合法：{e}") from None


def parse_cookie_text(text: str) -> Dict[str, str]:
    """把浏览器复制的 'k=v; k=v' 解析成字典；容忍 'Cookie:' 前缀、多行、CRLF 与注释行。"""
    cookies: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line[len("cookie:"):]
        for item in line.split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key.strip():
                cookies[key.strip()] = value.strip()
    return cookies


def read_cookie_file(path: Path) -> Dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"找不到 {path}，请把浏览器复制的 Cookie 整行粘贴到 cookie.txt") from None
    except OSError as e:
        raise ConfigError(f"无法读取 {path}：{e}") from None
    cookies = parse_cookie_text(text)
    if "JSESSIONID" not in cookies:
        raise ConfigError(f"{path} 中没有 JSESSIONID，请重新从浏览器复制完整的 Cookie 整行")
    return cookies


def ensure_private(path: Path) -> bool:
    """权限宽于 0600 时收紧为 0600，返回是否做了修改。文件不存在返回 False。"""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return False
    if mode & 0o077:
        os.chmod(path, 0o600)
        return True
    return False
