import threading
from pathlib import Path

import pytest
import requests

from coin_alert.config import Config
from coin_alert.monitor import INVALID, NORMAL, Monitor

EMPTY = (200, "var banks=[];")
ERR = (200, "<ErrMsg>会话已超时</ErrMsg>")


def stock(name, n):
    return (200, f"var banks=[{{WDMC:'{name}',JNBZS:'{n}',WDDH:'0571'}}];")


def make_cfg(n_districts=3, **kw):
    districts = (("上城区", "330102"), ("拱墅区", "330105"), ("西湖区", "330106"))[:n_districts]
    p = Path("/nonexistent")
    return Config(crdt_no="330000200001010000", product_id="201947", city="杭州", districts=districts,
                  cookie_file=p, state_file=p, lock_file=p, log_file=None, **kw)


class FakeSession:
    def __init__(self, script=None):
        self.script = script or {}      # code -> list of replies (tuple 或 Exception)，用完后返回 EMPTY
        self.queried = []
        self.mtime = 1.0
        self.loaded_mtime = 1.0
        self.reloads = 0

    def query(self, code):
        self.queried.append(code)
        replies = self.script.get(code, [])
        r = replies.pop(0) if replies else EMPTY
        if isinstance(r, Exception):
            raise r
        return r

    def cookie_file_mtime(self):
        return self.mtime

    def reload_from_cookie_file(self):
        self.reloads += 1
        self.loaded_mtime = self.mtime
        return True

    def cookie_values(self):
        return ["sess1"]


class FakeAlarm:
    def __init__(self):
        self.starts = 0

    def start(self):
        self.starts += 1


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def sleep(self, d):
        self.sleeps.append(d)
        self.now += d
        if self.on_sleep:
            self.on_sleep(self)

    def clock(self):
        return self.now


class Rand:
    def __init__(self):
        self.calls = []

    def __call__(self, a, b):
        self.calls.append((a, b))
        return b


@pytest.fixture
def env():
    class Env:
        pass
    e = Env()
    e.alerts, e.alarm, e.time, e.rand = [], FakeAlarm(), FakeTime(), Rand()

    def build(session, cfg=None):
        e.session = session
        e.monitor = Monitor(cfg or make_cfg(), session, e.alerts.append, e.alarm,
                            sleep=e.time.sleep, clock=e.time.clock, rand=e.rand)
        return e.monitor
    e.build = build
    return e


def test_round_continues_after_http_error(env):
    s = FakeSession({"330102": [(503, "x")], "330105": [requests.Timeout("t")]})
    out = env.build(s).run_round()
    assert s.queried == ["330102", "330105", "330106"]
    assert out.failed == {"上城区", "拱墅区"} and not out.all_failed and not out.session_invalid


def test_round_stops_on_rejected(env):
    m = env.build(FakeSession({"330102": [ERR]}))
    out = m.run_round()
    assert env.session.queried == ["330102"] and out.session_invalid and m.state == INVALID
    assert len(env.alerts) == 1 and "会话可能已失效" in env.alerts[0].title
    assert "会话已超时" in env.alerts[0].message


def test_redirect_is_session_invalid(env):
    m = env.build(FakeSession({"330102": [(302, "")]}))
    assert m.run_round().session_invalid and m.state == INVALID


def test_stock_alert_and_alarm_once(env):
    m = env.build(FakeSession({"330102": [stock("A", 5), stock("A", 5)]}), make_cfg(1))
    m.run_round()
    m.run_round()
    stock_alerts = [a for a in env.alerts if "可用提醒" in a.title]
    assert len(stock_alerts) == 1 and "上城区 A 数量:5" in stock_alerts[0].message
    assert env.alarm.starts == 1


def test_unexpected_exception_does_not_escape(env):
    s = FakeSession({"330102": [KeyError("boom")]})
    out = env.build(s).run_round()
    assert "上城区" in out.failed and len(s.queried) == 3


def test_district_failure_alert_after_threshold(env):
    m = env.build(FakeSession({"330105": [(503, "x")] * 3}))
    for _ in range(3):
        m.run_round()
    fails = [a for a in env.alerts if "连续失败" in a.title]
    assert len(fails) == 1 and "拱墅区" in fails[0].message


def test_parse_errors_do_not_back_off(env):
    m = env.build(FakeSession({"330102": [(200, "var banks=[{WDMC:@@}];")]}), make_cfg(1))
    out = m.run_round()
    assert m.next_delay(out) == 10
    assert len([a for a in env.alerts if "解析异常" in a.title]) == 1


def test_backoff_only_when_all_fail(env):
    m = env.build(FakeSession({c: [(503, "x")] * 10 for c in ("330102", "330105", "330106")}))
    delays = [m.next_delay(m.run_round()) for _ in range(6)]
    assert delays == [20, 40, 80, 160, 300, 300]
    env.session.script = {"330102": [(503, "x")]}
    assert m.next_delay(m.run_round()) == 10


def test_invalid_state_probes_every_600s(env):
    m = env.build(FakeSession({"330102": [ERR]}))
    assert m.next_delay(m.run_round()) == 600


def test_recovery_after_invalid_sends_ok_alert(env):
    m = env.build(FakeSession({"330102": [ERR]}))
    m.run_round()
    m.run_round()
    assert m.state == NORMAL and any("已恢复" in a.title for a in env.alerts)


def test_network_errors_while_invalid_keep_invalid(env):
    t = requests.ConnectionError("x")
    m = env.build(FakeSession({"330102": [ERR, t], "330105": [t], "330106": [t]}))
    m.run_round()
    m.run_round()
    assert m.state == INVALID and not any("已恢复" in a.title for a in env.alerts)


def test_cookie_change_triggers_reload_and_immediate_round(env):
    s = FakeSession({c: [(503, "x")] * 2 for c in ("330102", "330105", "330106")})
    m = env.build(s)
    stop = threading.Event()
    rounds = []
    orig = m.run_round

    def counting_round(stop_arg=None):
        rounds.append(env.time.now)
        if len(rounds) == 3:
            stop.set()
        return orig()
    m.run_round = counting_round

    def on_sleep(t):
        if len(rounds) == 2 and t.now - rounds[-1] >= 3:
            s.mtime = 2.0
    env.time.on_sleep = on_sleep
    m.run_forever(stop)
    assert s.reloads == 1
    assert rounds[2] - rounds[1] < 40          # 第二轮后退避 40 秒，但改了 cookie 后立刻开始第三轮
    assert m._backoff == 10


def test_stop_event_ends_loop(env):
    m = env.build(FakeSession())
    stop = threading.Event()
    env.time.on_sleep = lambda t: stop.set()
    m.run_forever(stop)
    assert env.time.sleeps and all(d <= 1 for d in env.time.sleeps)


def test_default_cadence_has_no_gap_or_jitter(env):
    m = env.build(FakeSession())
    out = m.run_round()
    assert env.time.sleeps == [] and env.rand.calls == []
    assert m.next_delay(out) == 10 and env.rand.calls == []


def test_gap_and_jitter_used_when_configured(env):
    m = env.build(FakeSession(), make_cfg(3, district_gap_seconds=(0.5, 1.5), jitter_seconds=5))
    out = m.run_round()
    assert env.time.sleeps == [1.5, 1.5]
    assert m.next_delay(out) == 15


def test_reload_rearms_session_alert(env):
    s = FakeSession({"330102": [ERR, ERR]})
    m = env.build(s)
    m.run_round()
    s.mtime = 2.0
    stop = threading.Event()
    env.time.on_sleep = lambda t: stop.set()
    m._wait(600, stop)
    m.run_round()
    invalid = [a for a in env.alerts if "会话可能已失效" in a.title]
    assert s.reloads == 1 and len(invalid) == 2


def test_reload_failure_notifies(env):
    from coin_alert.config import ConfigError
    s = FakeSession()

    def bad_reload():
        raise ConfigError("cookie.txt 中没有 JSESSIONID")
    s.reload_from_cookie_file = bad_reload
    m = env.build(s)
    s.mtime = 2.0
    stop = threading.Event()
    env.time.on_sleep = lambda t: stop.set()
    m._wait(10, stop)
    assert any("JSESSIONID" in a.message for a in env.alerts)


def test_exception_outside_round_does_not_kill_loop(env):
    s = FakeSession()
    calls = {"n": 0}

    def flaky_mtime():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return s.mtime
    s.cookie_file_mtime = flaky_mtime
    m = env.build(s)
    stop = threading.Event()
    rounds = []
    orig = m.run_round
    m.run_round = lambda stop_arg=None: (rounds.append(1), stop.set() if len(rounds) == 2 else None, orig())[2]
    m.run_forever(stop)
    assert len(rounds) == 2


def test_stop_checked_between_districts(env):
    stop = threading.Event()
    s = FakeSession({"330102": [stock("A", 5)]})
    orig = s.query

    def query(code):
        stop.set()
        return orig(code)
    s.query = query
    m = env.build(s)
    m.run_round(stop)
    assert s.queried == ["330102"]


def test_health_alerts_batched_per_round(env):
    m = env.build(FakeSession({c: [(503, "x")] * 3 for c in ("330102", "330105", "330106")}))
    for _ in range(3):
        m.run_round()
    assert len(env.alerts) == 1
    assert all(n in env.alerts[0].message for n in ("上城区", "拱墅区", "西湖区"))


def test_alerts_are_logged(env, caplog):
    m = env.build(FakeSession({"330102": [ERR]}))
    m.run_round()
    assert any(r.levelname == "ERROR" and "会话可能已失效" in r.getMessage() for r in caplog.records)
