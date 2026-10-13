# -*- coding: utf-8 -*-
"""
有人云巡站客户端 · 网页界面服务端（Electron 无头版）
================================================================
用 Python 标准库起一个本机 HTTP 服务（127.0.0.1，端口自动分配），
界面是 ui/index.html，取数与截图逻辑全部复用 run_usrcloud.py，
不新增任何第三方依赖。

两种启动方式：
  - 普通双击 / pythonw 启动：起服务后用系统默认浏览器打开，标签页关掉约 20 秒后自退。
  - 环境变量 USRCLOUD_ELECTRON=1（Electron 壳拉起）：不起浏览器、不自动退出，
    端口写入 %TEMP%/usrcloud_patrol_port.txt 供 Electron 读取，消费即删。

接口：
    GET  /api/accounts            账号列表 + 每账号上次巡站摘要
    GET  /api/devices?account=名  登录并拉取该账号的全部设备
    POST /api/run                 开始巡站 {names:[], device_nos:{}, workers:4}
    GET  /api/status?since=N      运行状态 / 增量日志 / 进度 / 结果
    POST /api/pause  /api/resume
    GET  /shot?p=相对路径          返回截图 PNG
    GET  /api/open?p=路径          用系统默认方式打开（图片或压缩包）
    GET  /api/openfolder?p=路径    在文件管理器里打开文件夹
    POST /api/quit                退出程序
"""
import csv
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tempfile
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE, 'ui')
LOG_FILE = os.path.join(BASE, 'webui_error.log')
SETTINGS_FILE = os.path.join(BASE, '运行参数.json')   # 设置页持久化（并发线程/异常提醒方式）
CLOSE_MODE_FILE = os.path.join(BASE, '关闭方式.json')  # 关闭按钮行为：ask=每次询问 / tray=收托盘 / exit=直接退出
                                                    # 与 Electron 壳 main.js 共用同一个文件，避免两处设置打架

if sys.stdout is None:                      # pythonw.exe 下没有控制台
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='ignore')
if sys.stderr is None:
    sys.stderr = sys.stdout

sys.path.insert(0, BASE)
import run_usrcloud as R                    # noqa: E402

# 实时监控引擎：低频轮询平台接口、维护设备实时状态、按规则判异常。
# 加载失败不影响巡站主功能（降级为纯截图模式）。
try:
    import watch_engine as _WATCH            # noqa: E402
except Exception as _e:                      # pragma: no cover
    _WATCH = None
    print('[监控] 引擎加载失败，已降级为纯截图模式: {!r}'.format(_e))

# ---------------- 全局状态 ----------------

ST = {
    'running': False,
    'q': queue.Queue(),
    'logs': [],
    'results': {},          # 账号名 -> {'report': {...}, 'dir': 绝对路径}
    'wprog': {},            # worker id -> (已处理, 该 worker 总数)
    'acc_done': 0,          # 已完成账号的设备累计数
    'total': 0,             # 本次总设备数（解析"共 N 台设备"）
    'stage': '',
    'batch': None,
    'zip': None,
    'error': None,
    'done': None,           # (成功张数, 在线台数)
    'finished': None,
    'workers': R.WORKERS,
    'notify_mode': 'both',    # 异常提醒方式：off/flash(任务栏闪烁)/toast(系统通知)/both
    # 系统通知通道健康度：由 Electron 壳在实际投递后回报（本机通知平台被禁用时告知用户已降级）
    'notify_health': {'native': None, 'ps': None, 'as': None, 'ts': 0.0},
}


def _load_settings():
    """启动时从 运行参数.json 恢复上次保存的设置"""
    try:
        d = json.load(open(SETTINGS_FILE, encoding='utf-8'))
        w = int(d.get('workers') or 0)
        if 1 <= w <= 8:
            ST['workers'] = w
        nm = str(d.get('notify_mode') or '')
        if nm in ('off', 'flash', 'toast', 'both'):
            ST['notify_mode'] = nm
    except Exception:
        pass


def get_close_mode():
    """读关闭按钮行为；文件缺失/损坏一律回 'ask'（每次问，绝不擅自让程序留在后台）"""
    try:
        d = json.load(open(CLOSE_MODE_FILE, encoding='utf-8'))
        m = str((d or {}).get('mode') or '')
        if m in ('ask', 'tray', 'exit'):
            return m
    except Exception:
        pass
    return 'ask'


def set_close_mode(mode):
    m = str(mode or '')
    if m not in ('ask', 'tray', 'exit'):
        return False
    try:
        json.dump({'mode': m, 'ts': int(time.time())},
                  open(CLOSE_MODE_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print('[警告] 关闭方式保存失败: {}'.format(e))
        return False


def _save_settings():
    try:
        json.dump({'workers': ST['workers'],
                   'notify_mode': ST.get('notify_mode', 'both')},
                  open(SETTINGS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    except Exception as e:
        print('[警告] 设置保存失败: {}'.format(e))


LAST_SEEN = {'t': time.time()}
_PAT_WORKER = re.compile(r'\[worker(\d+)\]\s*\((\d+)/(\d+)\)')
_PAT_TOTAL = re.compile(r'^共\s*(\d+)\s*台设备')


class _QueueWriter:
    """把 print 输出收进队列，前端轮询增量拉取（跨线程安全）"""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        for line in str(s).splitlines():
            if line.strip():
                self.q.put(line.rstrip())

    def flush(self):
        pass


def _pump():
    """后台把队列里的日志搬进 ST['logs']，顺手算进度"""
    while True:
        try:
            line = ST['q'].get(timeout=0.3)
        except queue.Empty:
            continue
        ST['logs'].append(line)
        if len(ST['logs']) > 3000:
            del ST['logs'][:1500]
        m = _PAT_WORKER.search(line)
        if m:
            ST['wprog'][m.group(1)] = (int(m.group(2)), int(m.group(3)))


sys.stdout = _QueueWriter(ST['q'])
sys.stderr = sys.stdout
threading.Thread(target=_pump, daemon=True).start()

# 监控引擎实例（不传 log 回调，日志直接 print -> 进上面的队列 -> 前端可见）
WATCHER = None
if _WATCH is not None:
    try:
        WATCHER = _WATCH.WatchEngine(BASE)
    except Exception as _e:                  # pragma: no cover
        print('[监控] 引擎初始化失败: {!r}'.format(_e))
        WATCHER = None


# ---------------- 辅助 ----------------

def _latest_batch():
    pat = re.compile(r'^\d{8}_\d{6}_截图$')
    ds = [d for d in os.listdir(BASE) if pat.match(d) and os.path.isdir(os.path.join(BASE, d))]
    return os.path.join(BASE, sorted(ds)[-1]) if ds else None


BATCH_PAT = re.compile(r'^\d{8}_\d{6}_截图$')
ZIP_PAT = re.compile(r'^有人云巡站截图_(\d{8}_\d{6})\.zip$')


def _dir_size(d):
    n = 0
    try:
        for root, _ds, fs in os.walk(d):
            for f in fs:
                try:
                    n += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return n


def _zip_of_batch(name):
    """批次目录 20261001_221324_截图 → 对应的压缩包路径"""
    return os.path.join(BASE, '有人云巡站截图_{}.zip'.format(name[:-len('_截图')]))


def _batch_rows():
    """列出工具目录下的往期截图批次（含大小 / 张数 / 对应压缩包）"""
    rows = []
    try:
        names = os.listdir(BASE)
    except Exception:
        names = []
    for n in names:
        p = os.path.join(BASE, n)
        if n.startswith('_del_'):
            # 之前"改名兜底"的残壳（OneDrive 锁目录期间挪出来的）：起后台线程慢慢清，
            # 不在请求里同步删——OneDrive 释放句柄要几分钟，同步删会拖死弹窗
            if os.path.isdir(p):
                R.purge_background(p)
            continue
        if not (BATCH_PAT.match(n) and os.path.isdir(p)):
            continue
        zp = _zip_of_batch(n)
        zsz = os.path.getsize(zp) if os.path.isfile(zp) else 0
        rows.append({
            'name': n,
            'png': R.count_png(p),
            'size': _dir_size(p),
            'zip': os.path.basename(zp) if zsz else '',
            'zip_size': zsz,
        })
    # 孤立压缩包：旧版"跑完自动删上一批目录、只留压缩包"造出来的残局——
    # 批次目录没了、zip 还在。列表如果只认目录，这些包在界面上永远看不到、
    # 也就永远删不掉，所以一并列出来（orphan 标记，前端会说明来由）。
    seen = set(r['name'] for r in rows)
    for n in names:
        m = ZIP_PAT.match(n)
        if not m:
            continue
        bname = m.group(1) + '_截图'
        if bname in seen:
            continue
        zp = os.path.join(BASE, n)
        try:
            zsz = os.path.getsize(zp)
        except OSError:
            continue
        rows.append({
            'name': bname,
            'png': 0,
            'size': 0,
            'zip': n,
            'zip_size': zsz,
            'orphan': True,
        })
    rows.sort(key=lambda r: r['name'], reverse=True)
    return rows


def _rm_file_retry(p, attempts=5, wait=0.8):
    """删文件（OneDrive 会短暂占用），失败重试"""
    for i in range(attempts):
        try:
            os.chmod(p, 0o666)
        except Exception:
            pass
        try:
            os.remove(p)
            return True
        except Exception:
            if i < attempts - 1:
                time.sleep(wait)
    return False


# ---------- 历史数据（本地监控记录）：查看 / 清理 ----------
# 目录与文件名前缀跟 watch_engine 保持一致，避免两处各写一份将来漂移。
HIST_DIR = getattr(_WATCH, 'HIST_DIR', '历史数据') if _WATCH else '历史数据'
REC_PREFIX = getattr(_WATCH, 'REC_PREFIX', '监控记录_') if _WATCH else '监控记录_'
# 允许可选的主机名后缀：监控记录_YYYY-MM.csv 或 监控记录_YYYY-MM-<host>.csv
HIST_PAT = re.compile(re.escape(REC_PREFIX) + r'(\d{4})-(\d{2})(?:-[^.]+)?\.csv$')


def _hist_dir():
    return os.path.join(BASE, HIST_DIR)


def _hist_cur_name():
    """当月文件名（正在被巡检追加的那个）"""
    return '{}{}.csv'.format(REC_PREFIX, datetime.now().strftime('%Y-%m'))


def _hist_files():
    """列出巡检自动累积的「监控记录_YYYY-MM.csv」。

    只认这个前缀：用户手动点「保存到本地」产生的是「历史数据_YYYY-MM.csv」，
    属于主动留档，不该出现在清理列表里，更不该被删。"""
    rows = []
    d = _hist_dir()
    cur = datetime.now().strftime('%Y-%m')
    try:
        names = sorted(os.listdir(d))
    except Exception:
        names = []
    for n in names:
        m = HIST_PAT.match(n)
        if not m:
            continue
        p = os.path.join(d, n)
        if not os.path.isfile(p):
            continue
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        mon = '{}-{}'.format(m.group(1), m.group(2))
        first, last, cnt = '', '', 0
        try:
            with open(p, 'r', encoding='utf-8-sig', newline='') as f:
                rdr = csv.reader(f)
                next(rdr, None)                       # 表头
                for row in rdr:
                    if not row:
                        continue
                    cnt += 1
                    if not first:
                        first = row[0]
                    last = row[0]
        except Exception:
            pass
        rows.append({'name': n, 'mon': mon, 'size': sz, 'rows': cnt,
                     'first': first, 'last': last, 'current': mon == cur})
    rows.sort(key=lambda r: r['name'], reverse=True)
    return rows


def _hist_trim(path, days):
    """把某个监控记录 CSV 裁成「只留最近 days 天」的数据行。

    表头与 BOM 原样保留；先写同目录临时文件再 os.replace 替换，
    中途任何异常都不会毁掉原文件。返回 (保留行数, 删除行数)。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    kept = dropped = 0
    d = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.trim_', suffix='.csv.tmp')
    os.close(fd)
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as fi, \
                open(tmp, 'w', encoding='utf-8-sig', newline='') as fo:
            rdr = csv.reader(fi)
            w = csv.writer(fo)
            header = next(rdr, None)
            w.writerow(header or ['时间', '账号', '设备名称', '设备编号', '变量', '值'])
            for row in rdr:
                if not row:
                    continue
                if row[0] >= cutoff:
                    w.writerow(row)
                    kept += 1
                else:
                    dropped += 1
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return kept, dropped


def _acct_dir(batch, aname):
    """批次目录下某账号的子目录。
    子目录名是 run_usrcloud 的 safe_name(账号名)（去掉空白和 ?（）():: 等），
    账号名里带空格时（如「线路甲 包银」）目录名会是「线路甲包银」，不能直接拼串，
    否则新加的带空格账号会整批显示"未巡/无数据/暂无截图"。
    依次尝试：原名 / 清洗后同名 -> 各目录 巡站报告.json 里的 account_name。"""
    if not batch or not aname:
        return None
    try:
        dirs = [d for d in os.listdir(batch) if os.path.isdir(os.path.join(batch, d))]
    except Exception:
        return None
    want = R.safe_name(aname)
    for d in dirs:
        if d == aname or d == want:
            return os.path.join(batch, d)
    for d in dirs:
        try:
            j = json.load(open(os.path.join(batch, d, '巡站报告.json'), encoding='utf-8'))
        except Exception:
            continue
        nm = j.get('account_name') or ''
        if nm == aname or R.safe_name(nm) == want:
            return os.path.join(batch, d)
    return None


def _snapshot():
    """读最新批次下各账号的巡站报告"""
    b = _latest_batch()
    out = {}
    if not b:
        return out, None
    for d in os.listdir(b):
        rp = os.path.join(b, d, '巡站报告.json')
        if os.path.isfile(rp):
            try:
                j = json.load(open(rp, encoding='utf-8'))
                rec = {'report': j, 'dir': os.path.join(b, d)}
                out[j.get('account_name') or d] = rec
                out.setdefault(d, rec)        # 目录名（safe_name）也建一个索引，双保险
            except Exception:
                pass
    return out, b


def _safe_under(path):
    """只允许访问工具目录内的文件"""
    p = os.path.realpath(path)
    root = os.path.realpath(BASE)
    return p == root or p.startswith(root + os.sep)


def _bring_explorer_front(path):
    """把刚打开的资源管理器窗口拉到最前（os.startfile 打开的窗口常被压在后面）。
    EnumWindows 按 Z 序从顶到底返回，新开的文件夹窗口就是最上面的那个
    CabinetWClass；找不到也不报错——置顶失败不影响打开本身。"""
    try:
        import ctypes
        hwnd = []

        def _cb(h, _):
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.user32.GetClassNameW(h, buf, 260)
            if buf.value == 'CabinetWClass':      # 资源管理器窗口类名
                hwnd.append(h)
                return False                      # 找到最上面的就停
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd[0], 9)          # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd[0])
    except Exception:
        pass


# ---------------- 账号表（accounts.csv）管理 ----------------

def _read_rows():
    """读 accounts.csv，返回 (路径, 表头, 行列表)；缺列自动补齐"""
    import csv
    p = os.path.join(BASE, 'accounts.csv')
    if not os.path.isfile(p):
        R.ensure_accounts_file()
    with open(p, encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        fields = list(r.fieldnames or [])
        rows = [dict(x) for x in r]
    for c in ['名称', '账号', '密码', '平台ID', '启用', '站点过滤']:
        if c not in fields:
            fields.append(c)
    return p, fields, rows


def _write_rows(p, fields, rows):
    import csv
    with open(p, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in fields})


def _row_enabled(r):
    return ((r.get('启用') or '是').strip().lower()
            not in ('否', 'n', 'no', '0', 'false', ''))


# ---------------- 巡站执行 ----------------

def _runner(sel, device_nos):
    ST.update(running=True, error=None, results={}, wprog={}, acc_done=0,
              total=0, stage='', batch=None, zip=None, done=None, finished=None)
    ST['logs'] = []
    R._PAUSE_EVENT.set()
    try:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        batch = os.path.join(BASE, stamp + '_截图')
        os.makedirs(batch, exist_ok=True)
        ST['batch'] = batch
        print('批次目录: {}'.format(batch))

        args = SimpleNamespace(limit=0, workers=ST.get('workers') or R.WORKERS,
                               browser_mode=False, no_headless=False, multi=len(sel) > 1)
        total_ok = total_on = 0

        for i, cfg in enumerate(sel, 1):
            ST['stage'] = '账号 {}/{} · {}'.format(i, len(sel), cfg['name'])
            ST['wprog'] = {}
            nos = (device_nos or {}).get(cfg['name'])
            if nos is not None and not nos:
                print('[跳过] {} 未勾选任何站点'.format(cfg['name']))
                continue
            cfg2 = dict(cfg, device_nos=nos) if nos else cfg
            try:
                tok, uid = R.ensure_login(cfg2)
                want = set(nos) if nos else None
                kws = cfg2.get('station_kw') or []
                # 分母必须和 run_for_account 实际跑的范围一致：
                # 手工勾选 > 站点过滤；否则进度会显示 49/121 这种永远跑不满的数
                n_dev = len([d for d in R.get_devices(tok, uid)
                             if (want is None or d['no'] in want)
                             and (not kws or any(k in (d.get('name') or '')
                                                 for k in kws))])
                ST['total'] += n_dev
            except Exception:
                pass
            out_dir, ok = R.run_for_account(cfg2, args, batch)
            total_ok += ok
            ST['acc_done'] += ok
            rp = os.path.join(out_dir, '巡站报告.json')
            try:
                d = json.load(open(rp, encoding='utf-8'))
                ST['results'][cfg['name']] = {'report': d, 'dir': out_dir}
                total_on += (d.get('summary') or {}).get('online', 0)
            except Exception as e:
                print('[警告] 报告读取失败: {}'.format(e))

        if total_ok > 0:
            zp = os.path.join(BASE, '有人云巡站截图_{}.zip'.format(stamp))
            R.make_zip(batch, zp)
            ST['zip'] = zp
            print('压缩包: {}'.format(zp))

        # 往期批次不再自动清理：以前每跑完一次就删掉上一次的截图目录，现在
        # 界面已有「清理往期」入口，删不删由用户自己决定，程序不擅自删数据。
        try:
            n_old = len([e for e in os.listdir(BASE)
                         if BATCH_PAT.match(e)
                         and os.path.isdir(os.path.join(BASE, e))])
        except Exception:
            n_old = 0
        print('[清理] 往期批次已全部保留（目录内现有 {} 个批次）。'
              '如需删除请在界面点「清理往期」。'.format(n_old))

        print('全部完成。')
        ST['done'] = (total_ok, total_on)
    except Exception as e:
        ST['error'] = repr(e)[:400]
        print('[错误] {}'.format(ST['error']))
    finally:
        ST['running'] = False
        ST['stage'] = ''
        R._PAUSE_EVENT.set()
        ST['finished'] = time.time()


# ---------------- HTTP ----------------

MIME = {'.html': 'text/html; charset=utf-8',
        '.css': 'text/css; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
        '.ico': 'image/x-icon',
        '.woff2': 'font/woff2'}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass                                  # 不往控制台刷访问日志

    # ---------- 输出 ----------
    def _send(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8', code)

    def _err(self, msg, code=400):
        self._json({'ok': False, 'msg': msg}, code)

    def _static(self, rel):
        path = os.path.join(BASE, rel)
        if not _safe_under(path) or not os.path.isfile(path):
            self._send(b'not found', 'text/plain', 404)
            return
        ext = os.path.splitext(path)[1].lower()
        self._send(open(path, 'rb').read(), MIME.get(ext, 'application/octet-stream'))

    # ---------- 路由 ----------
    def do_GET(self):
        LAST_SEEN['t'] = time.time()
        u = urlsplit(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path in ('/', '/index.html'):
            self._static('ui/index.html')
            return
        if path.startswith('/ui/'):
            self._static(path[1:])
            return
        if path == '/shot':
            return self._shot(qs.get('p', [''])[0])
        if path == '/favicon.ico':
            self._send(_FAVICON, 'image/svg+xml')
            return

        if not path.startswith('/api/'):
            self._send(b'not found', 'text/plain', 404)
            return

        name = path[5:]
        try:
            if name == 'accounts':
                return self._accounts()
            if name == 'devices':
                return self._devices(qs.get('account', [''])[0],
                                     live=(qs.get('live', ['1'])[0] != '0'))
            if name == 'report':
                return self._report(qs.get('account', [''])[0])
            if name == 'accountsall':
                return self._accountsall()
            if name == 'watch':
                return self._watch()
            if name == 'watchdev':
                return self._watchdev(qs.get('account', [''])[0], qs.get('no', [''])[0])
            if name == 'watchhist':
                return self._watchhist(qs)
            if name == 'histfiles':
                return self._histfiles()
            if name == 'status':
                return self._status(int(qs.get('since', ['0'])[0]))
            if name == 'open':
                return self._open(qs.get('p', [''])[0])
            if name == 'openfolder':
                return self._open(qs.get('p', [''])[0])
            if name == 'hello':
                return self._json({'ok': True})
            if name == 'settings':
                return self._json({'ok': True, 'workers': ST['workers'],
                                   'notify_mode': ST.get('notify_mode', 'both')})
            if name == 'notifypoll':
                # Electron 壳每 5 秒轮询：有新异常/恢复事件时按设置做任务栏闪烁或系统通知
                try:
                    since = float(qs.get('since', ['0'])[0])
                except (TypeError, ValueError):
                    since = 0.0
                evs = WATCHER.drain_notify(since) if WATCHER else []
                nm = ST.get('notify_mode', 'both')
                return self._json({'ok': True, 'now': time.time(), 'events': evs,
                                   'flash': bool(evs) and nm in ('flash', 'both'),
                                   'toast': bool(evs) and nm in ('toast', 'both')})
            if name == 'notifyhealth':
                return self._json({'ok': True, 'health': ST.get('notify_health') or {},
                                   'notify_mode': ST.get('notify_mode', 'both')})
            if name == 'watchcatalog':
                if WATCHER is None:
                    return self._json({'ok': False, 'accounts': [], 'devices': [], 'points': []})
                return self._json(WATCHER.catalog())
            if name == 'batches':
                return self._batches()
        except Exception as e:
            return self._err('{}'.format(e), 500)
        self._err('unknown api', 404)

    def do_POST(self):
        LAST_SEEN['t'] = time.time()
        u = urlsplit(self.path)
        name = u.path[5:] if u.path.startswith('/api/') else ''
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n).decode('utf-8', 'ignore') if n else '{}'
        try:
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {}
        try:
            if name == 'run':
                return self._run(body)
            if name == 'pause':
                R._PAUSE_EVENT.clear()
                print('[暂停] 当前设备跑完后停住，点"继续"恢复。')
                return self._json({'ok': True})
            if name == 'resume':
                R._PAUSE_EVENT.set()
                print('[继续] 恢复运行。')
                return self._json({'ok': True})
            if name == 'closemode':
                m = str(body.get('mode') or '')
                if not set_close_mode(m):
                    return self._err('关闭方式无效，只支持 ask / tray / exit')
                print('[设置] 关闭按钮行为 = {}'.format(m))
                return self._json({'ok': True, 'mode': m})
            if name == 'quit':
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._json({'ok': True})
            if name == 'hello':
                return self._json({'ok': True})
            if name == 'settings':
                # 只更新请求里带到的字段（异常提醒页只传 notify_mode，不能误清其它设置）
                if 'workers' in body:
                    try:
                        w = int(body.get('workers') or 0)
                    except (TypeError, ValueError):
                        w = 0
                    if 1 <= w <= 8:
                        ST['workers'] = w
                nm = str(body.get('notify_mode') or '')
                if nm in ('off', 'flash', 'toast', 'both'):
                    ST['notify_mode'] = nm
                _save_settings()
                print('[设置] 并发线程={} · 异常提醒={}'.format(
                    ST['workers'], ST['notify_mode']))
                return self._json({'ok': True, 'workers': ST['workers'],
                                   'notify_mode': ST.get('notify_mode', 'both')})
            if name == 'closemode':
                return self._json({'ok': True, 'mode': get_close_mode()})
            if name == 'notifyhealth':
                # Electron 壳投递系统通知后回报结果：
                #   which=native（Electron 原生）/ ps（Windows PowerShell 直投）/ as（macOS AppleScript）
                which = str(body.get('which') or '')
                if which not in ('native', 'ps', 'as'):
                    return self._err('which 需为 native / ps / as')
                h = ST.setdefault('notify_health', {'native': None, 'ps': None, 'as': None, 'ts': 0.0})
                h.setdefault('as', None)
                h[which] = bool(body.get('ok'))
                h['ts'] = time.time()
                return self._json({'ok': True, 'health': h})
            if name == 'notifytest':
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                WATCHER.push_notify_test()
                print('[提醒] 已发送一条测试提醒（方式：{}）'.format(ST.get('notify_mode', 'both')))
                return self._json({'ok': True,
                                   'notify_mode': ST.get('notify_mode', 'both')})
            if name == 'addaccount':
                return self._addaccount(body)
            if name == 'batchdel':
                return self._batchdel(body)
            if name == 'histdel':
                return self._histdel(body)
            if name == 'histtrim':
                return self._histtrim(body)
            if name == 'toggleaccount':
                return self._toggleaccount(body)
            if name == 'editaccount':
                return self._editaccount(body)
            if name == 'delaccount':
                return self._delaccount(body)
            if name == 'watchscan':
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                if 'accounts' in body:
                    WATCHER.set_scope(body.get('accounts'))   # 点巡检时强制同步当前选择
                if not WATCHER.scan_async():
                    return self._json({'ok': False, 'msg': '正在巡检中，请稍候'})
                print('[监控] 手动触发一轮巡检')
                return self._json({'ok': True})
            if name == 'watchtoggle':
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                return self._json({'ok': True,
                                   'enabled': WATCHER.set_enabled(bool(body.get('enabled')))})
            if name == 'watchinterval':
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                return self._json({'ok': True,
                                   'interval': WATCHER.set_interval(body.get('interval'))})
            if name == 'watchrules':
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                if not WATCHER.save_rules(body.get('rules') or {}):
                    return self._err('规则保存失败')
                print('[监控] 监控规则已更新')
                return self._json({'ok': True, 'rules': WATCHER.rules})
            if name == 'watchscope':
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                names = WATCHER.set_scope(body.get('accounts'))
                return self._json({'ok': True, 'scope': names})
            if name == 'watchhistsave':
                return self._watchhistsave(body)
            if name == 'watchfetch':
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                acc = str(body.get('account') or '')
                no = str(body.get('no') or '')
                if not acc or not no:
                    return self._err('缺少 account 或 no 参数')
                try:
                    d = WATCHER.fetch_one(acc, no)
                except Exception as e:
                    return self._json({'ok': False,
                                       'msg': '获取失败: {}'.format(str(e)[:150])})
                return self._json({'ok': True, 'dev': d})
            if name == 'control':
                # 控制下发（写值 / 开关）：必须带 confirm=true（前端二次确认后才发）
                if WATCHER is None:
                    return self._err('监控引擎未启用')
                acc = str(body.get('account') or '')
                no = str(body.get('no') or '')
                pts = body.get('points') or []
                if not acc or not no or not pts:
                    return self._err('缺少 account / no / points 参数')
                if body.get('confirm') is not True:
                    return self._json({'ok': False,
                                       'msg': '缺少二次确认（confirm=true），已拒绝下发'})
                norm = []
                for p in pts:
                    try:
                        apid = int(p.get('dataPointId'))
                    except (TypeError, ValueError):
                        return self._json({'ok': False, 'msg': '变量 ID 非法'})
                    norm.append({'dataPointId': apid,
                                 'identifier': str(p.get('identifier') or ''),
                                 'value': str(p.get('value'))})
                try:
                    r = WATCHER.control_points(acc, no, norm)
                except Exception as e:
                    return self._json({'ok': False,
                                       'msg': '下发失败: {}'.format(str(e)[:150])})
                if r.get('ok'):
                    print('[控制] {} / {} 下发 {} 个变量: {}'.format(
                        acc, no, len(norm),
                        ', '.join('{}={}'.format(x['identifier'], x['value']) for x in norm)))
                return self._json(r)
        except Exception as e:
            return self._err('{}'.format(e), 500)
        self._err('unknown api', 404)

    # ---------- 各接口 ----------
    def _shot(self, rel):
        p = os.path.join(BASE, unquote(rel))
        if not _safe_under(p) or not os.path.isfile(p):
            self._send(b'not found', 'text/plain', 404)
            return
        self._send(open(p, 'rb').read(), 'image/png')

    def _accounts(self):
        try:
            accs = R.load_accounts()
        except SystemExit:
            accs = []
        except Exception as e:
            return self._err('读取 accounts.csv 失败: {}'.format(e), 500)
        snap, _ = _snapshot()
        out = []
        for a in accs:
            item = {'name': a['name'], 'account': a['account'],
                    'platform': a.get('platform_id') or 'W3rfV',
                    'kw': a.get('station_kw') or [],
                    'enabled': bool(a.get('enabled', True)),
                    'last': None, 'dir': None}
            rep = snap.get(a['name'])
            if rep:
                s = rep['report'].get('summary') or {}
                item['last'] = {'run_at': rep['report'].get('run_at'),
                                'total': s.get('total', 0), 'online': s.get('online', 0),
                                'stale': s.get('stale', 0), 'offline': s.get('offline', 0),
                                'blank': s.get('blank', 0)}
                item['dir'] = os.path.relpath(rep['dir'], BASE)
            elif WATCHER is not None:
                # 没有巡站截图批次时改用监控引擎的实时状态统计，
                # 否则左侧永远显示「未巡站」（监控明明已经跑出设备了）
                try:
                    live = WATCHER.device_summary(a['name']) or {}
                except Exception:
                    live = {}
                if live:
                    cnt = {'total': 0, 'online': 0, 'stale': 0,
                           'offline': 0, 'blank': 0}
                    for st in live.values():
                        cnt['total'] += 1
                        k = st.get('status')
                        if k in cnt:
                            cnt[k] += 1
                        else:
                            cnt['offline'] += 1
                    wst = WATCHER.status() or {}
                    ts = wst.get('last_run')
                    item['last'] = {
                        'run_at': (time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts))
                                   if ts else None),
                        'total': cnt['total'], 'online': cnt['online'],
                        'stale': cnt['stale'], 'offline': cnt['offline'],
                        'blank': cnt['blank'],
                        'src': 'live',
                    }
            out.append(item)
        self._json({'ok': True, 'accounts': out,
                    'batch': os.path.relpath(_latest_batch(), BASE) if _latest_batch() else None})

    def _accountsall(self):
        """全部账号行（含停用，不含密码），供账号管理页"""
        try:
            _p, _f, rows = _read_rows()
        except Exception as e:
            return self._err('读取 accounts.csv 失败: {}'.format(e), 500)
        out = []
        for r in rows:
            nm = (r.get('名称') or '').strip()
            if not nm or nm.startswith('#') or not (r.get('账号') or '').strip():
                continue
            out.append({'name': nm, 'account': (r.get('账号') or '').strip(),
                        'platform': (r.get('平台ID') or '').strip() or 'W3rfV',
                        'kw': (r.get('站点过滤') or '').strip(),
                        'has_pwd': bool((r.get('密码') or '').strip()),
                        'enabled': _row_enabled(r)})
        self._json({'ok': True, 'accounts': out})

    def _addaccount(self, b):
        name = (b.get('name') or '').strip()
        acc = (b.get('account') or '').strip()
        pwd = (b.get('password') or '').strip()
        plat = (b.get('platform') or '').strip() or 'W3rfV'
        kw = (b.get('kw') or '').strip()
        if not name or not acc or not pwd:
            return self._err('名称、账号、密码都不能为空')
        try:
            p, fields, rows = _read_rows()
        except Exception as e:
            return self._err('读取 accounts.csv 失败: {}'.format(e), 500)
        if any((r.get('名称') or '').strip() == name for r in rows):
            return self._err('已存在同名账号：{}'.format(name))
        row = {k: '' for k in fields}
        row.update({'名称': name, '账号': acc, '密码': pwd,
                    '平台ID': plat, '启用': '是', '站点过滤': kw})
        rows.append(row)
        _write_rows(p, fields, rows)
        print('[账号] 新增 {}（{}）'.format(name, acc))
        self._json({'ok': True})

    def _toggleaccount(self, b):
        name = (b.get('name') or '').strip()
        enable = bool(b.get('enabled'))
        try:
            p, fields, rows = _read_rows()
        except Exception as e:
            return self._err('读取 accounts.csv 失败: {}'.format(e), 500)
        hit = False
        for r in rows:
            if (r.get('名称') or '').strip() == name:
                r['启用'] = '是' if enable else '否'
                hit = True
        if not hit:
            return self._err('没找到账号: {}'.format(name))
        _write_rows(p, fields, rows)
        print('[账号] {} {}'.format(name, '启用' if enable else '停用'))
        self._json({'ok': True})

    def _drop_token_cache(self, pairs):
        """删掉指定 (名称, 账号) 组合对应的登录缓存文件（改名/删除/换号后旧缓存没用了）"""
        try:
            d = os.path.join(BASE, '登录缓存')
            if not os.path.isdir(d):
                return 0
            want = set()
            for nm, ac in pairs:
                if nm and ac:
                    want.add('app_login_{}.json'.format(R.safe_name(nm + '_' + ac)))
            n = 0
            for f in os.listdir(d):
                if f in want:
                    try:
                        os.remove(os.path.join(d, f))
                        n += 1
                    except Exception:
                        pass
            return n
        except Exception:
            return 0

    def _editaccount(self, b):
        """改账号信息。name_old 定位原行，name 为新名称（可改名）；密码留空表示不修改。"""
        old = (b.get('name_old') or '').strip()
        new_name = (b.get('name') or '').strip()
        acc = (b.get('account') or '').strip()
        pwd = (b.get('password') or '').strip()
        plat = (b.get('platform') or '').strip() or 'W3rfV'
        kw = (b.get('kw') or '').strip()
        if not old:
            return self._err('缺少原账号名称')
        if not new_name or not acc:
            return self._err('名称、账号都不能为空')
        try:
            p, fields, rows = _read_rows()
        except Exception as e:
            return self._err('读取 accounts.csv 失败: {}'.format(e), 500)
        hit = None
        for r in rows:
            if (r.get('名称') or '').strip() == old:
                hit = r
                break
        if hit is None:
            return self._err('没找到账号: {}'.format(old))
        if new_name != old and any((r.get('名称') or '').strip() == new_name for r in rows):
            return self._err('已存在同名账号：{}'.format(new_name))
        old_acc = (hit.get('账号') or '').strip()
        hit['名称'] = new_name
        hit['账号'] = acc
        if pwd:                      # 留空 = 不改密码
            hit['密码'] = pwd
        hit['平台ID'] = plat
        hit['站点过滤'] = kw
        _write_rows(p, fields, rows)
        # 改名或换手机号后，旧登录缓存对不上了，清掉（下次自动重新登录）
        dropped = 0
        if new_name != old or acc != old_acc:
            dropped = self._drop_token_cache([(old, old_acc), (new_name, old_acc),
                                              (old, acc), (new_name, acc)])
        print('[账号] 已修改 {} → {}（{}）{}'.format(
            old, new_name, acc, '，清理旧登录缓存 {} 个'.format(dropped) if dropped else ''))
        self._json({'ok': True, 'name': new_name, 'dropped_cache': dropped})

    def _delaccount(self, b):
        """从 accounts.csv 里删掉一行（含其登录缓存）"""
        name = (b.get('name') or '').strip()
        if not name:
            return self._err('缺少账号名称')
        try:
            p, fields, rows = _read_rows()
        except Exception as e:
            return self._err('读取 accounts.csv 失败: {}'.format(e), 500)
        keep, gone = [], None
        for r in rows:
            if (r.get('名称') or '').strip() == name:
                gone = r
                continue
            keep.append(r)
        if gone is None:
            return self._err('没找到账号: {}'.format(name))
        _write_rows(p, fields, keep)
        dropped = self._drop_token_cache([(name, (gone.get('账号') or '').strip())])
        print('[账号] 已删除 {}（{}）{}'.format(
            name, (gone.get('账号') or '').strip(),
            '，清理登录缓存 {} 个'.format(dropped) if dropped else ''))
        self._json({'ok': True, 'dropped_cache': dropped})

    def _attach_shots(self, aname, devs):
        """把最新批次中该账号的截图与巡站报告匹配到各设备。
        截图文件名形如 20261001_153909_林东接触网工区YS2410033-26.png（时间_时间戳_设备完整名称），
        平台的 no 是内部编号（0000049399000740），不出现在文件名里，必须按设备 name 匹配。
        匹配两级：全名（清洗后）→ 设备编号（名称里的 YSxxxxxxx-x，平台改名后仍能对上）。
        状态/数据年龄来自同目录 巡站报告.json（get_devices 接口不返回状态，不能凭空猜）。"""
        def codes(s):
            return re.findall(r'[A-Za-z]{2,}\d{6,}(?:-\d+)?', s or '')

        b = _latest_batch()
        if not b:
            return devs
        adir = _acct_dir(b, aname)
        if not adir:
            return devs
        rep_map = {}
        try:
            rp = os.path.join(adir, '巡站报告.json')
            if os.path.isfile(rp):
                meta = json.load(open(rp, encoding='utf-8'))
                for it in (meta.get('devices') or []):
                    nm = re.sub(r'[？（）)():：\s]+', '', it.get('name') or '')
                    rep_map[nm] = it
                    for c in codes(it.get('name')):
                        rep_map.setdefault(c, it)
        except Exception:
            rep_map = {}
        try:
            files = [f for f in os.listdir(adir) if f.lower().endswith('.png')]
        except Exception:
            files = []
        rel_dir = os.path.relpath(adir, BASE).replace('\\', '/')
        index = {}
        for f in files:
            stem = f[:-4]
            parts = stem.split('_', 2)
            if len(parts) == 3:
                # 同名取时间戳最新（前缀时间戳同长，字符串比较即时间序）
                if parts[2] not in index or stem > index[parts[2]]:
                    index[parts[2]] = stem
                for c in codes(parts[2]):
                    old = index.get(c)
                    if not old or stem > old:
                        index[c] = stem
        for d in devs:
            # 文件名里的设备名经过 safe_name 清洗（去 ？（）():: 和空白），查询侧做同样清洗
            nm = re.sub(r'[？（）)():：\s]+', '', d.get('name') or '')
            keys = [nm] + codes(d.get('name'))
            best = None
            for k in keys:
                if index.get(k):
                    best = index[k]
                    break
            if best:
                d['shot'] = rel_dir + '/' + best + '.png'
                t = best[:15]
                if len(t) == 15 and t[8] == '_':
                    d['shot_time'] = '{}-{}-{} {}:{}:{}'.format(
                        t[:4], t[4:6], t[6:8], t[9:11], t[11:13], t[13:15])
            it = None
            for k in keys:
                if rep_map.get(k):
                    it = rep_map[k]
                    break
            if it:
                st = {'online': '在线', 'stale': '数据陈旧',
                      'offline': '离线', 'blank': '采集异常'}.get(it.get('status'))
                if st:
                    d['status'] = st
                age = it.get('age')
                if not age:
                    m = re.search(r'数据(.+)$', it.get('preview') or '')
                    age = m.group(1) if m else None
                if age:
                    d['age'] = age
        return devs

    def _devices(self, aname, live=True):
        try:
            accs = R.load_accounts()
        except SystemExit:
            accs = []
        hit = [a for a in accs if a['name'] == aname]
        if not hit:
            return self._err('没找到账号: {}'.format(aname))
        try:
            tok, uid = R.ensure_login(hit[0])
            devs = R.get_devices(tok, uid)
        except Exception as e:
            return self._err('获取设备列表失败: {}'.format(e), 500)
        # 站点过滤对所有页面生效：不需要的设备根本不进列表
        kws = hit[0].get('station_kw') or []
        if kws:
            devs = [d for d in devs if any(k in (d.get('name') or '') for k in kws)]
        devs = self._attach_shots(aname, devs)
        # 监控引擎的实时状态优先于批次快照（快照只是"上次巡站那一刻"的真相）
        # 巡站页传 live=0 拿纯快照，和监控页彻底分开
        if live and WATCHER is not None:
            try:
                live = WATCHER.device_summary(aname)
            except Exception:
                live = {}
            for d in devs:
                s = live.get(d.get('no'))
                if not s:
                    continue
                d['status'] = s['status']
                d['age'] = R.fmt_age(s['age_min'])
                d['live'] = True
                d['watch_ts'] = s['ts']
                d['points'] = s.get('n_points', 0)
                d['anoms'] = [{'name': a.get('rule_name'), 'level': a.get('level'),
                               'val': a.get('val'), 'cond': a.get('cond'),
                               'since': a.get('since'), 'dur': a.get('duration_min')}
                              for a in (s.get('anoms') or [])]
            # 有异常的排前面（稳定排序，其余保持原序）
            devs.sort(key=lambda d: 0 if d.get('anoms') else 1)
        self._json({'ok': True, 'devices': devs})

    def _watch(self):
        """监控总览：巡检状态 + 异常清单 + 规则"""
        if WATCHER is None:
            return self._json({'ok': False, 'msg': '监控引擎未启用'})
        self._json(WATCHER.status())

    def _watchdev(self, aname, no):
        """某台设备的实时明细（点开设备时用，含全部变量实时值）"""
        if WATCHER is None:
            return self._err('监控引擎未启用')
        s = WATCHER.device_state(aname, no)
        if not s:
            return self._json({'ok': False,
                               'msg': '该设备还没有实时数据，等下一轮巡检，或点「立即获取」'})
        # 控制信息（是否可写 / 下发ID / identifier）按需补，前端据此渲染开关或输入框
        rwmap = {}
        try:
            rwmap = WATCHER.live_vals_rw(aname, no) or {}
        except Exception:
            rwmap = {}
        vals = []
        for k, v in (s.get('vals') or {}).items():
            x = rwmap.get(k) or {}
            vals.append({'name': k, 'v': v.get('v'), 't': v.get('t'),
                         'rw': x.get('rw', 0), 'id': x.get('id'),
                         'ident': x.get('ident', '')})
        self._json({'ok': True, 'status': s['status'], 'age': R.fmt_age(s['age_min']),
                    'newest': s.get('newest'), 'ts': s['ts'], 'n_points': s.get('n_points'),
                    'anoms': s.get('anoms') or [], 'vals': vals})

    def _watchhist(self, qs):
        """历史数据（折线图）：按需拉取，不参与周期巡检"""
        if WATCHER is None:
            return self._err('监控引擎未启用')
        acc = qs.get('account', [''])[0]
        no = qs.get('no', [''])[0]
        if not acc or not no:
            return self._err('缺少 account 或 no 参数')
        try:
            hours = int(qs.get('hours', ['24'])[0])
        except ValueError:
            hours = 24
        want = [v for v in (qs.get('vars', [''])[0] or '').split('|') if v]
        src = (qs.get('src', ['local'])[0] or 'local').lower()
        try:
            if src == 'cloud':
                self._json(WATCHER.history(acc, no, hours=hours, want=want or None))
            else:
                res = WATCHER.local_history(acc, no, hours=hours, want=want or None)
                if not any((res.get('series') or {}).values()):
                    res = {'ok': False,
                           'msg': '本地还没有这台设备的监控记录（每轮巡检自动累积，'
                                  '也可切到「云端历史」拉取）'}
                self._json(res)
        except Exception as e:
            self._err('历史数据获取失败: {}'.format(str(e)[:150]), 500)

    def _watchhistsave(self, body):
        """历史数据保存到本地（月度 CSV，按数据点月份分文件）"""
        if WATCHER is None:
            return self._err('监控引擎未启用')
        acc = str(body.get('account') or '')
        no = str(body.get('no') or '')
        if not acc or not no:
            return self._err('缺少 account 或 no 参数')
        try:
            hours = int(body.get('hours') or 24)
        except (TypeError, ValueError):
            hours = 24
        want = [str(v) for v in (body.get('vars') or []) if str(v)]
        try:
            self._json(WATCHER.save_history(acc, no, hours=hours,
                                            want=want or None))
        except Exception as e:
            self._err('历史数据保存失败: {}'.format(str(e)[:150]), 500)

    def _report(self, aname):
        snap, _ = _snapshot()
        rep = snap.get(aname)
        if not rep:
            return self._json({'ok': False, 'msg': '这个账号还没有巡站报告'})
        self._json({'ok': True, 'report': rep['report'],
                    'dir': os.path.relpath(rep['dir'], BASE)})

    def _run(self, body):
        if ST['running']:
            return self._err('已经在跑了')
        names = body.get('names') or []
        accs = R.load_accounts()
        sel = [a for a in accs if a['name'] in names]
        if not sel:
            return self._err('没有勾选任何账号')
        try:
            ST['workers'] = max(1, min(8, int(body.get('workers') or R.WORKERS)))
        except Exception:
            ST['workers'] = R.WORKERS
        threading.Thread(target=_runner, args=(sel, body.get('device_nos') or {}),
                         daemon=True).start()
        self._json({'ok': True})

    def _status(self, since):
        logs = ST['logs']
        done = ST['acc_done'] + sum(v[0] for v in ST['wprog'].values())
        res = {}
        for k, v in ST['results'].items():
            res[k] = {'report': v['report'], 'dir': os.path.relpath(v['dir'], BASE)}
        b = ST['batch'] or _latest_batch()   # 本会话没跑过时兜底取磁盘最新批次，前端靠它感知新截图
        self._json({'ok': True,
                    'running': ST['running'],
                    'paused': not R._PAUSE_EVENT.is_set(),
                    'logs': logs[since:] if since < len(logs) else [],
                    'n': len(logs),
                    'stage': ST['stage'],
                    'progress': {'done': done, 'total': ST['total']},
                    'results': res,
                    'error': ST['error'],
                    'batch': os.path.relpath(b, BASE) if b else None,
                    'zip': os.path.relpath(ST['zip'], BASE) if ST['zip'] else None,
                    'done': ST['done']})

    def _open(self, rel):
        p = os.path.join(BASE, unquote(rel))
        if not _safe_under(p) or not os.path.exists(p):
            return self._err('文件不存在')
        try:
            if os.name == 'nt':
                os.startfile(p)                     # noqa: S606
                _bring_explorer_front(p)            # 资源管理器窗口置顶
            else:
                import subprocess
                subprocess.Popen(['open', p])
        except Exception as e:
            return self._err('打开失败: {}'.format(e), 500)
        self._json({'ok': True})

    # ---------- 往期截图批次：查看 / 清理 ----------

    def _batches(self):
        return self._json({'ok': True, 'batches': _batch_rows(),
                           'running': bool(ST.get('running')),
                           'current': os.path.basename(ST.get('batch') or ''),
                           'latest': os.path.basename(_latest_batch() or '')})

    def _batchdel(self, body):
        """删除选中的往期批次目录，并同步删除对应的压缩包。
        只剩压缩包的"孤立包"（旧版自动清理留下的）也能单独删掉。"""
        if ST.get('running'):
            return self._err('正在截图，等本次跑完再清理')
        want = body.get('names') or []
        if not isinstance(want, list) or not want:
            return self._err('没有选择要删除的批次')
        cur = os.path.basename(ST.get('batch') or '')
        latest = os.path.basename(_latest_batch() or '')
        deleted, failed, freed = [], [], 0
        pending = 0                            # OneDrive 占用、转后台清理的批次数
        for name in want:
            name = str(name)
            if not BATCH_PAT.match(name):        # 只认批次目录名，杜绝路径穿越
                continue
            if name in (cur, latest):
                failed.append({'name': name, 'msg': '是当前/最新批次，跳过'})
                continue
            dp = os.path.join(BASE, name)
            zp = _zip_of_batch(name)
            has_dir = os.path.isdir(dp)
            has_zip = os.path.isfile(zp)
            if not has_dir and not has_zip:
                failed.append({'name': name, 'msg': '目录与压缩包都已不存在'})
                continue
            sz = _dir_size(dp) if has_dir else 0
            zsz = os.path.getsize(zp) if has_zip else 0
            ok = R._rmtree_retry(dp) if has_dir else True   # 清只读 + 重试（OneDrive 占用）
            renamed = has_dir and (R.LAST_RM_MODE == 'renamed')
            if ok and has_zip:
                ok = _rm_file_retry(zp)
            if ok:
                deleted.append(name)
                if renamed:
                    pending += 1
                freed += sz + zsz
                print('[清理] 已删除 {} {}，释放 {}'.format(
                    '批次 ' + name if has_dir else '孤立压缩包 ' + name.replace('_截图', ''),
                    '（{} 张）'.format(R.count_png(dp)) if has_dir else '',
                    _fmt_size(sz + zsz)))
            else:
                failed.append({'name': name, 'msg': '文件被占用（OneDrive 可能正在同步），稍后再试'
                               + (' [' + R.LAST_RM_ERROR + ']' if R.LAST_RM_ERROR else '')})
        return self._json({'ok': True, 'deleted': deleted, 'failed': failed,
                           'freed': freed, 'freed_text': _fmt_size(freed),
                           'pending': pending})

    # ---------- 历史数据：本地监控记录 查看 / 整删 / 裁剪 ----------

    def _histfiles(self):
        rows = _hist_files()
        total = sum(r['size'] for r in rows)
        cur = ''
        for r in rows:
            if r['current']:
                cur = r['name']
                break
        return self._json({'ok': True, 'files': rows, 'current': cur,
                           'total': total, 'total_text': _fmt_size(total)})

    def _histdel(self, body):
        """按月份整个删除监控记录 CSV（当月文件正在写入，不可删——可改用裁剪）"""
        want = body.get('names') or []
        if not isinstance(want, list) or not want:
            return self._err('没有选择要删除的文件')
        cur = _hist_cur_name()
        d = _hist_dir()
        deleted, failed, freed = [], [], 0
        for name in want:
            name = os.path.basename(str(name))
            if not HIST_PAT.match(name):            # 只认监控记录_YYYY-MM.csv，杜绝路径穿越
                continue
            if name == cur:
                failed.append({'name': name, 'msg': '当月文件正在写入，跳过（可用「裁剪」）'})
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                failed.append({'name': name, 'msg': '文件已不存在'})
                continue
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = 0
            if _rm_file_retry(p):
                deleted.append(name)
                freed += sz
                print('[清理] 已删除监控记录 {}，释放 {}'.format(name, _fmt_size(sz)))
            else:
                failed.append({'name': name, 'msg': '文件被占用（OneDrive 可能正在同步），稍后再试'})
        return self._json({'ok': True, 'deleted': deleted, 'failed': failed,
                           'freed': freed, 'freed_text': _fmt_size(freed)})

    def _histtrim(self, body):
        """裁剪某个监控记录 CSV：只保留最近 N 天的数据行（默认当月文件）"""
        try:
            days = int(body.get('days') or 30)
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(3650, days))
        name = os.path.basename(str(body.get('name') or '')) or _hist_cur_name()
        if not HIST_PAT.match(name):
            return self._err('文件名不合法')
        p = os.path.join(_hist_dir(), name)
        if not os.path.isfile(p):
            return self._err('文件不存在')
        if os.path.getsize(p) < 1:
            return self._json({'ok': True, 'kept': 0, 'dropped': 0, 'days': days,
                               'name': name, 'msg': '文件是空的，无需裁剪'})
        # 落盘发生在巡检线程里、且整轮持 _scan_lock：拿不到锁说明正在巡检，
        # 直接让用户等一轮，别在写文件的同时改写它（会丢数据）。
        if WATCHER is not None:
            if not WATCHER._scan_lock.acquire(blocking=False):
                return self._err('正在巡检（一轮约 2 分钟），跑完再裁剪，避免丢数据')
            try:
                kept, dropped = _hist_trim(p, days)
            except Exception as e:
                return self._err('裁剪失败: {}'.format(str(e)[:120]))
            finally:
                WATCHER._scan_lock.release()
        else:
            kept, dropped = _hist_trim(p, days)
        print('[清理] {} 裁剪到最近 {} 天：保留 {} 行，删除 {} 行'.format(
            name, days, kept, dropped))
        return self._json({'ok': True, 'kept': kept, 'dropped': dropped,
                           'days': days, 'name': name})


def _fmt_size(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('{:.0f} {}' if u == 'B' else '{:.1f} {}').format(n, u)
        n /= 1024.0


_FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="8" fill="#2F6BFF"/>'
    b'<circle cx="16" cy="16" r="9" fill="none" stroke="#fff" stroke-width="2"/>'
    b'<circle cx="16" cy="16" r="3" fill="#fff"/></svg>')


# ---------------- 启动 ----------------

def _watchdog(srv):
    """浏览器标签关掉后过一会儿自己退出，不留后台进程"""
    while True:
        time.sleep(3)
        if ST['running']:
            LAST_SEEN['t'] = time.time()
            continue
        if time.time() - LAST_SEEN['t'] > 20:
            try:
                srv.shutdown()
            except Exception:
                pass
            os._exit(0)


def _fallback(msg):
    """起不来就退回原来的 tkinter 界面"""
    try:
        open(LOG_FILE, 'w', encoding='utf-8').write(msg)
    except Exception:
        pass
    try:
        R.run_gui()
    except Exception:
        pass


def main():
    _load_settings()        # 恢复上次保存的设置（并发线程/保留历史批次）
    if not os.path.isdir(UI_DIR) or not os.path.isfile(os.path.join(UI_DIR, 'index.html')):
        _fallback('缺少 ui/index.html')
        return
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except Exception as e:
        _fallback('端口监听失败: {}'.format(e))
        return
    port = srv.server_address[1]
    url = 'http://127.0.0.1:{}/'.format(port)
    threading.Thread(target=_watchdog, args=(srv,), daemon=True).start()
    if WATCHER is not None:
        WATCHER.start()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)


def electron_main():
    """Electron 无头模式：不起系统浏览器、不自动退出，端口写入临时文件供壳读取。"""
    _load_settings()        # 恢复上次保存的设置（并发线程/保留历史批次）——Electron 路径此前漏掉，导致重启后设置回默认
    if not os.path.isdir(UI_DIR) or not os.path.isfile(os.path.join(UI_DIR, 'index.html')):
        print('缺少 ui/index.html')
        return
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except Exception as e:
        print('端口监听失败: {}'.format(e))
        return
    port = srv.server_address[1]
    try:
        open(os.path.join(tempfile.gettempdir(), 'usrcloud_patrol_port.txt'),
             'w', encoding='utf-8').write(str(port))
    except Exception:
        pass
    print('USRCLOUD backend listening on port', port)
    if WATCHER is not None:
        WATCHER.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == '__main__':
    if os.environ.get('USRCLOUD_ELECTRON') == '1':
        electron_main()
    else:
        try:
            main()
        except Exception as e:
            import traceback
            _fallback(traceback.format_exc())
