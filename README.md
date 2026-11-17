# 建行纪念币预约余量监控

定时查询建行纪念币预约页（jinianbi.ccb.com）指定区县各网点的余量，有货时弹出 macOS 系统通知并响铃；
会话失效、连续查询失败等异常也会通知。

## 环境

- macOS（通知用 terminal-notifier，响铃用系统自带的 afplay）
- Python 3.9+
- terminal-notifier：`brew install terminal-notifier`。没装时程序照常运行，但只写日志、不弹通知

```bash
pip install -r requirements.txt     # 建议在虚拟环境中安装
```

## 配置

`config.json` 默认放在项目根目录；`cookie.txt`，以及程序生成的 `.cookie_state.json`、`.monitor.lock`、`logs/`，都放在 `config.json` 所在目录。
在项目根目录时它们都已被 `.gitignore` 忽略。程序启动时会把 `config.json` 和 `cookie.txt` 的权限收紧为 600。

### config.json

先复制 `config.example.json` 为 `config.json`，再按下表填写。以 `_` 开头的键会被忽略，可以用来写备注。

| 字段 | 必填 | 说明 |
|---|---|---|
| `crdt_no` | 是 | 预约人身份证号，必须和 Cookie 对应的登录账号一致 |
| `product_id` | 是 | 纪念币批次 ID，每期不同，取自查询请求里的 `PRODUCT_ID` 参数 |
| `city` | 是 | 城市名，写全称（`杭州市`）、简称（`杭州`）或唯一前缀（`大理`）都可以；运行 `python main.py list` 查看支持的城市 |
| `districts` | 否 | 要监控的区县列表，写名称或代码都可以；省略则监控该城市全部区县 |
| `interval_seconds` | 否 | 一轮查询结束后等待多少秒再开始下一轮，默认 10 |
| `jitter_seconds` | 否 | 每轮间隔额外随机增加 0～N 秒，默认 0（关闭） |
| `district_gap_seconds` | 否 | 同一轮内相邻区县之间的随机停顿 `[最小, 最大]` 秒，默认 `[0, 0]` |
| `failure_alert_threshold` | 否 | 某区县连续失败多少轮后通知，默认 3 |
| `log_file` | 否 | 日志路径（相对 `config.json` 所在目录），默认 `logs/monitor.log`，按 5MB×3 滚动；写 `null` 表示只输出到终端 |

### cookie.txt

1. 用浏览器登录 https://jinianbi.ccb.com ，按 F12 打开 Network 面板，勾选 Fetch/XHR。
2. 在页面上查询一次区县，找到 `WCCMainPlatV5?...TXCODE=NYB004` 这条请求。
3. 在它的 Request Headers 里复制 Cookie 整行，粘贴进 `cookie.txt`。开头带不带 `Cookie: ` 都行；以 `#` 开头的行视为注释。

注意：

- **抓完 Cookie 后，不要再在同一个浏览器会话里操作建行页面。** 建行每次响应都会轮换令牌（`tranCCBIBS1` / `tranFAVOR`），浏览器和本程序会互相把对方的令牌作废。
- 运行中的监控在两轮之间的等待期每秒检查一次 `cookie.txt`，内容变了就自动重新加载并立即开始新一轮，不需要重启。
- 程序会把最新令牌保存到 `.cookie_state.json`，重启后接着用；`cookie.txt` 内容一变，就改用新 Cookie。

## 用法

```bash
python main.py                 # 持续监控（默认）
python main.py once [区县]     # 单次查询；区县写名称或代码，须属于配置里的城市；不带参数时查配置里的全部区县
python main.py list [城市]     # 列出支持的城市（按省份）/ 某个城市的区县代码
```

| 选项 | 说明 |
|---|---|
| `-c, --config 路径` | 配置文件路径，默认是 `main.py` 同目录下的 `config.json` |
| `-v, --verbose` | 输出 DEBUG 日志，包括每轮完整的网点列表 |

选项要写在子命令前面，例如 `python main.py -v once 西湖区`。

退出码：`0` 正常；`1` 配置错误、查询失败，或已有进程在运行；`2` 会话失效（仅 `once`，需要重新抓 Cookie），或命令行参数写错。

同一组 Cookie 同一时间只能被一个进程使用，否则多个进程会争抢令牌。监控和 `once` 不能同时运行，后启动的会直接退出。

### 后台运行

```bash
nohup python main.py >/dev/null 2>&1 &
```

用 nohup 启动后，关掉终端也不会退出；用 `kill <PID>` 正常停止，PID 写在 `.monitor.lock` 里。
如果改用 launchd 或 cron 启动，它们的 PATH 很精简，找不到 terminal-notifier，需要在 plist 或 crontab 里把
Homebrew 的 bin 目录加进 PATH（Apple Silicon 是 `/opt/homebrew/bin`，Intel 是 `/usr/local/bin`）。

## 运行机制

- **有货提醒**：某网点首次有货、或余量比上一轮多时，发通知并响铃 60 秒。余量不变或减少都不会重复提醒。
- **会话失效**：服务端返回 `ErrMsg`（如"请重新登录"）或跳转到登录页时，立即结束本轮并通知一次，之后每 600 秒探测一次；更新 `cookie.txt` 后立即恢复。
- **查询失败**：单个区县失败不影响其他区县；连续失败达到 `failure_alert_threshold` 轮时通知一次，恢复后再通知一次。
- **全部失败时退避**：一轮里所有区县都失败（例如断网）时，间隔逐轮翻倍，最长 300 秒。
- **解析异常**：网点数据有解析不了的条目时通知，避免因为页面格式变化而漏报。
- **通知合并**：同一轮产生的多条状态通知合并成一条。
- **日志脱敏**：身份证号和 `cookie.txt` 里的 Cookie 值在日志中替换为 `***`；但加 `-v` 时，日志会记录每次轮换后新令牌的前 8 位。

## 支持的地区

区县代码就是请求参数 `CntyAndDstc_Cd`，即国家行政区划代码（GB/T 2260 县级 6 位）。数据在 `coin_alert/regions.py`，
取自 PRODUCT_ID=201948 那一期的预约页，共 10 个省、107 个城市、673 个区县。

不同批次开放的地区可能不同。换批次后如果查不到，打开新批次的预约页
（`TXCODE=NYB001`），在 F12 控制台执行 `provinceArray`，按结果更新 `regions.py`，
同时更新 `tests/test_regions.py` 里的数据快照校验值。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
