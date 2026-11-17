import json
import logging
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from coin_alert.config import Config
from coin_alert.session import CcbSession


class FakeServer:
    def __init__(self):
        self.replies = []      # 每项: (status, [(header, value)], body_bytes)
        self.requests = []     # 每项: {"cookie": str, "query": dict}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                server.requests.append({
                    "cookie": self.headers.get("Cookie", ""),
                    "query": parse_qs(urlparse(self.path).query),
                })
                status, headers, body = server.replies.pop(0) if server.replies else (200, [], b"var banks=[];")
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/tran/WCCMainPlatV5"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.httpd.shutdown()


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "cookie.txt").write_text("JSESSIONID=sess1; tranFAVOR=orig; INFO=x")
    return Config(crdt_no="330000200001010000", product_id="201948", city="杭州",
                  districts=(("滨江区", "330108"),), cookie_file=tmp_path / "cookie.txt",
                  state_file=tmp_path / ".cookie_state.json", lock_file=tmp_path / ".monitor.lock",
                  log_file=None)


def test_rolling_cookie_sent_back_next_request(server, cfg):
    server.replies.append((200, [("Set-Cookie", "tranFAVOR=new1; Path=/")], b"var banks=[];"))
    s = CcbSession(cfg, url=server.url)
    s.query("330108")
    s.query("330108")
    second = server.requests[1]["cookie"]
    assert "tranFAVOR=new1" in second and second.count("tranFAVOR=") == 1
    assert "JSESSIONID=sess1" in second


def test_state_file_written_0600_and_reused(server, cfg):
    server.replies.append((200, [("Set-Cookie", "tranFAVOR=new1; Path=/")], b"var banks=[];"))
    CcbSession(cfg, url=server.url).query("330108")
    assert stat.S_IMODE(cfg.state_file.stat().st_mode) == 0o600
    state = json.loads(cfg.state_file.read_text())
    assert state["origin_jsessionid"] == "sess1" and state["cookies"]["tranFAVOR"] == "new1"
    CcbSession(cfg, url=server.url).query("330108")
    assert "tranFAVOR=new1" in server.requests[1]["cookie"]


def test_state_from_other_login_ignored(server, cfg):
    cfg.state_file.write_text(json.dumps({"origin_jsessionid": "other", "cookies": {"tranFAVOR": "stale"}}))
    CcbSession(cfg, url=server.url).query("330108")
    assert "tranFAVOR=orig" in server.requests[0]["cookie"]


def test_legacy_flat_state_supported(server, cfg):
    cfg.state_file.write_text(json.dumps({"JSESSIONID": "sess1", "tranFAVOR": "old"}))
    CcbSession(cfg, url=server.url).query("330108")
    assert "tranFAVOR=old" in server.requests[0]["cookie"]


def test_corrupt_state_warns(server, cfg, caplog):
    cfg.state_file.write_text("{")
    CcbSession(cfg, url=server.url).query("330108")
    assert "状态文件" in caplog.text
    assert "tranFAVOR=orig" in server.requests[0]["cookie"]


def test_redirect_not_followed(server, cfg):
    server.replies.append((302, [("Location", "/login")], b""))
    status, _ = CcbSession(cfg, url=server.url).query("330108")
    assert status == 302 and len(server.requests) == 1


def test_cookie_without_value_does_not_crash(server, cfg):
    server.replies.append((200, [("Set-Cookie", "tranFAVOR; path=/")], b"var banks=[];"))
    s = CcbSession(cfg, url=server.url)
    assert s.query("330108")[0] == 200
    s.query("330108")


def test_query_decodes_chinese_without_charset(server, cfg):
    body = "var banks=[{WDMC:'滨江支行'}];".encode("utf-8")
    server.replies.append((200, [("Content-Type", "text/html")], body))
    _, text = CcbSession(cfg, url=server.url).query("330108")
    assert "滨江支行" in text


def test_reload_discards_state_file(server, cfg):
    cfg.state_file.write_text(json.dumps({"origin_jsessionid": "sess1", "cookies": {"tranFAVOR": "rolled"}}))
    s = CcbSession(cfg, url=server.url)
    cfg.cookie_file.write_text("JSESSIONID=sess1; tranFAVOR=repasted")
    assert s.reload_from_cookie_file() is True
    s.query("330108")
    assert "tranFAVOR=repasted" in server.requests[0]["cookie"]


def test_reload_updates_mtime_and_values(server, cfg):
    s = CcbSession(cfg, url=server.url)
    assert s.loaded_mtime == s.cookie_file_mtime()
    cfg.cookie_file.write_text("JSESSIONID=sess2; tranFAVOR=fresh")
    s.reload_from_cookie_file()
    assert s.loaded_mtime == s.cookie_file_mtime()
    assert "fresh" in s.cookie_values() and "orig" not in s.cookie_values()


def test_crdt_no_in_params(server, cfg):
    CcbSession(cfg, url=server.url).query("330108")
    q = server.requests[0]["query"]
    assert q["CRDT_NO"] == ["330000200001010000"] and q["PRODUCT_ID"] == ["201948"]
    assert q["CntyAndDstc_Cd"] == ["330108"] and q["TXCODE"] == ["NYB004"]
    assert q["SERVLET_NAME"] == ["WCCMainPlatV5"] and q["CCB_IBSVersion"] == ["V5"]
    assert q["isAjaxRequest"] == ["true"] and q["JNB_DATE_TYPE"] == ["0"]


def test_state_ignored_when_cookie_txt_repasted_with_same_jsessionid(server, cfg):
    server.replies.append((200, [("Set-Cookie", "tranFAVOR=rolled; Path=/")], b"var banks=[];"))
    CcbSession(cfg, url=server.url).query("330108")
    cfg.cookie_file.write_text("JSESSIONID=sess1; tranFAVOR=fresh; INFO=x")
    CcbSession(cfg, url=server.url).query("330108")
    assert "tranFAVOR=fresh" in server.requests[1]["cookie"]


def test_reload_same_content_keeps_rotated_tokens(server, cfg):
    server.replies.append((200, [("Set-Cookie", "tranFAVOR=rolled; Path=/")], b"var banks=[];"))
    s = CcbSession(cfg, url=server.url)
    s.query("330108")
    cfg.cookie_file.write_text(cfg.cookie_file.read_text())   # 只是重新保存
    assert s.reload_from_cookie_file() is False
    s.query("330108")
    assert "tranFAVOR=rolled" in server.requests[1]["cookie"]


def test_reload_new_content_returns_true(server, cfg):
    s = CcbSession(cfg, url=server.url)
    cfg.cookie_file.write_text("JSESSIONID=sess2; tranFAVOR=new")
    assert s.reload_from_cookie_file() is True


def test_rotation_debug_log_hides_token_value(server, cfg, caplog):
    server.replies.append((200, [("Set-Cookie", "tranFAVOR=SECRETTOKEN123; Path=/")], b"var banks=[];"))
    with caplog.at_level(logging.DEBUG, logger="coin_alert.session"):
        CcbSession(cfg, url=server.url).query("330108")
    assert "令牌已轮换: tranFAVOR" in caplog.text
    assert "SECRET" not in caplog.text
