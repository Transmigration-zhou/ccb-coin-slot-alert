"""与建行纪念币接口的 HTTP 会话，以及滚动令牌的持久化。

建行每次响应都会通过 Set-Cookie 下发新的 tranCCBIBS1 / tranFAVOR，旧值随即作废，所以：
- 下一次请求必须带上最新值；
- 新值要落盘，重启后接着用（否则会拿 cookie.txt 里已作废的旧值）；
- 同一时刻只能有一个进程使用这组会话（见 lock.py）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from .config import Config, read_cookie_file

logger = logging.getLogger(__name__)

SERVLET_NAME = "WCCMainPlatV5"
URL = f"https://jinianbi.ccb.com/tran/{SERVLET_NAME}"
TIMEOUT = (5, 15)   # (连接, 读取) 秒
ROLLING_KEYS = ("tranCCBIBS1", "tranFAVOR")

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://jinianbi.ccb.com",
    "Pragma": "no-cache",
    "Referer": f"{URL}?CCB_IBSVersion=V5&SERVLET_NAME={SERVLET_NAME}",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
    "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


class CcbSession:
    def __init__(self, config: Config, url: str = URL):
        self._config = config
        self._url = url
        self._host = urlparse(url).hostname or ""
        self._http = requests.Session()
        # 建行走直连：系统代理是境外出口，会话会因 IP 跳变被风控踢掉
        self._http.trust_env = False
        self._http.headers.update(HEADERS)
        self._cookies: Dict[str, str] = {}
        self._origin_jsessionid = ""
        self._cookie_hash = ""      # cookie.txt 解析结果的指纹，用来判断用户是否真的换了 Cookie
        self.loaded_mtime = 0.0
        self._load(use_state=True)

    # ---- 对外接口 ----

    def query(self, district_code: str) -> Tuple[int, str]:
        """查询一个区县，返回 (HTTP 状态码, 正文)。网络异常原样抛出 requests.RequestException。"""
        params = {
            "CCB_IBSVersion": "V5",
            "SERVLET_NAME": SERVLET_NAME,
            "isAjaxRequest": "true",
            "TXCODE": "NYB004",
            "CntyAndDstc_Cd": district_code,
            "PRODUCT_ID": self._config.product_id,
            "JNB_DATE_TYPE": "0",
            "CRDT_NO": self._config.crdt_no,
        }
        response = self._http.post(self._url, params=params, timeout=TIMEOUT, allow_redirects=False)
        self._absorb(response)
        if "charset" not in response.headers.get("Content-Type", "").lower():
            # 没声明编码时 requests 会按 ISO-8859-1 解码，中文网点名会乱码
            response.encoding = response.apparent_encoding
        return response.status_code, response.text

    def reload_from_cookie_file(self) -> bool:
        """cookie.txt 被修改时调用。内容确实变了才以它为准（丢弃落盘的滚动令牌）并返回 True；
        只是重新保存、内容没变则保留当前令牌并返回 False——那些令牌比文件里的更新。"""
        mtime = self.cookie_file_mtime()
        cookies = read_cookie_file(self._config.cookie_file)
        if _fingerprint(cookies) == self._cookie_hash:
            self.loaded_mtime = mtime
            logger.info("cookie.txt 被保存但内容没变，继续使用当前令牌")
            return False
        self._load(use_state=False)
        self.save_state()
        logger.info("已重新加载 cookie.txt")
        return True

    def cookie_file_mtime(self) -> float:
        try:
            return os.stat(self._config.cookie_file).st_mtime
        except OSError:
            return 0.0

    def cookie_values(self) -> List[str]:
        return [v for v in self._cookies.values() if v]

    def save_state(self) -> None:
        """原子写入：临时文件 → fsync → rename，权限 0600。"""
        path = str(self._config.state_file)
        tmp = path + ".tmp"
        data = {"origin_jsessionid": self._origin_jsessionid, "cookie_txt_sha256": self._cookie_hash,
                "cookies": self._cookies}
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("保存令牌状态文件失败（%s）：%s；重启后可能需要重新抓 Cookie", path, e)

    # ---- 内部 ----

    def _load(self, use_state: bool) -> None:
        self.loaded_mtime = self.cookie_file_mtime()
        cookies = read_cookie_file(self._config.cookie_file)
        self._origin_jsessionid = cookies["JSESSIONID"]
        self._cookie_hash = _fingerprint(cookies)
        if use_state:
            saved = self._read_state()
            if saved is not None:
                cookies.update(saved)
                logger.info("已复用上次运行保存的滚动令牌")
        self._set_cookies(cookies)

    def _read_state(self) -> Optional[Dict[str, str]]:
        path = self._config.state_file
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning("令牌状态文件 %s 无法读取（%s），改用 cookie.txt 中的值", path, e)
            return None
        if not isinstance(raw, dict):
            logger.warning("令牌状态文件 %s 格式不对，改用 cookie.txt 中的值", path)
            return None
        if "cookie_txt_sha256" in raw:             # 当前格式：cookie.txt 内容完全一致才复用
            same = raw.get("cookie_txt_sha256") == self._cookie_hash
            cookies = raw.get("cookies")
        elif "cookies" in raw:                     # 早期格式：只能按 JSESSIONID 判断
            same = raw.get("origin_jsessionid") == self._origin_jsessionid
            cookies = raw.get("cookies")
        else:                                      # 旧版脚本写的平铺格式
            same = raw.get("JSESSIONID") == self._origin_jsessionid
            cookies = raw
        if not same or not isinstance(cookies, dict):
            logger.info("cookie.txt 已更换，忽略上次保存的令牌")
            return None
        return {str(k): "" if v is None else str(v) for k, v in cookies.items()}

    def _set_cookies(self, cookies: Dict[str, str]) -> None:
        """只保留一份 Cookie：按名字去重后整体写回 jar，避免同名 Cookie 因 domain/path 不同被重复发送。"""
        self._cookies = dict(cookies)
        jar = self._http.cookies
        jar.clear()
        for name, value in self._cookies.items():
            jar.set(name, value, domain=self._host, path="/")

    def _absorb(self, response: requests.Response) -> None:
        updated = dict(self._cookies)
        for c in response.cookies:
            value = "" if c.value is None else c.value
            if updated.get(c.name) != value:
                if c.name in ROLLING_KEYS:
                    logger.debug("令牌已轮换: %s", c.name)
                updated[c.name] = value
        changed = updated != self._cookies
        self._set_cookies(updated)
        if changed:
            self.save_state()


def _fingerprint(cookies: Dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(cookies, sort_keys=True).encode("utf-8")).hexdigest()
