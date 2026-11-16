"""把接口响应解析成结构化结果。纯函数：无 I/O、无日志，便于测试。

响应正文是一段 HTML/JS，网点数据形如：
    var banks = [{WDMC:'网点名', WDDH:'电话', JNBZS:'5', ...}, ...];
这是 JS 对象字面量而不是 JSON（键不带引号、字符串用单引号），所以这里用一个小型扫描器
逐个读取 "键: 值"，而不是用正则把整段改写成 JSON——后者会把值里的冒号、引号改坏。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

SNIPPET_LEN = 120
UNRECOGNIZED_LEN = 300


@dataclass(frozen=True)
class Bank:
    name: str                 # WDMC 网点名称
    count: int                # JNBZS 规范化后的余量
    phone: str                # WDDH 网点电话
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class Ok:
    banks: Tuple[Bank, ...]
    parse_errors: int = 0
    error_snippets: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejected:
    """响应带 <ErrMsg>：通常是会话失效，但也可能是其他业务错误，消息原样保留。"""
    message: str


@dataclass(frozen=True)
class Redirected:
    """3xx：会话失效时服务端会跳到登录页。"""
    status: int


@dataclass(frozen=True)
class HttpError:
    status: int


@dataclass(frozen=True)
class Unrecognized:
    """既没有网点数据也没有 ErrMsg：登录页、WAF 拦截页或页面格式变了。"""
    snippet: str


QueryResult = Union[Ok, Rejected, Redirected, HttpError, Unrecognized]

_BANKS_START = re.compile(r"var\s+banks\s*=\s*\[")
_ERR_MSG = re.compile(r"<ErrMsg>(.*?)</ErrMsg>", re.DOTALL)
_KEY = re.compile(r"\s*([A-Za-z_$][\w$]*)\s*:")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_LITERALS = {"null": None, "true": True, "false": False}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def parse_response(status: int, text: str) -> QueryResult:
    if 300 <= status < 400:
        return Redirected(status)
    if status >= 400:
        return HttpError(status)
    array = _extract_banks_array(text)
    if array is not None:
        return _parse_banks(array)
    if "ErrMsg" in text:
        m = _ERR_MSG.search(text)
        return Rejected(m.group(1).strip() if m and m.group(1).strip() else "未知错误")
    return Unrecognized(text[:UNRECOGNIZED_LEN].strip())


def parse_count(value: Any) -> Optional[int]:
    """把 JNBZS 规范化为整数；无法识别返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return int(float(s))
    except (ValueError, OverflowError):
        return None


def _snippet(s: str) -> str:
    return s.strip()[:SNIPPET_LEN]


def _parse_banks(array: str) -> Ok:
    banks: List[Bank] = []
    errors: List[str] = []
    for chunk in _split_elements(array):
        if not chunk.startswith("{"):
            errors.append(_snippet(chunk))
            continue
        raw, bad_fields = _parse_object(chunk[1:-1])
        if not raw:                      # 一个字段都认不出来：只记错误，不造一个空网点
            errors.append(_snippet(chunk))
            continue
        count = parse_count(raw.get("JNBZS"))
        if bad_fields or count is None:
            errors.append(_snippet(chunk))
        if count is None:
            count = 0
        banks.append(Bank(name=str(raw.get("WDMC") or ""), count=count,
                          phone=str(raw.get("WDDH") or ""), raw=raw))
    return Ok(banks=tuple(banks), parse_errors=len(errors), error_snippets=tuple(errors))


def _find_close(s: str, start: int) -> int:
    """s[start] 是 '[' 或 '{'，返回与之配对的右括号下标；引号内的括号不计。未闭合返回 -1。"""
    depth = 0
    quote = None
    i = start
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _extract_banks_array(text: str) -> Optional[str]:
    """返回 banks 数组方括号内部的文本；找不到或未闭合返回 None。"""
    m = _BANKS_START.search(text)
    if not m:
        return None
    start = m.end() - 1
    end = _find_close(text, start)
    if end < 0:
        return None
    return text[start + 1:end]


def _split_elements(array: str) -> List[str]:
    """把数组内部拆成顶层元素（通常是 {...}），跳过空白和逗号（允许尾逗号）。"""
    out: List[str] = []
    i = 0
    n = len(array)
    while i < n:
        c = array[i]
        if c.isspace() or c == ",":
            i += 1
            continue
        if c in "[{":
            end = _find_close(array, i)
            if end < 0:
                out.append(array[i:])
                break
            out.append(array[i:end + 1])
            i = end + 1
            continue
        j = array.find(",", i)   # 非对象元素：吞到下一个逗号，计为解析错误
        j = n if j < 0 else j
        out.append(array[i:j])
        i = j
    return out


def _read_string(s: str, i: int) -> Tuple[str, int]:
    quote = s[i]
    buf = []
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            buf.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if c == quote:
            return "".join(buf), i + 1
        buf.append(c)
        i += 1
    raise ValueError("字符串未闭合")


def _read_value(s: str, i: int) -> Tuple[Any, int]:
    while i < len(s) and s[i].isspace():
        i += 1
    if i >= len(s):
        raise ValueError("缺少值")
    if s[i] in "'\"":
        return _read_string(s, i)
    m = _NUMBER.match(s, i)
    if m:
        txt = m.group(0)
        return (float(txt) if "." in txt else int(txt)), m.end()
    for word, val in _LITERALS.items():
        if s.startswith(word, i):
            return val, i + len(word)
    raise ValueError(f"无法识别的值：{s[i:i + 20]!r}")


def _skip_field(s: str, i: int) -> int:
    """从 i 开始跳到下一个顶层逗号（引号、括号内的逗号不算），返回逗号下标或结尾。"""
    depth = 0
    quote = None
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c in "[{(":
            depth += 1
        elif c in "]})":
            depth -= 1
        elif c == "," and depth <= 0:
            return i
        i += 1
    return i


def _parse_object(body: str) -> Tuple[Dict[str, Any], int]:
    """解析 {} 内部的 "键: 值, 键: 值"。认不出的字段跳过并计数，其余字段照常保留——
    这样服务端以后加了一个奇怪的字段，也不会让整个网点（连同余量）被丢掉。"""
    result: Dict[str, Any] = {}
    bad = 0
    i = 0
    n = len(body)
    while True:
        while i < n and (body[i].isspace() or body[i] == ","):
            i += 1
        if i >= n:
            return result, bad
        field_start = i
        try:
            key, i = _read_key(body, i)
            value, i = _read_value(body, i)
            while i < n and body[i].isspace():
                i += 1
            if i < n and body[i] != ",":
                raise ValueError(f"值后出现多余内容：{body[i:i + 20]!r}")
            result[key] = value
        except ValueError:
            bad += 1
            i = _skip_field(body, field_start)


def _read_key(body: str, i: int) -> Tuple[str, int]:
    if body[i] in "'\"":
        key, i = _read_string(body, i)
        while i < len(body) and body[i].isspace():
            i += 1
        if i >= len(body) or body[i] != ":":
            raise ValueError("键后缺少冒号")
        return key, i + 1
    m = _KEY.match(body, i)
    if not m:
        raise ValueError(f"无法识别的键：{body[i:i + 20]!r}")
    return m.group(1), m.end()
