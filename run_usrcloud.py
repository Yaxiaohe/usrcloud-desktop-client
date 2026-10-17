# -*- coding: utf-8 -*-
"""
有人云巡站客户端（数据来源：有人云）
================================================================
用途：登录有人云，逐台获取设备"实时概况"数据，按手机版式渲染并截图，
      运行结束后自动清理上一次的批次，并打包成 zip。

特点：不需要在网页上登录（有人云 Web 登录需要通行证，此账号没有），
      走 App 接口拿 token。
      v2 起：数据直接走平台接口三步直调（元信息/变量点/最新值），
      每台约 1-2 秒（原浏览器方式约 18 秒/台），并支持 4 路并发；
      如接口直调异常，可加 --browser-mode 退回浏览器控制台取数保底。

--------------------------------------------------------------
【电脑上首次使用，三步】

1) 装 Python 3.8+（Windows 到 python.org 下载，安装时勾选
   "Add Python to PATH"；macOS 一般自带）

2) 打开终端 /命令提示符，安装依赖：
       pip install playwright
       playwright install chromium

3) 运行：
       python run_usrcloud.py

   跑完会在脚本同目录生成：
       20260928_164919_截图/          ← 截图 PNG + 巡站报告.json（按账号分子目录）
       有人云巡站截图_20260928_164919.zip   ← 打包好的压缩包

--------------------------------------------------------------
【常用参数】

   python run_usrcloud.py                     # 正常全量运行（API 直调，快速）
   python run_usrcloud.py --workers 6         # 加并发（API 模式可调大）
   python run_usrcloud.py --clean             # 跑完顺手删掉更早的批次（默认不删）
   python run_usrcloud.py --clean --keep 1    # 只保留最近的 1 个历史批次
   python run_usrcloud.py --no-zip            # 不打包 zip
   python run_usrcloud.py --limit 3           # 只跑前 3 台（测试用）
   python run_usrcloud.py --browser-mode      # 退回浏览器取数（慢，保底用）
   python run_usrcloud.py --no-headless       # 显示浏览器窗口（排查用）

【账号密码】
   同目录 accounts.csv 表格维护（首次运行自动生成模板）。
================================================================
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# 运行/暂停开关：set=运行，clear=暂停（每台设备开始前检查）。
# 仅图形界面使用；命令行模式始终保持 set，行为与以前完全一致。
_PAUSE_EVENT = threading.Event()
_PAUSE_EVENT.set()

# playwright 仅浏览器保底模式（--browser-mode）需要；默认 API 模式 + 本地 Pillow
# 绘图渲染截图，完全不依赖浏览器（免安装版/绿色版即无 playwright）。
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    sync_playwright = None
    HAS_PLAYWRIGHT = False


# ============ 配置区（按需修改） ============
# 账号密码不再写在本文件里，改为同目录下的 accounts.csv 表格维护。
# 表格格式（可用 Excel / 记事本 / WPS 直接编辑，UTF-8 编码）：
#
#   名称,账号,密码,平台ID,启用
#   示例账号,请填手机号,请填密码,W3rfV,是
#   备用账号,请填手机号,请填密码,W3rfV,否
#
#   名称   —— 随便起，方便识别（会用于截图目录命名）
#   账号   —— 登录手机号
#   密码   —— 明文即可，程序内部自动转 md5
#   平台ID —— 一般填 W3rfV，不要乱改
#   启用   —— 填"是"或"Y"参与运行，填"否"跳过
#
# 多账号用法：
#   默认程序会让你选一个账号；加 --all-accounts 则所有启用账号依次跑一遍。
#   自带模板见 accounts.example.csv，首次运行若没有 accounts.csv 会自动生成一份。

ACCOUNTS_FILE = 'accounts.csv'

WORKERS = 4                    # API 直调模式的并发数（token 为 JWT，多会话共存，可放心并发）
EXTRA_WAIT_SEC = 1.5           # （浏览器模式）表格出现后的额外等待
KEEP_LAST = 0                  # 用 --clean 时，除本次外保留的历史批次数（0=只留本次）
NO_AUTO_CLEAN_NOTE = ('[清理] 往期批次已保留（不再自动删除）。'
                      '如需删除，请在界面点「清理往期」或运行加 --clean。')

MOBILE_W = 390                 # 手机版式宽度
MOBILE_DPR = 3.0               # 像素密度，3 倍与手机截图观感一致

STALE_MINUTES = 15             # 数据超过多少分钟未更新即判为离线（平台会保留旧值，不可只看有无数值）

MAX_CARDS = 10                 # 每张截图最多渲染的变量个数（与旧版浏览器每页 10 行一致；
                               # 状态判定仍用全部变量，不受影响）

STATUS_TAG = {
    'online':  '✓在线',
    'stale':   '○离线(数据陈旧)',
    'offline': '○离线(无数据)',
    'blank':   '⚠采集异常',
}
# ===========================================

LOGIN_URL = 'https://apimp.usr.cn/usrCloud/user/login'
DEVS_URL = 'https://apimp.usr.cn/usrCloud/cusdevice/getCusdevices'
INJECT_URL = 'https://mp.usr.cn/'
# API 直调三件套：设备元信息 / 变量点定义 / 变量最新值（数据与页面同源，0.3s 左右拿全）
INFO_URL = 'https://apimp.usr.cn/usrCloud/cusdevice/getCusdeviceInfo'
POINTS_URL = 'https://apimp.usr.cn/usrCloud/cusdevice/getDataPointInfoForCusdeviceNo'
LAST_URL = 'https://history.usr.cn:7002/history/cusdevice/lastDataPoint'

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, '登录缓存')                  # token 缓存统一收纳在此子文件夹
TOKEN_TMPL = os.path.join(CACHE_DIR, 'app_login_{}.json')        # 按 名称_手机号 分开缓存（同号不同租户也不冲突）


# ---------------- 账号表格读取 ----------------

def safe_name(name):
    return re.sub(r'[？（）)():：\s]+', '', name) or 'unnamed'


def accounts_path():
    return os.path.join(SCRIPT_DIR, ACCOUNTS_FILE)


def ensure_accounts_file():
    """没有表格就生成一份模板，并给出提示。
    优先复用旁边的 accounts.example.csv（交付包自带、账号已填好），直接改名即用。"""
    p = accounts_path()
    if os.path.exists(p):
        return p
    example_src = os.path.join(SCRIPT_DIR, 'accounts.example.csv')
    if os.path.exists(example_src):
        try:
            shutil.copy(example_src, p)
            print('已从 accounts.example.csv 复制生成: {}'.format(p))
            return p
        except Exception:
            pass
    example = (
        '名称,账号,密码,平台ID,启用,站点过滤\n'
        '# 下面是示例，请改成真实账号；填"否"的行会被跳过\n'
        '# 站点过滤：对截图/数据监控/历史记录全部生效，不需要的设备不跑；留空=该账号全部设备\n'
        '示例账号,请填手机号,请填密码,W3rfV,是,\n'
    )
    open(p, 'w', encoding='utf-8-sig').write(example)
    print('已生成账号表格模板: {}'.format(p))
    print('请编辑填入账号密码后重新运行（用 Excel / WPS / 记事本均可）。')
    return None


def load_accounts():
    """读取 accounts.csv，返回启用中的账号列表"""
    import csv
    p = ensure_accounts_file()
    if not p:
        sys.exit(1)
    accounts = []
    with open(p, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):   # 第 1 行是表头
            # 跳过空行和注释行
            name = (row.get('名称') or '').strip()
            acc = (row.get('账号') or '').strip()
            if not name or name.startswith('#') or not acc or acc.startswith('#'):
                continue
            pwd = (row.get('密码') or '').strip()
            plat = (row.get('平台ID') or '').strip() or 'W3rfV'
            enabled = (row.get('启用') or '是').strip().lower()
            if enabled in ('否', 'n', 'no', '0', 'false', ''):
                continue
            if not pwd:
                print('[警告] 第 {} 行 {} 缺少密码，已跳过'.format(i, name))
                continue
            kws_raw = (row.get('站点过滤') or '').strip()
            kws = [k for k in re.split(r'[、，,;；\s]+', kws_raw) if k]
            accounts.append({'name': name, 'account': acc, 'password': pwd,
                             'platform_id': plat, 'station_kw': kws})
    if not accounts:
        print('账号表格里没有可用账号（请检查"启用"列是否填了"是"、密码是否为空）。')
        sys.exit(1)
    return accounts


# ---------------- 有人云 API ----------------

def api_json(url, payload, tok=None, timeout=30):
    headers = {'Content-Type': 'application/json'}
    if tok:
        headers['token'] = tok
        headers['X-Access-Token'] = tok
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_cached_token(cfg):
    """token 有效期约 2h，1.5h 内的缓存直接复用（按账号分别缓存）"""
    tf = TOKEN_TMPL.format(safe_name(cfg['name'] + '_' + cfg['account']))
    try:
        d = json.load(open(tf, encoding='utf-8'))
        ts = d.get('_saved_at')
        if ts and datetime.now() - datetime.fromisoformat(ts) < timedelta(hours=1.5):
            return d['data']['token'], d['data']['uid']
    except Exception:
        pass
    return None, None


def migrate_old_tokens():
    """旧版把 token 缓存散落在脚本目录根下；升级后统一搬进 登录缓存/ 子文件夹。
    搬移即可复用，无需重新登录。"""
    if not os.path.isdir(SCRIPT_DIR):
        return
    old_files = [f for f in os.listdir(SCRIPT_DIR)
                 if f.startswith('app_login_') and f.endswith('.json')]
    if not old_files:
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    moved = 0
    for f in old_files:
        src = os.path.join(SCRIPT_DIR, f)
        dst = os.path.join(CACHE_DIR, f)
        try:
            if not os.path.exists(dst):
                shutil.move(src, dst)
            else:
                os.remove(src)      # 新位置已有同名缓存，旧文件直接清掉
            moved += 1
        except Exception:
            pass
    if moved:
        print('[整理] 已把 {} 个登录缓存文件归纳到 登录缓存/ 文件夹'.format(moved))


def ensure_login(cfg):
    tok, uid = load_cached_token(cfg)
    if tok:
        print('[登录] {} 使用缓存 token（1.5h 内）'.format(cfg['name']))
        return tok, uid
    pwd_md5 = hashlib.md5(cfg['password'].encode()).hexdigest()
    r = api_json(LOGIN_URL, {'account': cfg['account'], 'password': pwd_md5})
    if r.get('status') != 0:
        raise RuntimeError('登录失败: {}'.format(r))
    r['_saved_at'] = datetime.now().isoformat(timespec='seconds')
    r['_account'] = cfg['account']
    r['_name'] = cfg['name']
    os.makedirs(CACHE_DIR, exist_ok=True)
    tf = TOKEN_TMPL.format(safe_name(cfg['name'] + '_' + cfg['account']))
    json.dump(r, open(tf, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('[登录] {} 登录成功 uid={}'.format(cfg['name'], r['data']['uid']))
    return r['data']['token'], r['data']['uid']


def get_devices(tok, uid):
    r = api_json(DEVS_URL, {'pageNum': 1, 'pageSize': 200}, tok)
    if r.get('status') != 0:
        raise RuntimeError('设备列表失败: {}'.format(r))
    lst = r['data']['cusdeviceResponseDTO']
    return [{'no': d['cusdeviceNo'], 'name': d['cusdeviceName']} for d in lst]


def fetch_device_data_api(tok, dev_no):
    """【API 直调】绕过浏览器控制台，三步拿全数据（约 0.3s/台）：
      1) getCusdeviceInfo             -> 设备元信息（组织/模板/网关/地址/在线标志）
      2) getDataPointInfoForCusdeviceNo -> 变量点定义（名称/单位/小数位）
      3) lastDataPoint                -> 每个变量的最新值 + 上报时间（毫秒时间戳）
    返回结构与页面提取版 extract_device_data 完全兼容：
      rows 每行 [变量ID, 变量名称, 类型, 更新时间, 当前值, 操作]
    注意：lastDataPoint 用的是 dataPointRelId（页面抓包实证），不是 dataPointId。
    """
    info = api_json(INFO_URL, {'cusdeviceNo': dev_no}, tok).get('data') or {}
    pts = api_json(POINTS_URL, {
        'dataPointType': '', 'dataPointName': '', 'dataPointIdList': [], 'slaveId': '',
        'cusdeviceNo': dev_no, 'getOperable': 0, 'getTimestampDatas': 1,
        'getLocationDatas': 1, 'getStringDatas': 0, 'getBitDatas': 0,
        'pageNo': 1, 'pageSize': 500}, tok)
    plst = (pts.get('data') or {}).get('cusdeviceDataPointList') or []

    last_by_rel = {}
    if plst:
        dps = [{'cusdeviceNo': dev_no, 'dataPointId': p.get('dataPointRelId')}
               for p in plst if p.get('dataPointRelId') is not None]
        if dps:
            last = api_json(LAST_URL, {'dataPoints': dps}, tok)
            for r in (last.get('data') or {}).get('list') or []:
                last_by_rel[r.get('dataPointId')] = r

    rows = []
    for p in plst:
        unit = (p.get('unit') or '').strip()
        r = last_by_rel.get(p.get('dataPointRelId'))
        tm = '-'
        cur = '-'
        if r:
            ts = r.get('time')
            if ts:
                try:
                    tm = datetime.fromtimestamp(ts / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, OSError, OverflowError):
                    tm = '-'
            val = str(r.get('value') if r.get('value') is not None else '').strip()
            if val and val != '-':
                cur = '{} {}'.format(val, unit).strip() if unit else val
        rows.append([str(p.get('dataPointId') or ''), p.get('name') or '', '',
                     tm, cur, ''])
    return {'rows': rows,
            'org': info.get('projectName') or '--',
            'tpl': info.get('templateName') or '--',
            'gw': info.get('deviceName') or '--',
            'addr': info.get('address') or '--',
            'platform_online': info.get('onlineOffline')}


# ---------------- 命名 / 清理 / 打包 ----------------

def find_previous_batches(base_dir, current_dir, keep=KEEP_LAST):
    """匹配历史批次目录（形如 20260928_181521_截图，账号在批次内的子目录里）"""
    pat = re.compile(r'^\d{8}_\d{6}_截图$')
    batches = [e for e in os.listdir(base_dir)
               if pat.match(e) and os.path.isdir(os.path.join(base_dir, e))]
    batches.sort(reverse=True)
    cur = os.path.basename(current_dir) if current_dir else None
    batches = [b for b in batches if b != cur]
    return [os.path.join(base_dir, b) for b in batches[keep:]]


def count_png(d):
    """递归统计目录下的 PNG 数量（支持 批次/账号/ 两层结构）"""
    n = 0
    try:
        for root, _dirs, files in os.walk(d):
            n += len([f for f in files if f.lower().endswith('.png')])
    except Exception:
        pass
    return n


LAST_RM_ERROR = ''      # 最近一次删除的真实异常文本（诊断用）
LAST_RM_MODE = 'ok'     # 'ok'=彻底删除 / 'renamed'=OneDrive 占用，改名转后台清理
PURGE_INFLIGHT = set()  # 正在后台清理的残壳路径（防重复起线程）


def _purge_tree(d):
    """尽力删除整棵树：文件逐个删、目录自底向上 rmdir，全部删净返回 True。
    关键：不能用 shutil.rmtree——它碰到删不掉的目录会当场中止，后面的子目录
    根本轮不到删（OneDrive 锁目录时表现为"文件一个没少"）。OneDrive 锁定期间
    文件本身可以删（实测验证），只有目录 rmdir 报 WinError 5，所以要逐文件容忍失败。"""
    global LAST_RM_ERROR
    ok = True
    try:
        for root, ds, fs in os.walk(d, topdown=False):
            for f in fs:
                p = os.path.join(root, f)
                try:
                    os.chmod(p, stat.S_IWRITE)
                except Exception:
                    pass
                try:
                    os.remove(p)
                except Exception as e:
                    ok = False
                    LAST_RM_ERROR = '{}: {}'.format(type(e).__name__, str(e)[:120])
            for sub in ds:
                try:
                    os.rmdir(os.path.join(root, sub))
                except Exception as e:
                    ok = False
                    LAST_RM_ERROR = '{}: {}'.format(type(e).__name__, str(e)[:120])
        os.rmdir(d)
    except Exception as e:
        ok = False
        LAST_RM_ERROR = '{}: {}'.format(type(e).__name__, str(e)[:120])
    return ok


def purge_background(d, minutes=30, interval=20):
    """后台长周期清理残壳目录：OneDrive 释放句柄需要几分钟到更久，
    每 interval 秒试一轮，最多挂 minutes 分钟。同一路径不重复起线程。"""
    d = os.path.abspath(d)
    if d in PURGE_INFLIGHT:
        return
    PURGE_INFLIGHT.add(d)

    def _run():
        deadline = time.time() + minutes * 60
        try:
            while time.time() < deadline:
                try:
                    if _purge_tree(d):
                        return
                except Exception:
                    pass
                time.sleep(interval)
        finally:
            PURGE_INFLIGHT.discard(d)

    threading.Thread(target=_run, daemon=True).start()


def _rmtree_retry(d, attempts=8, wait=1.0):
    """删除目录。先常规 rmtree 重试（未被占用时一轮即成）；OneDrive 锁目录时
    （文件能删、rmdir 报 WinError 5）降级为：逐文件清空 → 改名挪出批次列表
    （界面立即看不到）→ 后台线程等 OneDrive 放手后清掉空壳（最多 30 分钟）。"""
    global LAST_RM_ERROR, LAST_RM_MODE
    LAST_RM_ERROR = ''
    LAST_RM_MODE = 'ok'
    for i in range(attempts):
        try:
            for root, _ds, fs in os.walk(d):
                for f in fs:
                    try:
                        os.chmod(os.path.join(root, f), stat.S_IWRITE)
                    except Exception:
                        pass
            shutil.rmtree(d)
            return True
        except Exception as e:
            LAST_RM_ERROR = '{}: {}'.format(type(e).__name__, str(e)[:200])
            if i < attempts - 1:
                time.sleep(wait)
    if not os.path.isdir(d):
        return True
    # OneDrive 占用：逐文件清空（文件当前可删，删一个少一个），只剩空目录骨架
    _purge_tree(d)
    if not os.path.isdir(d):
        return True
    # 顶层目录仍被锁：改名挪出 *_截图 命名规则，后台继续清空壳
    try:
        parent = os.path.dirname(os.path.abspath(d))
        trash = os.path.join(parent, '_del_' + os.path.basename(d) + '_' + time.strftime('%H%M%S'))
        os.rename(d, trash)
        LAST_RM_ERROR = ''
        LAST_RM_MODE = 'renamed'
        purge_background(trash)
        return True
    except Exception as e:
        LAST_RM_ERROR = 'rename fallback: {}: {}'.format(type(e).__name__, str(e)[:150])
        return False


def cleanup_old_batches(base_dir, current_dir, new_ok, do_clean=True):
    """清理往期批次。**不再由程序自动调用**——只在显式传 --clean 时走这里；
    界面上的「清理往期」走的是 webui_server 的 _batchdel（可挑选具体批次）。
    本次至少成功 1 个才动手，防全废误删。"""
    if not do_clean:
        print('\n[清理] 已按参数跳过清理。')
        return
    targets = find_previous_batches(base_dir, current_dir)
    if not targets:
        print('\n[清理] 没有需要清理的历史批次。')
        return
    if new_ok <= 0:
        print('\n[清理] 跳过：本次成功 0 个，保留旧数据不动。')
        return
    print('\n[清理] 本次执行结束，清理上一次的结果：')
    for d in targets:
        cnt = count_png(d)
        if _rmtree_retry(d):
            print('       已删除 {} （{} 张）'.format(os.path.basename(d), cnt))
        else:
            print('       删除失败 {} （{} 张）:: 文件被占用（OneDrive 可能正在同步），'
                  '下次运行会自动重试'.format(os.path.basename(d), cnt))


def make_zip(src_dir, zip_path):
    """把截图目录打包成 zip（只收 png/json）"""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        base = os.path.basename(src_dir.rstrip('/\\'))
        for root, _dirs, files in os.walk(src_dir):
            for f in files:
                if f.lower().endswith(('.png', '.json')):
                    full = os.path.join(root, f)
                    rel = os.path.join(base, os.path.relpath(full, src_dir))
                    z.write(full, rel)
    return zip_path


# ---------------- 页面处理 / 渲染 ----------------

def tidy_page(pg):
    """关掉遮挡内容的浮层：弹窗、AI 客服气泡"""
    for _ in range(3):
        vis = pg.locator('.el-dialog__headerbtn:visible, .el-message-box__headerbtn:visible')
        if vis.count() == 0:
            break
        try:
            vis.first.click(timeout=1500)
            pg.wait_for_timeout(400)
        except Exception:
            break
    try:
        pg.evaluate("""() => {
            const all = [...document.querySelectorAll('div')];
            for (const el of all) {
                const t = el.innerText || '';
                if (t.includes('快速了解有人云') && el.children.length < 8) {
                    el.style.display = 'none'; break;
                }
            }
        }""")
    except Exception:
        pass


def extract_device_data(pg):
    """从设备详情页提取：设备信息 + 变量概况表"""
    return pg.evaluate("""() => {
        const rows = [...document.querySelectorAll('.el-table__row')].map(tr =>
            [...tr.querySelectorAll('td')].map(td => td.innerText.trim()));
        const txt = document.body.innerText;
        const pick = (k) => {
            const m = txt.match(new RegExp(k + '[：:]\\\\s*([^\\\\n]+)'));
            return m ? m[1].trim() : '--';
        };
        return { rows: rows, org: pick('所属组织'), tpl: pick('设备模板'),
                 gw: pick('网关'), addr: pick('设备地址') };
    }""")


MOBILE_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; -webkit-font-smoothing:antialiased; }}
  body {{ font-family:"PingFang SC","Microsoft YaHei",sans-serif; background:#f2f4f8; }}
  .nav {{ display:flex; align-items:center; height:52px; background:#fff;
         padding:0 14px; border-bottom:1px solid #ececec; }}
  .nav .back {{ font-size:22px; color:#222; margin-right:10px; }}
  .nav .title {{ font-size:17px; font-weight:600; color:#1a1a1a; flex:1;
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .nav .badge {{ font-size:11px; font-weight:600; padding:4px 9px; border-radius:11px;
                 margin-left:8px; white-space:nowrap; }}
  .badge.on {{ color:#0f7b3d; background:#e3f7ec; }}
  .badge.off {{ color:#8a6d3b; background:#fdf3e0; }}
  .tabs {{ display:flex; background:#fff; padding:6px 10px 12px; gap:8px; }}
  .tab {{ flex:1; text-align:center; padding:9px 0; font-size:15px; color:#333;
          background:#eef1f6; border-radius:6px; }}
  .tab.on {{ background:#1f4bb8; color:#fff; font-weight:600; }}
  .meta {{ margin:10px 12px 0; padding:12px 14px; background:#fff; border-radius:10px;
           font-size:12px; color:#666; line-height:1.9; }}
  .meta b {{ color:#1a1a1a; font-size:13px; }}
  .meta .no {{ color:#8a97ad; font-size:11px; margin-left:6px; }}
  .note {{ margin:10px 12px 0; padding:9px 13px; border-radius:8px; font-size:12px; }}
  .note.ok {{ color:#0f7b3d; background:#e9f8ef; }}
  .note.warn {{ color:#9a6a12; background:#fdf4e3; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; padding:10px 12px 16px;
           min-height:581px; }}
  /* 显式声明背景色与合成层：Chromium 在整页截图重光栅化时，
     依赖默认样式的 grid 子项可能落到独立层被渲成深灰（看着像被点击选中）。 */
  .card {{ background:#fff; border-radius:10px; padding:13px 12px 11px;
           transform:translateZ(0); will-change:transform; }}
  .card .nm {{ font-size:13px; color:#333; display:flex; align-items:center;
               justify-content:space-between; }}
  .card .nm::after {{ content:"›"; color:#b9c2d0; font-size:15px; }}
  .card .val {{ font-size:24px; font-weight:700; color:#1a1a1a; margin:8px 0 6px; }}
  .card .val small {{ font-size:13px; font-weight:600; color:#444; margin-left:2px; }}
  .card .tm {{ font-size:11px; color:#9aa4b2; }}
</style></head><body>
  <div class="nav"><span class="back">‹</span><span class="title">{title}</span>
    <span class="badge {badge_cls}">{badge_txt}</span></div>
  <div class="tabs">
    <div class="tab on">实时概况</div><div class="tab">设备组态</div>
    <div class="tab">报警记录</div><div class="tab">定位轨迹</div>
  </div>
  <div class="meta"><b>{title}</b><span class="no">{no}</span><br>
    所属组织：{org}　设备模板：{tpl}<br>网关：{gw}　设备地址：{addr}</div>
  <div class="note {note_cls}">{note}</div>
  <div class="grid">{cards}</div>
</body></html>"""

CARD_TMPL = """<div class="card">
  <div class="nm">{name}</div>
  <div class="val">{val}<small>{unit}</small></div>
  <div class="tm">{time}</div>
</div>"""


def judge_status(rows, now=None):
    """判定设备在线状态（关键：不能只看"有没有数值"，必须看数据时间是否新鲜）

    平台只保留"最后一次上报值"，设备断线后旧值不会清空，
    因此出现数值很新 -> 在线；数值是陈旧的（如几小时/几天前）-> 实际已离线。

    'online'   数据时间在阈值内 -> 在线
    'stale'    有数值但数据时间过旧 -> 离线（显示陈旧数据）
    'offline'  当前值为 '-'，平台明确标记无数据 -> 离线
    'blank'    变量表未渲染 -> 采集异常
    """
    if not rows:
        return 'blank'
    now = now or datetime.now()
    times, has_val = [], False
    for r in rows:
        if len(r) < 5:
            continue
        tm, cur = r[3].strip(), r[4].strip()
        if re.search(r'\d+(?:\.\d+)?\s*(℃|kpa|bar)', cur):
            has_val = True
        if tm and tm != '-':
            try:
                times.append(datetime.strptime(tm, '%Y-%m-%d %H:%M:%S'))
            except ValueError:
                pass
    if not has_val or not times:
        return 'offline'
    newest = max(times)
    # 允许一定时钟偏差，负数视为刚上报
    age_min = (now - newest).total_seconds() / 60.0
    return 'online' if age_min <= STALE_MINUTES else 'stale'


def data_age_minutes(rows, now=None):
    """返回最新数据距今多少分钟（无有效时间则 None）"""
    now = now or datetime.now()
    times = []
    for r in rows:
        if len(r) >= 4 and r[3].strip() and r[3].strip() != '-':
            try:
                times.append(datetime.strptime(r[3].strip(), '%Y-%m-%d %H:%M:%S'))
            except ValueError:
                pass
    if not times:
        return None
    return (now - max(times)).total_seconds() / 60.0


def fmt_age(minutes):
    """把分钟数转成易读的"距今"文本"""
    if minutes is None:
        return '无数据时间'
    if minutes < 1:
        return '刚刚'
    if minutes < 60:
        return '{:.0f} 分钟前'.format(minutes)
    if minutes < 60 * 24:
        return '{:.1f} 小时前'.format(minutes / 60)
    return '{:.1f} 天前'.format(minutes / 60 / 24)


_PIL_FONT_CACHE = {}

def _pil_font(size_logical):
    """按平台挑选中文字体（微软雅黑/苹方/Noto），返回 (regular, bold)。size 为逻辑 px。"""
    from PIL import ImageFont
    S = 3   # 与 MOBILE_DPR 一致
    key = size_logical
    if key in _PIL_FONT_CACHE:
        return _PIL_FONT_CACHE[key]
    reg_cands = [r'C:\Windows\Fonts\msyh.ttc',
                 '/System/Library/Fonts/PingFang.ttc',
                 '/System/Library/Fonts/Hiragino Sans GB.ttc',
                 '/System/Library/Fonts/STHeiti Medium.ttc',
                 '/System/Library/Fonts/Supplemental/Songti.ttc',
                 '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
                 '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                 '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc']
    bold_cands = [r'C:\Windows\Fonts\msyhbd.ttc',
                  '/System/Library/Fonts/PingFang.ttc',
                  '/System/Library/Fonts/Hiragino Sans GB.ttc',
                  '/System/Library/Fonts/STHeiti Medium.ttc',
                  '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc']
    reg = bold = None
    for c in reg_cands:
        if os.path.exists(c):
            try:
                reg = ImageFont.truetype(c, size_logical * S)
                break
            except Exception:
                continue
    for c in bold_cands:
        if os.path.exists(c):
            try:
                # 苹方是字体集：遍历 face 找 Medium/Semibold/Bold，找不到就复用 regular
                for idx in range(6):
                    try:
                        f = ImageFont.truetype(c, size_logical * S, index=idx)
                        nm = ' '.join(f.getname())
                        if any(w in nm for w in ('Medium', 'Semibold', 'Bold', 'W6')):
                            bold = f
                            break
                        if idx == 0 and 'Regular' in nm:
                            reg = reg or f
                    except Exception:
                        break
                bold = bold or reg
                break
            except Exception:
                continue
    reg = reg or ImageFont.load_default()
    bold = bold or reg
    _PIL_FONT_CACHE[key] = (reg, bold)
    return reg, bold


def _pil_font_sym(size_logical):
    """符号兜底字体：‹ › 等箭头字形在部分中文字体（冬青黑体等）中缺失，用含全字形的字体兜底。"""
    from PIL import ImageFont
    S = 3
    key = ('sym', size_logical)
    if key in _PIL_FONT_CACHE:
        return _PIL_FONT_CACHE[key]
    f = None
    for c in [r'C:\Windows\Fonts\segoeui.ttf',
              '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
        if os.path.exists(c):
            try:
                f = ImageFont.truetype(c, size_logical * S)
                break
            except Exception:
                continue
    f = f or ImageFont.load_default()
    _PIL_FONT_CACHE[key] = f
    return f


def _pil_ellipsis(draw, s, font, max_w):
    if draw.textlength(s, font=font) <= max_w:
        return s
    while s and draw.textlength(s + '…', font=font) > max_w:
        s = s[:-1]
    return s + '…'


def _pill_text(draw, xy, text, size, bold=False, color='#1a1a1a', anchor='ls'):
    f = _pil_font(size)[1 if bold else 0]
    if any(ch in text for ch in '‹›'):
        f = _pil_font_sym(size)
    draw.text(xy, text, font=f, fill=color, anchor=anchor)
    return f


def render_mobile_cards_pil(dev, data, out_path, status):
    """无浏览器环境（免安装版）下的截图渲染：Pillow 本地绘制，
    版式与浏览器版 render_mobile_cards 完全一致（1170px 宽 = 390 逻辑 × 3 倍）。"""
    from PIL import Image, ImageDraw
    S, W = 3, 390 * 3
    f = lambda size: _pil_font(size)[0]
    fb = lambda size: _pil_font(size)[1]

    def measure(s, size, bold=False):
        return ImageDraw.Draw(Image.new('RGB', (10, 10))).textlength(
            s, font=fb(size) if bold else f(size))

    def rrect(d, box, r, fill):
        d.rounded_rectangle(box, radius=r, fill=fill)

    # ---- 数据准备（与浏览器版同源） ----
    cards_src = []
    for row in (data.get('rows') or [])[:MAX_CARDS]:
        if len(row) < 5:
            continue
        name, tm, cur = row[1], row[3], row[4]
        m = re.match(r'^(-?\d+(?:\.\d+)?)\s*(.*)$', cur or '')
        val, unit = (m.group(1), m.group(2)) if m else ((cur or '--'), '')
        cards_src.append((name, val, unit,
                          tm.replace('-', '/') if tm and tm != '-' else '--'))
    badge_txt, badge_on = {
        'online': ('● 在线', True), 'stale': ('● 离线', False),
        'offline': ('● 离线', False), 'blank': ('● 采集异常', False),
    }.get(status, ('● 未知', False))
    age = data_age_minutes(data.get('rows') or [])
    if status == 'online':
        note_on, note = True, '数据时间 {}（{}）'.format(
            max((r[3] for r in data.get('rows', []) if len(r) > 3 and r[3].strip() != '-'),
                default='--'), fmt_age(age))
    elif status in ('stale', 'offline'):
        note_on, note = False, '非实时数据 · 最后上报 {}'.format(fmt_age(age))
    else:
        note_on, note = False, '未能读取到变量数据'

    # ---- 尺寸计算（px = 逻辑 × 3） ----
    nav_h = 52 * S
    tabs_h = (6 + 9 + 9 + 12) * S + 15 * S          # pad + tab 文本高
    meta_h = (12 + 12) * S + 23 * S * 3
    note_h = (9 + 9) * S + 12 * S + 6 * S
    n_rows = (len(cards_src) + 1) // 2
    n_rows_fixed = (MAX_CARDS + 1) // 2   # 图高统一按满 10 个变量位（5 行）计，
                                          # 变量少的设备留白，保证所有账号/设备图比例一致
    card_h = (13 + 11) * S + 17 * S + 8 * S + 33 * S + 6 * S + 15 * S
    grid_h = 10 * S + n_rows_fixed * card_h + max(0, n_rows_fixed - 1) * 10 * S + 16 * S
    H = int(nav_h + tabs_h + 10 * S + meta_h + 10 * S + note_h + grid_h)

    img = Image.new('RGB', (W, H), '#f2f4f8')
    d = ImageDraw.Draw(img)
    y = 0

    # ---- 1. 顶部导航条 ----
    d.rectangle([0, y, W, y + nav_h], fill='#ffffff')
    d.line([0, y + nav_h, W, y + nav_h], fill='#ececec', width=1)
    _pill_text(d, (14 * S, y + nav_h / 2), '‹', 22, anchor='lm', color='#222222')
    bw = measure(badge_txt, 11) + 18 * S
    bx = W - 14 * S - bw
    rrect(d, [bx, y + (nav_h - 22 * S) / 2, bx + bw, y + (nav_h + 22 * S) / 2],
          11 * S, '#e3f7ec' if badge_on else '#fdf3e0')
    _pill_text(d, (bx + 9 * S, y + nav_h / 2), badge_txt, 11, bold=True,
               color='#0f7b3d' if badge_on else '#8a6d3b', anchor='lm')
    title_max = bx - 8 * S - (14 * S + 24 * S)
    _pill_text(d, (50 * S, y + nav_h / 2),
               _pil_ellipsis(d, dev['name'], f(17), title_max),
               17, bold=True, anchor='lm')
    y += nav_h

    # ---- 2. 四个页签 ----
    d.rectangle([0, y, W, y + tabs_h], fill='#ffffff')
    tx, tw = 10 * S, (W - 20 * S - 3 * 8 * S) / 4
    labels = ('实时概况', '设备组态', '报警记录', '定位轨迹')
    for i, lb in enumerate(labels):
        x0 = tx + i * (tw + 8 * S)
        d.rounded_rectangle([x0, y + 6 * S, x0 + tw, y + 6 * S + 33 * S],
                            radius=6 * S, fill='#1f4bb8' if i == 0 else '#eef1f6')
        _pill_text(d, (x0 + tw / 2, y + 6 * S + 16.5 * S), lb, 15,
                   bold=(i == 0), color='#ffffff' if i == 0 else '#333333', anchor='mm')
    y += tabs_h

    # ---- 3. 设备信息卡 ----
    my = y + 10 * S
    rrect(d, [12 * S, my, W - 12 * S, my + meta_h], 10 * S, '#ffffff')
    _pill_text(d, (14 * S + 12 * S, my + 12 * S + 12 * S),
               _pil_ellipsis(d, dev['name'], fb(13), W - 60 * S - measure(dev['no'], 11)),
               13, bold=True, anchor='ls', color='#1a1a1a')
    _pill_text(d, (W - 14 * S - measure(dev['no'], 11), my + 12 * S + 12 * S),
               dev['no'], 11, color='#8a97ad', anchor='ls')
    for li, line in enumerate(('所属组织：{}　设备模板：{}'.format(data.get('org', '--'), data.get('tpl', '--')),
                               '网关：{}　设备地址：{}'.format(data.get('gw', '--'), data.get('addr', '--')))):
        _pill_text(d, (26 * S, my + 12 * S + 23 * S * (li + 1) + 12 * S),
                   _pil_ellipsis(d, line, f(12), W - 52 * S), 12, color='#666666', anchor='ls')
    y = my + meta_h

    # ---- 4. 数据时间提示条 ----
    ny = y + 10 * S
    rrect(d, [12 * S, ny, W - 12 * S, ny + note_h], 8 * S,
          '#e9f8ef' if note_on else '#fdf4e3')
    _pill_text(d, (26 * S, ny + note_h / 2), note, 12, anchor='lm',
               color='#0f7b3d' if note_on else '#9a6a12')
    y = ny + note_h

    # ---- 5. 变量卡片（两列） ----
    gy = y + 10 * S
    cw = (W - 24 * S - 10 * S) / 2
    for ci, (nm, val, unit, tm) in enumerate(cards_src):
        r, c = divmod(ci, 2)
        cx = 12 * S + c * (cw + 10 * S)
        cy = gy + r * (card_h + 10 * S)
        rrect(d, [cx, cy, cx + cw, cy + card_h], 10 * S, '#ffffff')
        _pill_text(d, (cx + 12 * S, cy + 13 * S + 10 * S),
                   _pil_ellipsis(d, nm, f(13), cw - 40 * S), 13,
                   color='#333333', anchor='ls')
        _pill_text(d, (cx + cw - 10 * S, cy + 13 * S + 10 * S), '›', 15,
                   color='#b9c2d0', anchor='rs')
        vy = cy + 13 * S + 17 * S + 8 * S + 24 * S
        _pill_text(d, (cx + 12 * S, vy), val, 24, bold=True, anchor='lm')
        uw = measure(val, 24, bold=True)
        if unit:
            _pill_text(d, (cx + 12 * S + uw + 2 * S, vy + 1 * S), unit, 13,
                       bold=True, color='#444444', anchor='lm')
        _pill_text(d, (cx + 12 * S, cy + card_h - 11 * S - 2 * S), tm, 11,
                   color='#9aa4b2', anchor='ls')

    img.save(out_path, 'PNG')


def render_mobile_cards(render_pg, dev, data, out_path, status):
    """把数据按小程序'实时概况'样式渲染成手机宽度页面并截图（含在线/离线徽标）
    只渲染前 MAX_CARDS 个变量：模板里的修正值/时钟/时间段等工程参数不进图，
    也避免部分设备变量多达 50 个时整张图被拉成超长条（旧版浏览器表格每页 10 行，观感与此一致）。
    render_pg 为 None 时改用 Pillow 本地绘制（免安装版，无浏览器）。"""
    if render_pg is None:
        render_mobile_cards_pil(dev, data, out_path, status)
        return
    cards = []
    for row in (data.get('rows') or [])[:MAX_CARDS]:
        # 表格列: 变量ID | 变量名称 | 变量类型 | 更新时间 | 当前值 | 操作
        if len(row) < 5:
            continue
        name, _typ, tm, cur = row[1], row[2], row[3], row[4]
        m = re.match(r'^(-?\d+(?:\.\d+)?)\s*(.*)$', cur)
        val, unit = (m.group(1), m.group(2)) if m else ((cur or '--'), '')
        cards.append(CARD_TMPL.format(name=name, val=val, unit=unit,
                                      time=tm.replace('-', '/') if tm and tm != '-' else '--'))
    badge = {
        'online':  ('on',  '● 在线'),
        'stale':   ('off', '● 离线'),
        'offline': ('off', '● 离线'),
        'blank':   ('off', '● 采集异常'),
    }.get(status, ('off', '● 未知'))
    # 数据时间提示条：离线时明确告知数据是哪一刻的，避免误读为实时值
    age = data_age_minutes(data.get('rows') or [])
    if status == 'online':
        note_cls, note = 'ok', '数据时间 {}（{}）'.format(
            max((r[3] for r in data.get('rows', []) if len(r) > 3 and r[3].strip() != '-'),
                default='--'), fmt_age(age))
    elif status in ('stale', 'offline'):
        note_cls, note = 'warn', '非实时数据 · 最后上报 {}'.format(fmt_age(age))
    else:
        note_cls, note = 'warn', '未能读取到变量数据'
    html = MOBILE_TMPL.format(title=dev['name'], no=dev['no'],
                              org=data.get('org', '--'), tpl=data.get('tpl', '--'),
                              gw=data.get('gw', '--'), addr=data.get('addr', '--'),
                              badge_cls=badge[0], badge_txt=badge[1],
                              note_cls=note_cls, note=note,
                              cards=''.join(cards))
    render_pg.set_content(html, timeout=30000)
    # 先把视口撑到内容高度，再普通截图。
    # 原来用 full_page=True 整页截图，Chromium 会把超视口区域重新光栅化，
    # grid 布局下个别变量卡片会被分到不同合成层，渲成深灰色，
    # 看起来就像"这张卡片被点了一下"（实际是渲染问题，不是选中态）。
    try:
        h = render_pg.evaluate(
            "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
        vp = render_pg.viewport_size or {'width': 390, 'height': 844}
        if h and h > vp.get('height', 844):
            render_pg.set_viewport_size({'width': vp.get('width', 390), 'height': int(h)})
        # 让字体和布局稳定下来再截，避免拿到半渲染状态
        render_pg.evaluate("document.body.style.pointerEvents='none'")
        render_pg.wait_for_timeout(400)
    except Exception:
        pass
    render_pg.screenshot(path=out_path)


def inject_session(pg, token, platform_id):
    """重新打开带 token 的入口页，让前端 initUserInfo 建立会话。
    注意：必须每台设备都重新加载入口页，仅靠 hash 切换不会重新拉取数据。"""
    url = INJECT_URL + '?' + urllib.parse.urlencode({
        'token': token, 'usrpasstoken': 'x' * 8, 'platformId': platform_id})
    pg.goto(url, timeout=60000, wait_until='domcontentloaded')
    pg.wait_for_timeout(7000)
    return 'account.usr.cn' not in pg.url


def open_device_page(pg, dev, token, state):
    """每台设备重新注入会话 -> hash 切到设备详情页。
    若会话被踢回登录页则自动重试一次。"""
    for attempt in (1, 2):
        if not inject_session(pg, token, state.get('platform_id', 'W3rfV')):
            if attempt == 2:
                raise RuntimeError('会话注入失败，被跳转到通行证登录页')
            continue
        pg.evaluate(
            "u => { location.hash = u; window.dispatchEvent(new HashChangeEvent('hashchange')); }",
            '#/cloud/device/ViewDeviceDataView?devid=' + dev['no'])
        pg.wait_for_timeout(8000)
        if 'account.usr.cn' not in pg.url:
            return
        print('       [重试] 会话失效，重新注入（第 {} 次）'.format(attempt))
    raise RuntimeError('会话失效，两次重注入后仍被跳转到通行证登录页')


def shoot(render_pg, dev, data, out_dir):
    """数据已就绪 -> 渲染手机版式 -> 截图（API 与浏览器两种模式共用）
    状态: 'online' 数据新鲜 / 'stale' 有值但过旧 / 'offline' 无数据 / 'blank' 未渲染
    """
    rows = data.get('rows') or []
    status = judge_status(rows)
    age = data_age_minutes(rows)
    filename = "{}_{}.png".format(datetime.now().strftime("%Y%m%d_%H%M%S"), safe_name(dev['name']))
    path = os.path.join(out_dir, filename)
    render_mobile_cards(render_pg, dev, data, path, status)
    vn = '{} 个变量'.format(len(rows))
    if len(rows) > MAX_CARDS:
        vn = '前 {}/{} 个变量'.format(MAX_CARDS, len(rows))
    preview = '{} | {} | 数据{}'.format(dev['name'], vn, fmt_age(age))
    return status, preview, path


def worker(worker_id, rows, out_dir, token, headless=True, platform_id='W3rfV',
           browser_mode=False, acc_name=''):
    """单 worker 串行处理分到的设备。
    API 直调模式（默认）：纯接口拿数据 ~0.3s/台 + Pillow 本地绘图渲染截图（无浏览器）；
    浏览器模式（--browser-mode，需完整 Python 环境装 playwright）：每台重新注入
    会话打开控制台页（慢，接口异常时保底用）。"""
    results, errors = [], []
    state = {'ready': False, 'platform_id': platform_id}
    pw = browser = ctx = render_pg = None
    try:
        if browser_mode:
            if not HAS_PLAYWRIGHT:
                raise RuntimeError('当前运行环境未内置浏览器组件（playwright），'
                                   '不支持 --browser-mode，请直接用默认模式重跑')
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=headless,
                                         args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'])
            ctx = browser.new_context(viewport={'width': 1500, 'height': 950})
            rctx = browser.new_context(viewport={'width': MOBILE_W, 'height': 844},
                                       device_scale_factor=MOBILE_DPR)
            render_pg = rctx.new_page()
        for i, dev in enumerate(rows):
            try:
                _PAUSE_EVENT.wait()      # GUI"暂停"时阻塞在此：当前设备跑完后停住，恢复后继续
                if browser_mode:
                    pg = ctx.new_page()
                    try:
                        open_device_page(pg, dev, token, state)
                        try:
                            pg.wait_for_selector('.el-table__row', timeout=20000)
                        except Exception:
                            pass
                        pg.wait_for_timeout(int(EXTRA_WAIT_SEC * 1000))
                        tidy_page(pg)
                        pg.wait_for_timeout(500)
                        data = extract_device_data(pg)
                    finally:
                        try:
                            pg.close()
                        except Exception:
                            pass
                else:
                    data = fetch_device_data_api(token, dev['no'])
                status, preview, path = shoot(render_pg, dev, data, out_dir)
                results.append((dev['name'], status, preview, path))
                tag = STATUS_TAG[status]
                print('[{}][worker{}] ({}/{}) {} {}'.format(acc_name, worker_id, i + 1, len(rows), tag, dev['name']))
            except Exception as e:
                errors.append((dev['name'], repr(e)[:150]))
                state['ready'] = False          # 下次重新注入
                print('[{}][worker{}] ({}/{}) ✗失败 {} :: {}'.format(
                    acc_name, worker_id, i + 1, len(rows), dev['name'], repr(e)[:90]))
    finally:
        try:
            if pw:
                pw.stop()
        except Exception:
            pass
    return worker_id, results, errors


def choose_account(accounts):
    """让用户选择账号；只有一个账号时直接用它"""
    if len(accounts) == 1:
        return accounts[0]
    print('\n发现 {} 个可用账号：'.format(len(accounts)))
    for i, a in enumerate(accounts, 1):
        print('  [{}] {}  （{}）'.format(i, a['name'], a['account']))
    print('  [a] 全部依次执行')
    while True:
        try:
            sel = input('请选择 [1-{}/a]（回车默认 a=全部）: '.format(len(accounts))).strip().lower()
        except EOFError:
            sel = ''
        if sel == '' or sel in ('a', 'all', '全部'):
            return 'ALL'
        if sel.isdigit() and 1 <= int(sel) <= len(accounts):
            return accounts[int(sel) - 1]
        print('输入无效，请重新选择。')


def run_for_account(cfg, args, batch_dir):
    """单个账号完整跑一遍：登录 -> 取设备 -> 截图 -> 报告 -> 打包"""
    print('\n' + '#' * 70)
    print('# 账号：{}（{}）'.format(cfg['name'], cfg['account']))
    print('#' * 70)

    token, uid = ensure_login(cfg)
    devs = get_devices(token, uid)
    total_all = len(devs)
    kws = cfg.get('station_kw') or []
    nos = set(cfg.get('device_nos') or [])
    if nos:
        # 图形界面里手工勾选的站点（精确到设备号，优先级最高）
        kept = [d for d in devs if d['no'] in nos]
        miss = nos - {d['no'] for d in devs}
        if miss:
            print('[提示] 有 {} 个已选站点不在账号设备列表中（可能刚被删除/改名）'.format(len(miss)))
        devs = kept
        print('指定站点: 保留 {} / 共 {} 台'.format(len(devs), total_all))
    elif kws:
        # accounts.csv 的「站点过滤」对所有功能生效（截图/监控/历史），
        # 目的：不需要的设备不浪费请求和截图资源
        kept = [d for d in devs if any(k in d['name'] for k in kws)]
        miss = [k for k in kws if not any(k in d['name'] for d in devs)]
        if miss:
            print('[警告] 关键词未匹配到任何设备: {}（请核对设备名写法）'.format('、'.join(miss)))
        devs = kept
        print('站点过滤: 保留 {} / 共 {} 台'.format(len(devs), total_all))
    if args.limit:
        devs = devs[:args.limit]
    print('共 {} 台设备'.format(len(devs)))

    # 目录结构： 批次时间戳/账号名/  （多账号时同一次运行归在同一时间戳下）
    out_dir = os.path.join(batch_dir, safe_name(cfg['name']))
    os.makedirs(out_dir, exist_ok=True)
    print('截图目录: {}'.format(out_dir))

    n = max(1, args.workers)
    if args.browser_mode:
        n = 1        # 浏览器模式多并发会互踢会话（实测），强制串行
    buckets = [[] for _ in range(n)]
    for idx, d in enumerate(devs):
        buckets[idx % n].append(d)

    all_res, all_err = [], []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(worker, w, buckets[w], out_dir, token,
                            not args.no_headless, cfg.get('platform_id') or 'W3rfV',
                            args.browser_mode, cfg.get('name') or '')
                for w in range(n) if buckets[w]]
        for fut in as_completed(futs):
            _, ok, err = fut.result()
            all_res.extend(ok)
            all_err.extend(err)

    print('=' * 70)
    n_on = sum(1 for _, s, _, _ in all_res if s == 'online')
    n_stale = sum(1 for _, s, _, _ in all_res if s == 'stale')
    n_off = sum(1 for _, s, _, _ in all_res if s == 'offline')
    n_blank = sum(1 for _, s, _, _ in all_res if s == 'blank')
    report = []
    for name, status, preview, path in sorted(all_res, key=lambda x: x[0]):
        tag = STATUS_TAG[status]
        print('[{}][{}] {}'.format(cfg.get('name'), tag, name))
        print('      内容: {}'.format(preview))
        m = re.search(r'数据(.+)$', preview or '')
        report.append({'name': name, 'status': status,
                       'file': os.path.basename(path), 'preview': preview,
                       'age': m.group(1) if m else ''})
    for name, e in sorted(all_err):
        print('[✗失败] {} :: {}'.format(name, e))
    print('=' * 70)
    print('完成：成功 {} 个（在线 {}，离线-陈旧数据 {}，离线-无数据 {}，采集异常 {}）；失败 {} 个；耗时 {:.0f}s'.format(
        len(all_res), n_on, n_stale, n_off, n_blank, len(all_err), time.time() - t0))
    print('截图目录: {}'.format(out_dir))

    report_meta = {'account': cfg['account'], 'account_name': cfg['name'],
                   'run_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   'summary': {'total': len(all_res), 'online': n_on, 'stale': n_stale,
                               'offline': n_off, 'blank': n_blank, 'failed': len(all_err)},
                   'devices': report}
    json.dump(report_meta, open(os.path.join(out_dir, '巡站报告.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    return out_dir, len(all_res)


# ---------------- 图形界面（--gui） ----------------

class _QueueWriter:
    """把 print 输出重定向到队列，界面轮询取出来显示到日志区（线程安全）"""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        for line in s.splitlines():
            if line.strip():
                self.q.put(line)
        return len(s)

    def flush(self):
        pass


def gui_open_path(path):
    """用系统默认程序打开文件夹/文件。失败不静默：真实原因打印到日志区。
    Mac 上用绝对路径 /usr/bin/open（不依赖 PATH），失败再退回 PATH 查找并
    尝试 open -R 在 Finder 中定位，保证用户一定能看到文件在哪。"""
    try:
        if sys.platform == 'darwin':
            import subprocess
            exe = '/usr/bin/open' if os.path.exists('/usr/bin/open') else 'open'
            r = subprocess.run([exe, path], capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                print('[提示] 打开失败({}): {} {}'.format(
                    r.returncode, (r.stderr or '').strip()[:200], path))
                try:
                    r2 = subprocess.run([exe, '-R', path], capture_output=True,
                                        text=True, timeout=15)
                    if r2.returncode == 0:
                        print('[提示] 已改为在 Finder 中显示该文件位置。')
                        return
                except Exception:
                    pass
                print('[提示] 请手动打开这个位置: {}'.format(os.path.dirname(path)))
        elif os.name == 'nt':
            os.startfile(path)          # noqa
        else:
            import subprocess
            r = subprocess.Popen(['xdg-open', path])
            del r
    except Exception as e:
        print('[提示] 打开失败: {} :: {}'.format(repr(e)[:150], path))


def run_gui():
    """图形界面主流程：账号勾选 -> 开始巡站 -> 进度/日志/结果 -> 打开产物"""
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
    from types import SimpleNamespace

    migrate_old_tokens()
    if not os.path.exists(accounts_path()):
        ensure_accounts_file()
    try:
        accounts = load_accounts()
    except SystemExit:
        accounts = []

    root = tk.Tk()
    root.title('有人云巡站客户端')
    root.geometry('840x700')
    root.minsize(740, 600)

    style = ttk.Style()
    try:
        style.theme_use('aqua' if sys.platform == 'darwin'
                        else ('vista' if os.name == 'nt' else 'clam'))
    except Exception:
        pass

    mono = ('Menlo', 11) if sys.platform == 'darwin' else \
           ('Microsoft YaHei UI', 9) if os.name == 'nt' else ('DejaVu Sans Mono', 10)
    st = {'running': False, 'q': queue.Queue(), 'zip': None,
          'device_sel': None, 'row_paths': {},
          'batch': None, 'done': None, 'err': None}

    # ---------- 第 1 步：账号勾选 ----------
    top = ttk.LabelFrame(root, text=' 第 1 步：勾选要跑的账号（来自 accounts.csv） ')
    top.pack(fill='x', padx=10, pady=(10, 6))

    cols = ('sel', 'name', 'acc', 'kws')
    tree = ttk.Treeview(top, columns=cols, show='headings',
                        height=min(max(len(accounts), 2), 5), selectmode='none')
    for c, w, txt in (('sel', 44, '选中'), ('name', 170, '名称'),
                      ('acc', 140, '账号'), ('kws', 220, '站点过滤')):
        tree.heading(c, text=txt)
        tree.column(c, width=w, anchor='center' if c == 'sel' else 'w')
    tree.pack(fill='x', padx=8, pady=(6, 2))
    for a in accounts:
        tree.insert('', 'end', values=('☑', a['name'], a['account'],
                                       '、'.join(a.get('station_kw') or []) or '（全部设备）'))

    def toggle_sel(ev):
        if tree.identify_column(ev.x) != '#1':
            return
        row = tree.identify_row(ev.y)
        if not row:
            return
        v = tree.item(row, 'values')
        tree.item(row, values=('☐' if v[0] == '☑' else '☑',) + tuple(v[1:]))

    tree.bind('<Button-1>', toggle_sel)

    def selected_accounts():
        sel = []
        for a, item in zip(accounts, tree.get_children()):
            if tree.item(item, 'values')[0] == '☑':
                sel.append(a)
        return sel

    def set_all(mark):
        for item in tree.get_children():
            v = tree.item(item, 'values')
            tree.item(item, values=(mark,) + tuple(v[1:]))

    btns1 = ttk.Frame(top)
    btns1.pack(fill='x', padx=8, pady=(2, 6))
    ttk.Button(btns1, text='全选', width=8, command=lambda: set_all('☑')).pack(side='left')
    ttk.Button(btns1, text='全不选', width=8, command=lambda: set_all('☐')).pack(side='left', padx=6)
    ttk.Button(btns1, text='编辑账号表格', command=lambda: gui_open_path(accounts_path())).pack(side='left')
    ttk.Label(btns1, text='  勾选打在"选中"列上；站点过滤为空 = 该账号全部设备').pack(side='left')

    # ---------- 第 2 步：运行控制 ----------
    mid = ttk.LabelFrame(root, text=' 第 2 步：开始巡站 ')
    mid.pack(fill='x', padx=10, pady=6)
    btn_run = ttk.Button(mid, text='▶ 开始巡站', width=16)
    btn_run.pack(side='left', padx=8, pady=8)
    btn_pick = ttk.Button(mid, text='📋 选择站点', width=13)
    btn_pick.pack(side='left')
    btn_pause = ttk.Button(mid, text='⏸ 暂停', width=9, state='disabled')
    btn_pause.pack(side='left', padx=6)
    prog = ttk.Progressbar(mid, mode='indeterminate', length=140)
    prog.pack(side='left', padx=(0, 10))
    lbl_prog = ttk.Label(mid, text='就绪 · 勾选账号后点"开始巡站"')
    lbl_prog.pack(side='left')

    # ---------- 日志 ----------
    lf_log = ttk.LabelFrame(root, text=' 运行日志 ')
    lf_log.pack(fill='both', expand=True, padx=10, pady=6)
    log = scrolledtext.ScrolledText(lf_log, height=9, state='disabled',
                                    font=mono, wrap='none')
    log.pack(fill='both', expand=True, padx=4, pady=4)

    # ---------- 结果 ----------
    lf_res = ttk.LabelFrame(root, text=' 结果（双击一行打开对应截图） ')
    lf_res.pack(fill='both', expand=True, padx=10, pady=(6, 4))
    res = ttk.Treeview(lf_res, columns=('name', 'status', 'age', 'file'),
                       show='headings', height=7, selectmode='browse')
    for c, w, txt in (('name', 230, '设备'), ('status', 110, '状态'),
                      ('age', 150, '数据时间'), ('file', 240, '截图文件')):
        res.heading(c, text=txt)
        res.column(c, width=w, anchor='w')
    ysb = ttk.Scrollbar(lf_res, orient='vertical', command=res.yview)
    res.configure(yscrollcommand=ysb.set)
    res.pack(side='left', fill='both', expand=True, padx=(4, 0), pady=4)
    ysb.pack(side='left', fill='y', pady=4)
    res.tag_configure('online', foreground='#0f7b3d')
    res.tag_configure('stale', foreground='#9a6a12')
    res.tag_configure('offline', foreground='#5a6472')
    res.tag_configure('blank', foreground='#c0392b')

    # ---------- 底部 ----------
    bottom = ttk.Frame(root)
    bottom.pack(fill='x', padx=10, pady=(2, 10))
    def open_batch_dir():
        if st.get('batch'):
            gui_open_path(st['batch'])
        else:
            print('[提示] 截图文件夹还没生成——先跑一次巡站。')

    def open_zip_file():
        if st.get('zip'):
            gui_open_path(st['zip'])
        else:
            print('[提示] 结果压缩包还没生成——先跑一次巡站（跑完自动打包）。')

    ttk.Button(bottom, text='📁 打开截图文件夹',
               command=open_batch_dir).pack(side='left')
    ttk.Button(bottom, text='🗂 打开结果压缩包',
               command=open_zip_file).pack(side='left', padx=6)
    ttk.Label(bottom, text='双击"结果"里的行可直接查看图片').pack(side='right')

    def append_log(line):
        log.configure(state='normal')
        log.insert('end', line + '\n')
        log.see('end')
        log.configure(state='disabled')

    def fetch_devices_thread(sel):
        """后台逐账号登录并拉取设备列表，完成后经队列通知主线程弹选择窗口"""
        try:
            result = {}
            for cfg in sel:
                token, uid = ensure_login(cfg)
                devs = get_devices(token, uid)
                result[cfg['name']] = [(d['no'], d['name']) for d in devs]
                st['q'].put('已获取「{}」设备列表：{} 台'.format(cfg['name'], len(devs)))
            st['q'].put(('pick_done', result))
        except Exception as e:
            st['q'].put(('pick_fail', repr(e)[:300]))

    def on_pick():
        """点"选择站点"：先勾账号 -> 后台取设备列表 -> 弹选择窗口"""
        if st['running']:
            return
        sel = selected_accounts()
        if not sel:
            messagebox.showwarning('提示', '请先在上方勾选账号（点"选中"列）。')
            return
        btn_pick.configure(state='disabled')
        lbl_prog.configure(text='正在获取设备列表…（首次要登录，几秒）')
        threading.Thread(target=fetch_devices_thread, args=(sel,), daemon=True).start()

    def open_pick_window(result):
        """站点挑选窗口：全部设备列出，勾选后只跑选中的（全选 = 跑该账号全部）"""
        win = tk.Toplevel(root)
        win.title('选择要巡检的站点')
        win.geometry('780x560')
        win.transient(root)

        marks = {}                      # (账号名, 设备号) -> 是否选中，默认全选
        for acc, lst in result.items():
            for no, name in lst:
                marks[(acc, no)] = True

        topf = ttk.Frame(win)
        topf.pack(fill='x', padx=10, pady=(10, 4))
        ttk.Label(topf, text='搜索:').pack(side='left')
        var_kw = tk.StringVar()
        ent = ttk.Entry(topf, textvariable=var_kw, width=26)
        ent.pack(side='left', padx=6)
        lbl_cnt = ttk.Label(topf, text='')
        lbl_cnt.pack(side='left', padx=10)

        tv = ttk.Treeview(win, columns=('sel', 'acc', 'name'),
                          show='headings', height=16, selectmode='none')
        for c, w, txt in (('sel', 50, '选中'), ('acc', 150, '账号'), ('name', 460, '设备')):
            tv.heading(c, text=txt)
            tv.column(c, width=w, anchor='center' if c == 'sel' else 'w')
        tv.pack(fill='both', expand=True, padx=10, pady=4)

        def update_cnt():
            n = sum(1 for v in marks.values() if v)
            lbl_cnt.configure(text='已选 {} / 共 {} 台'.format(n, len(marks)))

        def refresh():
            kw = var_kw.get().strip()
            tv.delete(*tv.get_children())
            for acc, lst in result.items():
                for no, name in lst:
                    if kw and kw not in name and kw not in acc:
                        continue
                    tv.insert('', 'end', iid='{}|{}'.format(acc, no),
                              values=('☑' if marks[(acc, no)] else '☐', acc, name))
            update_cnt()

        def toggle(ev):
            if tv.identify_column(ev.x) != '#1':
                return
            row = tv.identify_row(ev.y)
            if not row:
                return
            acc, no = row.split('|', 1)
            marks[(acc, no)] = not marks[(acc, no)]
            v = tv.item(row, 'values')
            tv.item(row, values=('☐' if v[0] == '☑' else '☑',) + tuple(v[1:]))
            update_cnt()
        tv.bind('<Button-1>', toggle)
        ent.bind('<KeyRelease>', lambda e: refresh())

        def set_all(flag):
            for k in marks:
                marks[k] = flag
            refresh()

        def confirm():
            sel_nos, desc = {}, []
            for acc, lst in result.items():
                nos = [no for (a, no) in marks if a == acc and marks[(a, no)]]
                if not nos:
                    sel_nos[acc] = []
                    desc.append('{} 0/{}（跳过）'.format(acc, len(lst)))
                elif len(nos) < len(lst):
                    sel_nos[acc] = nos
                    desc.append('{} {}/{}'.format(acc, len(nos), len(lst)))
                else:
                    sel_nos[acc] = nos      # 全选也存完整列表：跑该账号全部（绕过关键词过滤）
                    desc.append('{} 全部 {} 台'.format(acc, len(lst)))
            st['device_sel'] = sel_nos
            append_log('已指定站点：' + '，'.join(desc) + '（点"开始巡站"生效）')
            lbl_prog.configure(text='站点已指定 · ' + '，'.join(desc))
            win.destroy()

        botf = ttk.Frame(win)
        botf.pack(fill='x', padx=10, pady=8)
        ttk.Button(botf, text='全选', command=lambda: set_all(True)).pack(side='left')
        ttk.Button(botf, text='全不选', command=lambda: set_all(False)).pack(side='left', padx=6)
        ttk.Label(botf, text='全部勾选 = 跑该账号全部设备').pack(side='left', padx=8)
        ttk.Button(botf, text='确 定', command=confirm).pack(side='right')
        ttk.Button(botf, text='取 消', command=win.destroy).pack(side='right', padx=6)
        refresh()

    def toggle_pause():
        """暂停/继续：当前设备跑完后停住，恢复后继续跑剩下的"""
        if not st['running']:
            return
        if _PAUSE_EVENT.is_set():
            _PAUSE_EVENT.clear()
            btn_pause.configure(text='▶ 继续')
            prog.stop()
            lbl_prog.configure(text='已暂停（当前设备完成后停住）')
            append_log('[暂停] 当前设备完成后停住，点"继续"恢复。')
        else:
            _PAUSE_EVENT.set()
            btn_pause.configure(text='⏸ 暂停')
            prog.start(12)
            lbl_prog.configure(text='运行中…')
            append_log('[继续] 恢复运行。')

    def poll_queue():
        while True:
            try:
                msg = st['q'].get_nowait()
            except queue.Empty:
                break
            if msg is None:                       # 后台线程结束标记
                st['running'] = False
                _PAUSE_EVENT.set()
                btn_run.configure(state='normal')
                btn_pause.configure(state='disabled', text='⏸ 暂停')
                btn_pick.configure(state='normal')
                prog.stop()
                if st['err']:
                    lbl_prog.configure(text='运行出错')
                    messagebox.showerror('运行出错', st['err'])
                else:
                    ok, on, tot = st['done'] or (0, 0, 0)
                    lbl_prog.configure(text='完成 · {} 台（在线 {}）'.format(ok, on))
                    messagebox.showinfo('完成', '本次巡站完成：共 {} 张截图，在线 {} 台。\n\n结果压缩包已生成，可在"打开结果压缩包"查看。'.format(ok, on))
                continue
            if isinstance(msg, tuple):            # ('pick_done', result) / ('pick_fail', err)
                kind, payload = msg
                btn_pick.configure(state='normal')
                if kind == 'pick_done':
                    lbl_prog.configure(text='就绪')
                    if payload:
                        open_pick_window(payload)
                elif kind == 'pick_fail':
                    lbl_prog.configure(text='获取设备列表失败')
                    messagebox.showerror('获取设备列表失败', payload)
                continue
            append_log(msg)
        root.after(250, poll_queue)
    poll_queue()

    def fill_results(account_name, report_path):
        try:
            d = json.load(open(report_path, encoding='utf-8'))
        except Exception:
            return
        s = d.get('summary') or {}
        append_log('—— {} 完成：{} 张（在线 {} / 陈旧 {} / 无数据 {} / 异常 {}）'.format(
            account_name, s.get('total', 0), s.get('online', 0),
            s.get('stale', 0), s.get('offline', 0), s.get('blank', 0)))
        for dev in d.get('devices') or []:
            preview = dev.get('preview') or ''
            parts = [p.strip() for p in preview.split('|')]
            age = parts[-1] if parts else ''
            iid = res.insert('', 'end', values=(
                dev.get('name'), STATUS_TAG.get(dev.get('status'), '?'), age,
                dev.get('file')), tags=(dev.get('status'),))
            f = dev.get('file') or ''
            if f:
                st['row_paths'][iid] = os.path.join(os.path.dirname(report_path), f)
        return s or {}

    def open_result_row(ev):
        item = res.identify_row(ev.y)
        if not item:
            return
        path = st.get('row_paths', {}).get(item)
        if not path:
            return
        if os.path.exists(path):
            gui_open_path(path)
        else:
            print('[提示] 截图文件不在了（可能批次被清理）：{}'.format(path))

    res.bind('<Double-1>', open_result_row)

    def worker(sel, args):
        try:
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            batch_dir = os.path.join(SCRIPT_DIR, '{}_截图'.format(stamp))
            os.makedirs(batch_dir, exist_ok=True)
            st['batch'] = batch_dir
            print('批次目录: {}'.format(batch_dir))
            total_ok, total_on = 0, 0
            for i, cfg in enumerate(sel, 1):
                print('\n{} 账号 {}/{}：{}（{}）'.format('=' * 20, i, len(sel),
                                                       cfg['name'], cfg['account']))
                nos = (st.get('device_sel') or {}).get(cfg['name'])
                if nos is not None and not nos:
                    print('[跳过] 该账号未勾选任何站点。')
                    continue
                cfg2 = dict(cfg, device_nos=nos) if nos else cfg
                out_dir, ok_count = run_for_account(cfg2, args, batch_dir)
                total_ok += ok_count
                summ = fill_results(cfg['name'], os.path.join(out_dir, '巡站报告.json'))
                total_on += (summ or {}).get('online', 0)
            if total_ok > 0:
                zp = os.path.join(SCRIPT_DIR, '有人云巡站截图_{}.zip'.format(stamp))
                make_zip(batch_dir, zp)
                st['zip'] = zp
                print('\n压缩包: {}'.format(zp))
            # 往期批次不再自动清理（界面已有「清理往期」入口，删不删由用户决定）
            print('\n' + NO_AUTO_CLEAN_NOTE)
            print('\n全部完成。')
            st['done'] = (total_ok, total_on, 0)
        except Exception as e:
            st['err'] = repr(e)[:400]
            print('[错误] {}'.format(st['err']))
        finally:
            st['q'].put(None)

    def on_run():
        if st['running']:
            return
        sel = selected_accounts()
        if not sel:
            messagebox.showwarning('提示', '请先在上方勾选至少一个账号（点"选中"列）。')
            return
        st.update(running=True, zip=None, batch=None, done=None, err=None)
        res.delete(*res.get_children())
        st['row_paths'] = {}
        btn_run.configure(state='disabled')
        btn_pick.configure(state='disabled')
        btn_pause.configure(state='normal', text='⏸ 暂停')
        _PAUSE_EVENT.set()
        prog.start(12)
        lbl_prog.configure(text='运行中…（共 {} 个账号）'.format(len(sel)))
        args = SimpleNamespace(limit=0, workers=WORKERS, browser_mode=False,
                               no_headless=False, multi=len(sel) > 1)
        threading.Thread(target=worker, args=(sel, args), daemon=True).start()

    btn_run.configure(command=on_run)
    btn_pick.configure(command=on_pick)
    btn_pause.configure(command=toggle_pause)
    if not accounts:
        append_log('accounts.csv 里还没有可用账号：点"编辑账号表格"填好后，重新打开本程序。')
    root.mainloop()


def main():
    # 图形界面优先：--gui 或双击启动器进入；缺 tkinter 时自动退回命令行
    if '--gui' in sys.argv:
        try:
            import tkinter  # noqa
        except ImportError:
            print('[提示] 这台电脑缺图形组件（tkinter），改用命令行模式。')
            sys.argv.remove('--gui')
        else:
            run_gui()
            return

    ap = argparse.ArgumentParser(description='有人云巡站客户端（账号从 accounts.csv 读取）')
    ap.add_argument('--gui', action='store_true', help='打开图形界面（双击启动器默认）')
    ap.add_argument('--workers', type=int, default=WORKERS, help='并发数（默认 %d，仅 API 模式生效）' % WORKERS)
    ap.add_argument('--clean', action='store_true',
                    help='本次跑完后清理更早的批次目录（默认不清理，往期截图请在界面用「清理往期」删）')
    ap.add_argument('--keep', type=int, default=KEEP_LAST,
                    help='配合 --clean 使用：除本次外保留的历史批次数（默认 %d）' % KEEP_LAST)
    ap.add_argument('--limit', type=int, default=0, help='只跑前 N 台（测试用）')
    ap.add_argument('--account', default='', help='直接指定账号（名称或手机号），不写则启动时选择')
    ap.add_argument('--all-accounts', action='store_true', help='所有启用账号依次执行')
    ap.add_argument('--no-clean', action='store_true',
                    help='（已无实际作用，保留仅为兼容旧启动器；现在默认就是不清理）')
    ap.add_argument('--no-zip', action='store_true', help='不打包 zip')
    ap.add_argument('--no-headless', action='store_true', help='显示浏览器窗口（排查用）')
    ap.add_argument('--browser-mode', action='store_true',
                    help='改用旧的浏览器控制台取数方式（慢，约18s/台；接口直调异常时保底用）')
    args, unknown = ap.parse_known_args()
    if unknown:
        print('[提示] 忽略了无法识别的参数: {}。'.format(' '.join(unknown)))
        print('       这通常是启动器与 run_usrcloud.py 版本不匹配——请到网盘重新下载最新工具包覆盖。')

    migrate_old_tokens()      # 旧版散落的登录缓存自动归纳进 登录缓存/

    if args.browser_mode:
        print('\n[模式] 浏览器控制台取数（慢速保底模式，每台约 18 秒）')
    else:
        print('\n[模式] API 直调取数（快速模式，每台约 1-2 秒）')

    accounts = load_accounts()

    # 确定要跑哪些账号
    if args.account:
        hit = [a for a in accounts if args.account in (a['name'], a['account'])]
        if not hit:
            print('未在 accounts.csv 中找到账号: {}'.format(args.account))
            sys.exit(1)
        todo = hit
    elif args.all_accounts:
        todo = accounts
    else:
        sel = choose_account(accounts)
        todo = accounts if sel == 'ALL' else [sel]

    args.multi = len(todo) > 1
    if args.multi:
        print('\n将依次执行 {} 个账号（同一次运行归在同一批次目录下）'.format(len(todo)))

    # 批次目录：一个时间戳，本次运行的每个账号在下面各占一个子目录
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    batch_dir = os.path.join(SCRIPT_DIR, '{}_截图'.format(stamp))
    os.makedirs(batch_dir, exist_ok=True)
    print('\n本次批次目录: {}'.format(batch_dir))
    print('结构: {}/<账号名>/截图文件（+ 每账号一份 巡站报告.json）'.format(os.path.basename(batch_dir)))

    total_ok = 0
    for cfg in todo:
        out_dir, ok_count = run_for_account(cfg, args, batch_dir)
        total_ok += ok_count

    # 整批打包（含所有账号）
    zip_path = None
    if not args.no_zip and total_ok > 0:
        zip_path = os.path.join(SCRIPT_DIR, '有人云巡站截图_{}.zip'.format(stamp))
        make_zip(batch_dir, zip_path)
        print('\n压缩包:   {}'.format(zip_path))

    # 往期批次默认不再自动清理（界面已有「清理往期」入口，删不删由用户决定）。
    # 需要旧行为时显式加 --clean。
    if args.clean and not args.limit:
        cleanup_old_batches(SCRIPT_DIR, batch_dir, total_ok, do_clean=True)
    else:
        print('\n' + NO_AUTO_CLEAN_NOTE)

    print('\n全部完成。')


if __name__ == '__main__':
    main()
