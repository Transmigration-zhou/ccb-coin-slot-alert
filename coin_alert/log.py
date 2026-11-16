"""日志初始化与脱敏：任何日志里都不能出现身份证号或 Cookie 值。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, List, Optional

FORMAT = "%(asctime)s %(levelname)s %(message)s"
MIN_SECRET_LEN = 4   # 太短的值（如 "1"）替换掉会把正常日志打成一片 ***
MASK = "***"


class RedactFilter(logging.Filter):
    def __init__(self, secrets: Iterable[Optional[str]] = ()):
        super().__init__()
        self._secrets: List[str] = []
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[Optional[str]]) -> None:
        # 长的先替换，避免一个值是另一个值的子串时漏掉一部分
        self._secrets = sorted({s for s in secrets if s and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True)

    def _redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = self._redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return True


def setup_logging(log_file: Optional[Path], verbose: bool) -> RedactFilter:
    """配置根 logger：终端 + 可选滚动文件（5MB × 3）。返回挂在所有 handler 上的脱敏过滤器。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in [h for h in root.handlers if getattr(h, "_coin_alert", False)]:
        root.removeHandler(h)
        h.close()

    redact = RedactFilter()
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(logging.Formatter(FORMAT))
        h.addFilter(redact)
        h._coin_alert = True  # type: ignore[attr-defined]
        root.addHandler(h)
    return redact
