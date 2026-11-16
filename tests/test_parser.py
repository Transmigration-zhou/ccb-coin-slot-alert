from pathlib import Path

import pytest

from coin_alert.parser import HttpError, Ok, Redirected, Rejected, Unrecognized, parse_response

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_fixture_ok():
    r = parse_response(200, read("banks_ok.txt"))
    assert isinstance(r, Ok) and r.parse_errors == 0
    assert [b.count for b in r.banks] == [5, 0, 0]
    assert r.banks[0].name == "中国建设银行杭州滨江支行"
    assert r.banks[0].phone == "0571-00000001"
    assert r.banks[2].name == "中国建设银行{测试}分理处"
    assert r.banks[0].raw["YYSJ"] == "09:00-17:00"


@pytest.mark.parametrize("value,expected", [
    ("'09:00-17:00'", "09:00-17:00"),
    ("'http://x'", "http://x"),
    ("'a}b'", "a}b"),
    (r"'O\'Neil'", "O'Neil"),
    ('"双引号"', "双引号"),
])
def test_tricky_string_values(value, expected):
    r = parse_response(200, f"var banks=[{{WDMC:{value},JNBZS:'1',WDDH:''}}];")
    assert isinstance(r, Ok) and r.parse_errors == 0
    assert r.banks[0].count == 1 and r.banks[0].name == expected


def test_space_before_colon_and_trailing_comma():
    r = parse_response(200, "var banks = [ {WDMC : 'a', JNBZS : '5',}, ];")
    assert r.parse_errors == 0 and r.banks[0].count == 5


@pytest.mark.parametrize("raw,expected,errors", [
    ("0", 0, 0), ("'00'", 0, 0), ("'5'", 5, 0), ("5", 5, 0), ("'0.00'", 0, 0), ("' 3 '", 3, 0),
    ("''", 0, 1), ("null", 0, 1), ("'abc'", 0, 1),
])
def test_count_normalization(raw, expected, errors):
    r = parse_response(200, f"var banks=[{{WDMC:'a',JNBZS:{raw}}}];")
    assert r.banks[0].count == expected and r.parse_errors == errors


def test_missing_count_field_is_error():
    r = parse_response(200, "var banks=[{WDMC:'a'}];")
    assert r.banks[0].count == 0 and r.parse_errors == 1


def test_empty_array():
    assert parse_response(200, "var banks=[];") == Ok(banks=())


def test_broken_object_counted_not_dropped_silently():
    r = parse_response(200, "var banks=[{WDMC:'a',JNBZS:'2'},{WDMC:@@}];")
    assert len(r.banks) == 1 and r.parse_errors == 1
    assert "WDMC:@@" in r.error_snippets[0]


def test_unterminated_array_is_unrecognized():
    assert isinstance(parse_response(200, "var banks=[{WDMC:'a'"), Unrecognized)


def test_err_msg():
    assert parse_response(200, read("err_msg.txt")) == Rejected("会话已超时")


def test_login_page():
    r = parse_response(200, read("login_page.html"))
    assert isinstance(r, Unrecognized) and "登录" in r.snippet


def test_redirect():
    assert parse_response(302, "") == Redirected(302)


def test_server_error():
    assert parse_response(503, "x") == HttpError(503)
