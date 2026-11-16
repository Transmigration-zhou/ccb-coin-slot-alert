from coin_alert.alerter import HealthTracker, StockTracker, format_stock_alert
from coin_alert.parser import Bank


def b(name, count, phone="0571"):
    return Bank(name=name, count=count, phone=phone)


def test_first_stock_is_new():
    t = StockTracker()
    assert t.update({"滨江区": (b("A", 5),)}, set()) == [("滨江区", b("A", 5))]


def test_same_stock_not_repeated():
    t = StockTracker()
    t.update({"滨江区": (b("A", 5),)}, set())
    assert t.update({"滨江区": (b("A", 5),)}, set()) == []


def test_increase_alerts_decrease_does_not():
    t = StockTracker()
    t.update({"滨江区": (b("A", 5),)}, set())
    assert t.update({"滨江区": (b("A", 8),)}, set()) == [("滨江区", b("A", 8))]
    assert t.update({"滨江区": (b("A", 3),)}, set()) == []


def test_disappear_then_reappear_is_new():
    t = StockTracker()
    t.update({"滨江区": (b("A", 5),)}, set())
    assert t.update({"滨江区": ()}, set()) == []
    assert t.update({"滨江区": (b("A", 5),)}, set()) == [("滨江区", b("A", 5))]


def test_failed_district_keeps_last_stock():
    t = StockTracker()
    t.update({"滨江区": (b("A", 5),), "西湖区": ()}, set())
    assert t.update({"西湖区": ()}, {"滨江区"}) == []
    assert t.update({"滨江区": (b("A", 5),), "西湖区": ()}, set()) == []


def test_new_items_sorted_by_district_then_name():
    t = StockTracker()
    got = t.update({"西湖区": (b("Z", 1), b("B", 1)), "滨江区": (b("A", 1),)}, set())
    assert [(d, x.name) for d, x in got] == sorted([("西湖区", "Z"), ("西湖区", "B"), ("滨江区", "A")])


def test_stock_message_format():
    a = format_stock_alert("杭州", [("滨江区", b("A", 5))])
    assert a.title == "建行「杭州」纪念币可用提醒"
    assert "滨江区 A 数量:5 电话:0571" in a.message
    assert a.sound == "default"


def test_stock_message_truncates():
    items = [("滨江区", b(f"中国建设银行杭州某某支行{i:03d}", 5)) for i in range(100)]
    a = format_stock_alert("杭州", items)
    assert len(a.message) <= 1000 and a.message.endswith("…等 100 个网点")


def test_session_invalid_once_until_ok():
    h = HealthTracker(3)
    first = h.session_invalid("会话已超时")
    assert first is not None and "会话已超时" in first.message and "cookie.txt" in first.message
    assert first.sound == "Basso"
    assert h.session_invalid("again") is None
    assert h.session_ok() is not None
    assert h.session_ok() is None


def test_district_failure_threshold():
    h = HealthTracker(3)
    assert h.district_failed("滨江区", "HTTP 503") is None
    assert h.district_failed("滨江区", "HTTP 503") is None
    a = h.district_failed("滨江区", "HTTP 503")
    assert a is not None and "滨江区" in a.message and "HTTP 503" in a.message
    assert h.district_failed("滨江区", "HTTP 503") is None
    assert h.district_ok("滨江区") is not None


def test_district_ok_without_alert_is_silent():
    h = HealthTracker(3)
    h.district_failed("滨江区", "x")
    h.district_failed("滨江区", "x")
    assert h.district_ok("滨江区") is None
    assert h.district_failed("滨江区", "x") is None   # 计数已清零


def test_parse_errors_once():
    h = HealthTracker(3)
    a = h.parse_errors("滨江区", ("{WDMC:@@}",))
    assert a is not None and "{WDMC:@@}" in a.message and "解析异常" in a.title
    assert h.parse_errors("滨江区", ("{WDMC:@@}",)) is None
    assert h.parse_ok("滨江区") is not None
    assert h.parse_ok("滨江区") is None
