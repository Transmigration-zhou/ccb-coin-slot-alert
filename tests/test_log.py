import logging

import pytest

from coin_alert.log import RedactFilter, setup_logging

SECRET = "330000200001010000"


@pytest.fixture
def logger():
    lg = logging.getLogger("test_redact")
    yield lg
    lg.filters.clear()


def test_redacts_message_and_args(logger, caplog):
    logger.addFilter(RedactFilter([SECRET]))
    logger.warning("请求失败: %s", f"for url: ...CRDT_NO={SECRET}")
    assert SECRET not in caplog.text and "***" in caplog.text


def test_redacts_exception_text(logger, caplog):
    logger.addFilter(RedactFilter([SECRET]))
    try:
        raise RuntimeError(f"boom {SECRET}")
    except RuntimeError:
        logger.exception("出错")
    assert SECRET not in caplog.text and "boom ***" in caplog.text


def test_set_secrets_replaces_old(logger, caplog):
    f = RedactFilter(["oldsecret"])
    logger.addFilter(f)
    f.set_secrets(["newsecret"])
    logger.warning("oldsecret newsecret")
    assert "oldsecret ***" in caplog.text


def test_short_secrets_ignored(logger, caplog):
    logger.addFilter(RedactFilter(["1", "", None]))
    logger.warning("第 1 轮")
    assert "第 1 轮" in caplog.text


def test_setup_logging_creates_dir(tmp_path):
    log_file = tmp_path / "logs" / "m.log"
    f = setup_logging(log_file, verbose=False)
    f.set_secrets([SECRET])
    logging.getLogger("coin_alert.x").info("hello %s", SECRET)
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_file.read_text(encoding="utf-8")
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_coin_alert", False)]:
        root.removeHandler(h)
        h.close()
    assert "hello ***" in text and SECRET not in text
