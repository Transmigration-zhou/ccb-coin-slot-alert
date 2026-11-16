import signal
import threading

import pytest

from main import install_signal_handlers


@pytest.fixture(autouse=True)
def restore():
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def test_second_sigint_raises_keyboard_interrupt():
    stop = threading.Event()
    install_signal_handlers(stop)
    signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
    assert stop.is_set()
    with pytest.raises(KeyboardInterrupt):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)


def test_sighup_stays_ignored_under_nohup():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    install_signal_handlers(threading.Event())
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN


def test_sigterm_sets_stop():
    stop = threading.Event()
    install_signal_handlers(stop)
    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    assert stop.is_set()
