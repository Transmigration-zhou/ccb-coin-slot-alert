import json
import stat

import pytest

from coin_alert.config import ConfigError, ensure_private, load_config, parse_cookie_text, read_cookie_file

BASE = {"crdt_no": "330000200001010000", "product_id": "201948", "city": "杭州"}


def write_cfg(tmp_path, data=None, raw=None):
    p = tmp_path / "config.json"
    p.write_text(raw if raw is not None else json.dumps(data if data is not None else BASE), encoding="utf-8")
    return p


def test_minimal_config_uses_defaults_and_all_districts(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert cfg.interval_seconds == 10 and cfg.jitter_seconds == 0
    assert cfg.district_gap_seconds == (0, 0) and cfg.failure_alert_threshold == 3
    assert len(cfg.districts) == 7
    assert cfg.cookie_file == tmp_path / "cookie.txt"
    assert cfg.state_file == tmp_path / ".cookie_state.json"
    assert cfg.lock_file == tmp_path / ".monitor.lock"
    assert cfg.log_file == tmp_path / "logs" / "monitor.log"


def test_districts_subset_resolved(tmp_path):
    cfg = load_config(write_cfg(tmp_path, dict(BASE, districts=["滨江区", "西湖区"])))
    assert cfg.districts == (("滨江区", "330108"), ("西湖区", "330106"))


def test_log_file_null_disables_file_logging(tmp_path):
    assert load_config(write_cfg(tmp_path, dict(BASE, log_file=None))).log_file is None


def test_underscore_keys_ignored_and_overrides_applied(tmp_path):
    cfg = load_config(write_cfg(tmp_path, dict(BASE, _说明="x", interval_seconds=5,
                                               district_gap_seconds=[0.5, 1.5])))
    assert cfg.interval_seconds == 5 and cfg.district_gap_seconds == (0.5, 1.5)


@pytest.mark.parametrize("missing", ["crdt_no", "product_id", "city"])
def test_missing_required_field(tmp_path, missing):
    data = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(write_cfg(tmp_path, data))


def test_unknown_district_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="滨江区"):
        load_config(write_cfg(tmp_path, dict(BASE, districts=["不存在区"])))


def test_missing_config_file_message_mentions_example(tmp_path):
    with pytest.raises(ConfigError, match="config.example.json"):
        load_config(tmp_path / "config.json")


def test_invalid_json(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, raw="{"))


def test_cookie_text_strips_header_prefix_and_crlf():
    assert parse_cookie_text("Cookie: JSESSIONID=a; tranFAVOR=b=c\r\n") == {"JSESSIONID": "a", "tranFAVOR": "b=c"}


def test_cookie_text_ignores_comments_and_blank_lines():
    assert parse_cookie_text("# 注释\n\nJSESSIONID=a;\n x=1 ") == {"JSESSIONID": "a", "x": "1"}


def test_cookie_file_without_jsessionid_is_error(tmp_path):
    p = tmp_path / "cookie.txt"
    p.write_text("a=1")
    with pytest.raises(ConfigError, match="JSESSIONID"):
        read_cookie_file(p)


def test_missing_cookie_file_is_error(tmp_path):
    with pytest.raises(ConfigError, match="cookie.txt"):
        read_cookie_file(tmp_path / "cookie.txt")


def test_ensure_private_tightens_mode(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    p.chmod(0o644)
    assert ensure_private(p) is True and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert ensure_private(p) is False


def test_cookie_file_non_utf8_is_config_error(tmp_path):
    p = tmp_path / "cookie.txt"
    p.write_text("JSESSIONID=a", encoding="utf-16")
    with pytest.raises(ConfigError, match="UTF-8"):
        read_cookie_file(p)


def test_cookie_file_with_bom(tmp_path):
    p = tmp_path / "cookie.txt"
    p.write_text("Cookie: JSESSIONID=a", encoding="utf-8-sig")
    assert read_cookie_file(p) == {"JSESSIONID": "a"}
