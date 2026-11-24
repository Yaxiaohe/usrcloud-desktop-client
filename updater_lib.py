# -*- coding: utf-8 -*-
"""
有人云巡站客户端 · 内置更新核心（只用标准库）
============================================================
职责：
  1. 每天一次 / 手动检查新版（GitHub Releases 或自定义清单地址）
  2. 带进度地下载更新包，校验 sha256
  3. 只覆盖白名单里的程序文件，覆盖前备份，账号 / 历史 / 缓存一律不碰
  4. 把进度和结果写进更新状态.json，供 /api/update/* 接口读

设计原则（用户反复强调的）：
  · 任何数值都要有出处：pct 由 done/total 算，文件数由白名单算，版本由 package.json 读。
  · 白名单而不是黑名单：新增数据文件时不会误覆盖。
  · 更新失败不能留下半成品：先下到临时目录、校验通过再动安装目录。
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen, build_opener, ProxyHandler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, '更新配置.json')
STATE_FILE = os.path.join(APP_DIR, '更新状态.json')
VERSION_FILE = os.path.join(APP_DIR, 'package.json')

DEFAULT_CONFIG = {
    'enabled': True,
    'channel': 'public',          # public / private（private 才允许多覆盖账号配置）
    'sourceUrl': '',              # 留空 = 走 GitHub Releases；填了就当清单直链（自定义清单地址）
    'repo': 'Yaxiaohe/usrcloud-desktop-client',
    'timeout': 20,
}

# 公开通道：允许覆盖的程序文件（与 updater_core.js 的 PUBLIC_FILES 一致）
PUBLIC_FILES = (
    'main.js', 'preload.js', 'updater_core.js', 'updater_lib.py', 'updater.js',
    'gpu_mode.js', 'package.json',
    'run_usrcloud.py', 'usr_control.py', 'varsort.py',
    'watch_engine.py', 'webui_server.py',
    'ui/index.html', 'build/icon.ico', 'build/icon.png',
    # macOS 专属资源：菜单栏托盘小图（含 Retina）与 Dock 大图标。
    # 不加这三条，Mac 版更新包在清单校验和安装校验两处都会被 is_allowed_file 拒掉。
    'build/tray.png', 'build/tray@2x.png', 'build/icon_mac_1024.png',
    '使用说明.txt', '更新记录.txt',
)
# 私密通道额外允许：账号表 + 用户配置 + 登录缓存（只放 json）
PRIVATE_FILES = (
    'accounts.csv', '监控规则.json', '变量排序.json', '窗口位置.json',
    '关闭方式.json', '渲染模式.json', '运行参数.json', '登录缓存',
)

VERSION_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')
SHA_RE = re.compile(r'^[a-fA-F0-9]{64}$')

# 进度：接口只读，install 线程写。简单字段用 GIL 足够，写盘时统一快照。
PROGRESS = {
    'running': False, 'phase': '', 'pct': -1,
    'done': 0, 'total': 0, 'text': '', 'ok': False,
    'error': '', 'needRestart': False, 'version': '',
}
_LOCK = threading.Lock()


def _set(**kw):
    with _LOCK:
        PROGRESS.update(kw)


# ---------------- 读写配置 / 状态 ----------------
def _read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else dict(default)
    except Exception:
        return dict(default)


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_read_json(CONFIG_FILE, {}))
    return cfg


def save_config(patch):
    cfg = read_config()
    for k, v in (patch or {}).items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
    _write_json(CONFIG_FILE, cfg)
    return cfg


def read_state():
    return _read_json(STATE_FILE, {})


def write_state(state):
    _write_json(STATE_FILE, state)


def current_version():
    try:
        with open(VERSION_FILE, 'r', encoding='utf-8') as f:
            return str(json.load(f).get('version') or '')
    except Exception:
        return ''


# 进程启动时加载的版本：用于判断「是不是已经重启到新版本」（不能用磁盘版本，
# 因为替换完成瞬间磁盘就是新版，而当前进程跑的还是旧代码）。
BOOT_VERSION = current_version()


def local_day(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime('%Y-%m-%d')


# ---------------- 纯函数（单独可测） ----------------
def parse_version(v):
    m = VERSION_RE.match(str(v or '').strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def compare_version(a, b):
    x, y = parse_version(a), parse_version(b)
    if not x or not y:
        return 0          # 脏版本当相等，宁可不更新
    return (x > y) - (x < y)


def is_newer(a, b):
    return compare_version(a, b) > 0


def safe_rel(p):
    s = str(p or '').replace('\\', '/')
    if not s or s.startswith('/') or re.match(r'^[A-Za-z]:', s):
        return None
    parts = s.split('/')
    for seg in parts:
        if not seg or seg in ('.', '..') or re.search(r'[<>:"|?*]', seg):
            return None
    return '/'.join(parts)


def is_allowed_file(rel, channel):
    s = safe_rel(rel)
    if not s:
        return False
    if s in PUBLIC_FILES:
        return True
    if channel == 'private':
        for p in PRIVATE_FILES:
            if s == p or s.startswith(p + '/'):
                return True
    return False


def validate_manifest(m, channel):
    if not isinstance(m, dict):
        return None
    ver = str(m.get('version') or '')
    if not parse_version(ver):
        return None
    p = m.get('patch') or {}
    if not isinstance(p, dict):
        p = {'file': str(m.get('patch') or ''), 'sha256': str(m.get('sha256') or '')}
    pf = safe_rel(p.get('file') or m.get('patchFile'))
    sha = str(p.get('sha256') or m.get('sha256') or '')
    if not pf or not SHA_RE.match(sha):
        return None
    ch = 'private' if (channel == 'private' or m.get('channel') == 'private') else 'public'
    files = m.get('files')
    if files is not None:
        if not isinstance(files, list):
            return None
        for it in files:
            rel = it if isinstance(it, str) else (it or {}).get('path')
            if not is_allowed_file(rel, ch):
                return None
    try:
        size = int(p.get('size') or m.get('size') or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        'version': ver, 'patchFile': pf, 'sha256': sha.lower(), 'size': size,
        'channel': ch, 'notes': str(m.get('notes') or ''),
        'files': files, 'patchUrl': str(p.get('url') or ''),
        # 备用源：主地址连不上时依次再试。域名只是入口不同（同一个清单源），
        # 一个走 IPv4、一个走 IPv6，谁通就用谁。
        'patchUrlBackup': str(p.get('urlBackup') or ''),
    }


def find_manifest_asset(release):
    assets = (release or {}).get('assets') or []
    if not isinstance(assets, list):
        return None
    for want in ('update.json', '版本信息.json'):
        for a in assets:
            if isinstance(a, dict) and a.get('name') == want and a.get('browser_download_url'):
                return a['browser_download_url']
    return None


def pct_of(done, total):
    d, t = float(done or 0), float(total or 0)
    if t <= 0:
        return -1
    return max(0, min(100, int(d * 100 / t)))


def needs_daily_check(state, now=None):
    last = str((state or {}).get('lastCheckDate') or '')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', last):
        return True
    return last != local_day(now)


# ---------------- 网络 ----------------
def _fetch(url, timeout=20):
    """返回 bytes。先按系统代理走；失败再裸连一次（代理解析不了某些域名时有用）。"""
    req = Request(url, headers={
        'User-Agent': 'usrcloud-patrol-updater',
        'Accept': 'application/vnd.github+json,application/json,*/*',
        'Cache-Control': 'no-cache',
    })
    last = None
    for opener in (None, build_opener(ProxyHandler({}))):
        try:
            with (opener or build_opener()).open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
    raise last


def _fetch_text(url, timeout=20):
    raw = _fetch(url, timeout)
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'ignore')


def _manifest_source(cfg):
    """返回 (清单URL, 补丁URL提取器所需信息)。自定义地址优先，其次 GitHub Releases。"""
    custom = str(cfg.get('sourceUrl') or '').strip()
    if custom:
        return custom, None
    repo = str(cfg.get('repo') or '').strip('/')
    if not repo:
        raise ValueError('更新配置里既没有 sourceUrl 也没有 repo')
    api = 'https://api.github.com/repos/' + repo + '/releases/latest'
    rel = json.loads(_fetch_text(api, int(cfg.get('timeout') or 20)))
    url = find_manifest_asset(rel)
    if not url:
        raise ValueError('最新版 Release 里没有 update.json / 版本信息.json')
    return url, rel


def _patch_url(manifest, manifest_url, release):
    if manifest.get('patchUrl'):
        return manifest['patchUrl']
    if release:
        for a in release.get('assets') or []:
            if isinstance(a, dict) and a.get('name') == manifest['patchFile']:
                return a.get('browser_download_url') or ''
    return urljoin(manifest_url, manifest['patchFile'])


# ---------------- 检查 ----------------
def check(manual=False):
    cfg = read_config()
    if not cfg.get('enabled', True):
        return {'ok': False, 'error': '更新功能已在 更新配置.json 里停用'}
    state = read_state()
    if not manual and not needs_daily_check(state):
        cached = dict(state.get('lastCheck') or {})
        run = BOOT_VERSION
        disk = current_version()
        # 缓存只能记「清单源上现在是哪个版本」，不能连「要不要更新」一起照抄：
        # 那份 hasUpdate 是当天早些时候算出来的，那时可能还跑着旧版本。
        # 典型现象：装完新版重启后进设置页走这条缓存 → 界面上「当前版本 1.5.18」
        # 旁边又冒出「立即更新」（缓存是 1.5.17 时算的 hasUpdate=True）。
        # 所以「要不要更新」一律拿已经到手的最高版本重新算，不联网、几乎不花时间。
        # 已经到手的最高版本：正在跑的 vs 磁盘上的（装完未重启时后者更新）。
        base = disk if is_newer(disk, run) else run
        if cached.get('version'):
            cached['hasUpdate'] = is_newer(cached['version'], base)
        cached.update({'ok': True, 'skipped': True, 'runVersion': run,
                       'current': disk, 'needRestart': need_restart(state)})
        return cached

    try:
        murl, rel = _manifest_source(cfg)
        raw = json.loads(_fetch_text(murl, int(cfg.get('timeout') or 20)))
    except Exception as e:
        return {'ok': False, 'error': '检查更新失败：' + str(e)}

    m = validate_manifest(raw, cfg.get('channel'))
    if not m:
        return {'ok': False, 'error': '更新清单格式不对或含不允许覆盖的文件'}
    m['patchUrl'] = _patch_url(m, murl, rel)
    cur = current_version()
    res = {
        'ok': True, 'skipped': False, 'hasUpdate': is_newer(m['version'], cur),
        'version': m['version'], 'current': cur, 'notes': m['notes'],
        'patchSize': m['size'], 'channel': m['channel'],
        'needRestart': need_restart(state),
        # 界面上「当前版本」用 running（正在跑的进程版本）；current 是磁盘版本，
        # 只负责判断「还用不用下载」。两者不等 = 已替换、待重启。
        'runVersion': BOOT_VERSION,
    }
    state['lastCheckDate'] = local_day()
    state['lastCheck'] = {
        'ok': True, 'hasUpdate': res['hasUpdate'], 'version': m['version'],
        'current': cur, 'notes': m['notes'], 'patchSize': m['size'],
        'channel': m['channel'], 'manifest': m,
        # 记下这次是拿哪个「正在跑的版本」比的：升级重启后能一眼看出缓存过期
        'runVersion': BOOT_VERSION,
    }
    write_state(state)
    return res


# ---------------- 下载 + 安装 ----------------
def _download(url, dst, total_hint, timeout):
    """下载更新包。和 _fetch 一样：先走系统代理，失败再裸连一次。

    老版本这里直接 urlopen，只走系统代理；代理解析不了域名时，检查能成功
    (_fetch 有裸连兜底)、下载却失败，而且错误只写内存不落盘，界面上像「点了没反应」。

    现在 url 可以是一串地址（主源 + 备用源）：逐个域名 ×（系统代理 / 裸连）试，
    前一个彻底失败才换下一个，全部失败才报错。备用地址有两个入口——一个 IPv4、
    一个 IPv6——家里没下发 IPv6 或者只走 IPv4 时，靠这个兜底才能装上。
    """
    urls = [u for u in ([url] if isinstance(url, str) else list(url or [])) if u]
    if not urls:
        raise ValueError('更新清单里没有可用的下载地址')
    last = None
    for idx, u in enumerate(urls):
        if idx:
            _set(phase='download', done=0, pct=0, total=total_hint,
                 text='主地址连不上，正在改用备用地址')
        req = Request(u, headers={'User-Agent': 'usrcloud-patrol-updater'})
        for opener in (None, build_opener(ProxyHandler({}))):
            try:
                with (opener or build_opener()).open(req, timeout=timeout) as r, open(dst, 'wb') as f:
                    total = int(r.headers.get('Content-Length') or total_hint or 0)
                    done = 0
                    _set(total=total, done=0, pct=pct_of(0, total), phase='download', text='正在下载更新包')
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        _set(done=done, total=total, pct=pct_of(done, total))
                return done
            except Exception as e:
                last = e
    raise last


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _download_verified(addrs, zpath, size, timeout_n, want):
    """按地址顺序下载 + 校验；校验不过说明这个地址给的不是新文件（CDN 旧缓存），
    把它挪到队尾换下一个重下。返回最后一次算出的 sha256，由调用方判等。

    地址轮转是为了「两个地址都能连、但其中一个发的是旧包」这种情况：
    只按连通性兜底是发现不了的，必须靠 sha256。
    """
    addrs = [a for a in addrs if a]
    got = ''
    for attempt in range(len(addrs)):
        _download(addrs, zpath, size, timeout_n)
        _set(phase='verify', pct=-1, text='正在校验更新包')
        got = _sha256(zpath)
        if got == want:
            return got
        if attempt + 1 < len(addrs):
            addrs = addrs[1:] + addrs[:1]
            _set(phase='download', pct=0, text='这个地址下到的包校验不对，换个地址重下')
    return got


def _install_worker():
    state = read_state()
    cached = dict(state.get('lastCheck') or {})
    m = cached.get('manifest')
    url = (m or {}).get('patchUrl') or ''
    backup = (m or {}).get('patchUrlBackup') or ''
    if not m or not url:
        msg = '没有可用的更新信息，请先点「检查更新」'
        _set(running=False, ok=False, error=msg, text='')
        state['lastInstall'] = {'ok': False, 'running': False, 'error': msg,
                                'at': time.time()}
        write_state(state)
        return
    # 安装结果必须落盘：以前只写内存 PROGRESS，进程一关就没了，
    # 事后打开「更新状态.json」只能看到检查结果，分不清是没点、下载失败还是替换失败。
    state['lastInstall'] = {'ok': False, 'running': True, 'phase': 'start',
                            'version': str(m.get('version') or ''), 'at': time.time()}
    write_state(state)
    tmp = tempfile.mkdtemp(prefix='usrcloud_upd_')
    try:
        zpath = os.path.join(tmp, m['patchFile'].replace('/', '_'))
        want = str(m['sha256']).lower()
        timeout_n = int(read_config().get('timeout') or 20)
        size = int(m.get('size') or 0)
        # 地址顺序：主源在前，备用源在后。连通性由 _download 内部兜底；
        # 这里再兜一层「能连上但给的是旧文件」——下载源放在 CDN 后面，
        # 同名文件更新后 CDN 可能还发旧缓存，sha256 一比对就知道，换地址重下一次。
        addrs = []
        for u in (url, backup):
            if u and u not in addrs:
                addrs.append(u)
        got = _download_verified(addrs, zpath, size, timeout_n, want)
        if got != want:
            raise ValueError('更新包校验不通过（sha256 不一致），未改动任何文件')

        # 解压前先看清单：任何一个成员路径不合法就整个拒绝，不写安装目录
        with zipfile.ZipFile(zpath) as z:
            members = []
            for info in z.infolist():
                if info.is_dir():
                    continue
                rel = safe_rel(info.filename)
                if not rel or not is_allowed_file(rel, m['channel']):
                    raise ValueError('更新包里有不允许覆盖的文件：' + str(info.filename))
                members.append((rel, info))
            total = len(members)
            _set(phase='extract', pct=-1, done=0, total=total, text='正在解出更新文件')
            stage = os.path.join(tmp, 'new')
            for i, (rel, info) in enumerate(members, 1):
                dst = os.path.join(stage, rel.replace('/', os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(info) as src, open(dst, 'wb') as out:
                    shutil.copyfileobj(src, out)
                _set(done=i, total=total, pct=pct_of(i, total))

        # 备份原文件：同名相对路径，出问题照原路径拷回即可
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = os.path.join(APP_DIR, '_更新前备份_' + stamp)
        _set(phase='backup', pct=-1, done=0, total=total, text='正在备份原文件')
        for i, (rel, _info) in enumerate(members, 1):
            src = os.path.join(APP_DIR, rel.replace('/', os.sep))
            if os.path.isfile(src):
                dst = os.path.join(backup, rel.replace('/', os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            _set(done=i, total=total, pct=pct_of(i, total))

        _set(phase='apply', pct=-1, done=0, total=total, text='正在替换程序文件')
        for i, (rel, _info) in enumerate(members, 1):
            src = os.path.join(stage, rel.replace('/', os.sep))
            dst = os.path.join(APP_DIR, rel.replace('/', os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            _set(done=i, total=total, pct=pct_of(i, total))

        state['lastCheckDate'] = local_day()
        state['needRestart'] = True
        state['appliedVersion'] = m['version']
        state['appliedAt'] = time.time()
        state['backupDir'] = backup
        state['lastInstall'] = {'ok': True, 'running': False, 'version': m['version'],
                                'files': total, 'backupDir': backup, 'at': time.time()}
        write_state(state)
        _set(running=False, ok=True, pct=100, needRestart=True,
             version=m['version'], text='更新完成，请重启程序')
        print('[更新] 已替换到 v{}，备份在 {}'.format(m['version'], backup))
    except Exception as e:
        _set(running=False, ok=False, error=str(e), text='更新失败')
        try:
            st = read_state()
            li = dict(st.get('lastInstall') or {})
            li.update({'ok': False, 'running': False, 'error': str(e), 'at': time.time()})
            st['lastInstall'] = li
            write_state(st)
        except Exception:
            pass
        print('[更新] 失败：{}'.format(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install():
    if PROGRESS.get('running'):
        return {'ok': False, 'error': '已有更新任务在跑'}
    _set(running=True, ok=False, error='', pct=-1, done=0, total=0,
         phase='start', text='准备更新', needRestart=False)
    threading.Thread(target=_install_worker, daemon=True).start()
    return {'ok': True}


def need_restart(state=None):
    """要不要重启：看「装上的版本」是不是当前进程正在跑的那个。

    落盘的 needRestart 只是安装完成时留下的记录，以前只有 status() 顺手清它，
    check() 却直接把这个过期值报给界面 —— 于是重启到新版之后，界面还在喊重启，
    按钮也一直挂着。这里统一现算，任何入口拿到的结论都一致。
    """
    st = read_state() if state is None else state
    if not st.get('needRestart'):
        return False
    applied = str(st.get('appliedVersion') or '')
    if applied and applied == BOOT_VERSION:
        # 进程已经跑在这个版本上，说明重启过了：把过期标志落盘清掉
        st['needRestart'] = False
        write_state(st)
        return False
    return True


def status():
    s = dict(PROGRESS)
    st = read_state()
    applied = str(st.get('appliedVersion') or '')
    s['needRestart'] = need_restart(st)
    s['appliedVersion'] = applied
    s['current'] = current_version()
    # 「当前版本」要给人看到的是**正在跑的**版本，不是磁盘上的版本。
    # 替换完还没重启时两者不一样：磁盘已是新版、进程还是旧代码，
    # 这时候显示磁盘版本会出现「当前版本 1.5.6 · 最新版 1.5.6」却还提示
    # 「已替换完成，请重启」的自相矛盾。
    s['runVersion'] = BOOT_VERSION   # 注意：不能叫 running，那是「安装线程在跑」的意思
    s['disk'] = current_version()
    return s


def clear_restart_flag():
    st = read_state()
    st['needRestart'] = False
    write_state(st)
