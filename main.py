import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import requests
import schedule

from notify import notify as send_notification

# ===== 需要定期更新的凭证 =====
# 更新方法：浏览器登录 https://jinianbi.ccb.com 后按 F12 -> Network -> 勾选 Fetch/XHR,
#           找到 WCCMainPlatV5_2?...TXCODE=NYB004 这条请求, 在 Headers 里复制 Cookie 整行,
#           整段替换下面三引号里的内容即可。
COOKIE_STR = """
"""

# 预约人身份证号，需与登录账号一致
CRDT_NO = ''

# 纪念币批次 ID，每期不同，从上面那条请求的 PRODUCT_ID 参数抄
PRODUCT_ID = '201947'


def parse_cookies(cookie_str):
    """把浏览器复制来的 'k=v; k=v' 字符串解析成 requests 用的字典"""
    cookies = {}
    for item in cookie_str.strip().split(';'):
        item = item.strip()
        if not item or '=' not in item:
            continue
        key, value = item.split('=', 1)
        cookies[key.strip()] = value.strip()
    return cookies


COOKIES = parse_cookies(COOKIE_STR)

# 建行走直连：系统代理是境外出口，会话会因 IP 跳变被风控踢掉
SESSION = requests.Session()
SESSION.trust_env = False

# 当前生效的 cookie，随服务端 Set-Cookie 滚动更新（建行的 tranCCBIBS1/tranFAVOR 每次响应都换新值）
CURRENT_COOKIES = dict(COOKIES)

# 轮换后的令牌落盘，重启时接着用；否则重启会拿 COOKIE_STR 里已被作废的旧令牌
COOKIE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cookie_state.json')


def load_cookie_state():
    """只在 JSESSIONID 一致(同一次登录)时复用磁盘状态；COOKIE_STR 换了新会话则丢弃旧状态"""
    try:
        with open(COOKIE_STATE_FILE) as f:
            saved = json.load(f)
    except (IOError, OSError, ValueError):
        return
    if saved.get('JSESSIONID') and saved.get('JSESSIONID') == COOKIES.get('JSESSIONID'):
        CURRENT_COOKIES.update(saved)
        print(f"已复用磁盘上的滚动令牌 ({COOKIE_STATE_FILE})")
    else:
        print("COOKIE_STR 是新会话，忽略磁盘上的旧令牌")


def save_cookie_state():
    try:
        with open(COOKIE_STATE_FILE, 'w') as f:
            json.dump(CURRENT_COOKIES, f)
    except (IOError, OSError):
        pass


# 这两个是滚动令牌，换新值时打印一次，便于确认会话在正常续期
_ROLLING_KEYS = ('tranCCBIBS1', 'tranFAVOR')

# Cookie 失效只通知一次，成功一次后复位
_cookie_alerted = False

# 定义区域代码字典
DISTRICTS = {
    "拱墅区": "330105",
    "滨江区": "330108",
    "钱塘区": "330114",
    "上城区": "330102",
    "西湖区": "330106",
    "萧山区": "330109",
    "余杭区": "330110"
}


def query_coin_availability(district_code, district_name=""):
    """查询建行网点纪念币可用情况"""
    global _cookie_alerted
    print(
        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始查询{'「' + district_name + '」' if district_name else ''}纪念币...")

    # URL和参数
    url = 'https://jinianbi.ccb.com/tran/WCCMainPlatV5_2'
    params = {
        'CCB_IBSVersion': 'V5',
        'SERVLET_NAME': 'WCCMainPlatV5_2',
        'isAjaxRequest': 'true',
        'TXCODE': 'NYB004',
        'CntyAndDstc_Cd': district_code,
        'PRODUCT_ID': PRODUCT_ID,
        'JNB_DATE_TYPE': '0',
        'CRDT_NO': CRDT_NO
    }

    # 请求头
    headers = {
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Content-Length': '0',
        'Origin': 'https://jinianbi.ccb.com',
        'Pragma': 'no-cache',
        'Referer': 'https://jinianbi.ccb.com/tran/WCCMainPlatV5_2?CCB_IBSVersion=V5&SERVLET_NAME=WCCMainPlatV5_2',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest',
        'sec-ch-ua': '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"macOS"'
    }

    try:
        # 发送POST请求
        response = SESSION.post(url, params=params, headers=headers,
                                cookies=CURRENT_COOKIES, timeout=20)

        # 合并服务端下发的新令牌，下次请求就带最新值
        for c in response.cookies:
            if CURRENT_COOKIES.get(c.name) != c.value:
                CURRENT_COOKIES[c.name] = c.value
                if c.name in _ROLLING_KEYS:
                    print(f"令牌已轮换: {c.name} -> {c.value[:24]}...")
                    save_cookie_state()
        # jar 里别留东西，否则和上面显式传的 cookies 混在一起会发出重复 cookie
        SESSION.cookies.clear()
        response.raise_for_status()  # 检查请求是否成功

        # 提取响应中的数据
        # 使用正则表达式提取JavaScript数组变量
        bank_data_match = re.search(r'var banks=\[(.*?)\];', response.text, re.DOTALL)

        if bank_data_match:
            _cookie_alerted = False
            # 提取匹配到的内容
            bank_data_str = bank_data_match.group(1)

            # 将JavaScript对象格式转换为Python可解析的JSON格式
            # 替换JavaScript对象中的单引号为双引号，并给所有键添加双引号
            bank_data_str = re.sub(r"'", r'"', bank_data_str)
            bank_data_str = re.sub(r'(\w+):', r'"\1":', bank_data_str)

            # 分割每个对象并转换为Python字典
            bank_list = []
            bank_objects = re.findall(r'{(.*?)}', bank_data_str, re.DOTALL)

            for bank_obj in bank_objects:
                try:
                    # 处理每个对象字符串
                    bank_json = "{" + bank_obj + "}"
                    bank_dict = json.loads(bank_json)
                    bank_list.append(bank_dict)
                except json.JSONDecodeError:
                    print(f"无法解析银行对象: {bank_obj}")

            # 打印提取的银行网点信息
            print(f"\n提取到的银行网点信息 ({len(bank_list)} 个):")
            print(bank_list)

            # 显示所有有纪念币的网点
            coins_available = [bank for bank in bank_list if bank.get('JNBZS', '0') != '0']

            if coins_available:
                message = f"{'「' + district_name + '」' if district_name else ''}以下网点有纪念币可用:\n"

                print("\n\n=== 有纪念币的网点 ===")
                for bank in coins_available:
                    bank_info = f"名称: {bank.get('WDMC', '未知')}, 数量: {bank.get('JNBZS', '未知')}, 电话: {bank.get('WDDH', '未知')}"
                    message += bank_info + "\n"
                    print(bank_info)

                # 发送系统通知
                send_notification(
                    message,
                    title=f"建行{'「' + district_name + '」' if district_name else ''}纪念币可用提醒",
                    sound="default"
                )
                play_alarm()
                return True, coins_available
            else:
                print("\n\n没有找到有纪念币的网点")
                return False, []
        else:
            if 'ErrMsg' in response.text:
                err = re.search(r'<ErrMsg>(.*?)</ErrMsg>', response.text)
                err_msg = err.group(1) if err else '未知错误'
                print(f"请求被拒绝: {err_msg}")
                if not _cookie_alerted:
                    _cookie_alerted = True
                    print("!!! COOKIE_EXPIRED !!! 请更新文件顶部的 COOKIE_STR")
                    send_notification(
                        f"{err_msg}\n请重新抓 Cookie 并更新 COOKIE_STR",
                        title="建行纪念币监控已失效",
                        sound="Basso"
                    )
            else:
                print("未能在响应中找到银行网点数据")
                print("\n原始响应内容:")
                print(response.text[:1000])
            return False, []

    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return False, []


def query_all_districts():
    """查询所有区域的纪念币情况"""
    all_available_banks = []
    has_coins = False

    for district_name, district_code in DISTRICTS.items():
        found, available_banks = query_coin_availability(district_code, district_name)
        if found:
            has_coins = True
            for bank in available_banks:
                bank['district'] = district_name
                all_available_banks.append(bank)

    # 如果有区域发现纪念币，输出总结
    if has_coins and all_available_banks:
        print("\n=== 所有有纪念币的网点汇总 ===")
        # 按区域分组
        by_district = {}
        for bank in all_available_banks:
            district = bank.get('district')
            if district not in by_district:
                by_district[district] = []
            by_district[district].append(bank)

        # 输出汇总信息
        for district, banks in by_district.items():
            print(f"\n{district}:")
            for bank in banks:
                print(f"  - {bank.get('WDMC')}: {bank.get('JNBZS')}个")

    return has_coins


LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.monitor.pid')

ALARM_SOUND = '/System/Library/Sounds/Glass.aiff'
ALARM_SECONDS = 60
_alarm_proc = None


def play_alarm():
    """有货时后台循环响铃约 ALARM_SECONDS 秒后自动停;想提前停: pkill afplay"""
    global _alarm_proc
    if _alarm_proc is not None and _alarm_proc.poll() is None:
        return  # 已在响,不叠加
    try:
        cmd = ('end=$(($(date +%s)+' + str(ALARM_SECONDS) + ')); '
                                                            'while [ $(date +%s) -lt $end ]; do afplay "' + ALARM_SOUND + '"; done')
        _alarm_proc = subprocess.Popen(['sh', '-c', cmd])
    except Exception as e:
        print(f"响铃启动失败: {e}")


def acquire_lock():
    """确保只有一个监控进程：多个进程会抢同一份滚动令牌，互相把对方的令牌作废"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # 不发信号，只探测进程是否存活
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # 陈旧的锁文件，可以覆盖
        else:
            print(f"已有监控进程在运行 (PID {old_pid})，本次退出。")
            print(f"要强制接管请先删除 {LOCK_FILE}")
            return False
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def run_scheduled_task():
    """运行定时任务"""
    if not acquire_lock():
        return
    load_cookie_state()
    print(f"开始监控纪念币预约情况... (PID {os.getpid()})")
    print(f"将监控以下区域: {', '.join(DISTRICTS.keys())}")
    print("程序将每隔10秒查询一次所有区域")
    print("如果找到有纪念币的网点，将发送系统通知")
    print("按Ctrl+C可以随时退出程序\n")

    # 立即运行一次
    query_all_districts()

    # 设置定时任务，每10秒查询一次
    schedule.every(10).seconds.do(query_all_districts)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n程序已退出")
    finally:
        release_lock()


if __name__ == "__main__":
    import sys

    # 检查命令行参数
    if len(sys.argv) > 1:
        # 如果有命令行参数
        arg = sys.argv[1]
        if arg == "list":
            # 列出所有可用区域
            print("可用的区域代码:")
            for name, code in DISTRICTS.items():
                print(f"{name}: {code}")
        elif arg in DISTRICTS.values():
            # 查询指定区域代码
            for name, code in DISTRICTS.items():
                if code == arg:
                    district_name = name
                    break
            print(f"单次查询 {district_name} ({arg}) 的纪念币情况")
            query_coin_availability(arg, district_name)
        elif arg in DISTRICTS.keys():
            # 查询指定区域名称
            district_code = DISTRICTS[arg]
            print(f"单次查询 {arg} ({district_code}) 的纪念币情况")
            query_coin_availability(district_code, arg)
        else:
            print(f"未知的区域代码或名称: {arg}")
            print("使用 'list' 参数查看所有可用区域")
    else:
        # 无参数，运行定时监控模式
        run_scheduled_task()
