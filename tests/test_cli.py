import json
import os
import subprocess
import sys

from coin_alert.lock import ProcessLock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), *args],
                          capture_output=True, text=True, cwd=ROOT, timeout=30)


def write_setup(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"crdt_no": "330000200001010000", "product_id": "201947",
                               "city": "杭州", "log_file": None}))
    (tmp_path / "cookie.txt").write_text("JSESSIONID=sess1; tranFAVOR=orig")
    return cfg


def test_list_cities():
    r = run("list")
    assert r.returncode == 0 and "杭州" in r.stdout


def test_list_city_districts():
    r = run("list", "杭州")
    assert r.returncode == 0 and "滨江区: 330108" in r.stdout


def test_list_unknown_city():
    r = run("list", "火星")
    assert r.returncode == 1 and "杭州" in r.stderr


def test_missing_config_exit_1(tmp_path):
    r = run("-c", str(tmp_path / "config.json"), "once")
    assert r.returncode == 1 and "config.example.json" in r.stderr


def test_once_refuses_when_locked(tmp_path):
    cfg = write_setup(tmp_path)
    with ProcessLock(tmp_path / ".monitor.lock"):
        r = run("-c", str(cfg), "once", "滨江区")
    assert r.returncode == 1 and "已有进程" in r.stderr


def test_once_unknown_district(tmp_path):
    cfg = write_setup(tmp_path)
    r = run("-c", str(cfg), "once", "不存在区")
    assert r.returncode == 1 and "滨江区" in r.stderr


def test_config_files_tightened_to_0600(tmp_path):
    cfg = write_setup(tmp_path)
    cfg.chmod(0o644)
    run("-c", str(cfg), "once", "不存在区")
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
