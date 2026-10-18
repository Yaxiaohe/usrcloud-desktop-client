# -*- coding: utf-8 -*-
"""
有人云巡站客户端 · 实时监控引擎
================================================================
在低频（默认 15 分钟）下轮询有人云接口，维护设备实时状态，按可自定义
规则判定异常。设计第一原则：**绝不触发平台风控**。

防拉黑措施（全部可调）：
  1) 令牌桶限速：平均 ≤3 请求/秒、突发 ≤5 —— 一轮集中跑完，其余时间完全静默
  2) 轮次间隔带 ±60 秒抖动，不做整点节拍
  3) 设备列表缓存 1 小时（列表几乎不变，省下每轮的重复请求）
  4) 并发压到 3（实测满并发 4 也只有 4.5 请求/秒，限速后更平缓）
  5) 连续失败指数退避；识别到 429/403 立即暂停 30 分钟并明确提示
  6) 程序关闭即停，不驻留后台
  7) 随时可暂停 / 改频率

告警策略（关键）：
  以**状态突变**为主：只在 正常 → 异常 时产生"新告警"，异常持续只累加时长，
  恢复时记"已恢复"。另可切「每次巡检都提醒」，按设定间隔对仍在命中的项重复推送。
  首轮（baseline_done=False）与后续轮次一视同仁：本轮命中即推送，不再"只建基线"。
"""
import csv
import json
import os
import re
import random
import threading
import time
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import run_usrcloud as R
import usr_control          # 控制下发（MQTT over WebSocket，见 usr_control.py）

VERSION = 1
RULES_FILE = '监控规则.json'
HIST_DIR = '历史数据'           # 历史数据本地留档目录（每月一个 CSV）
REC_PREFIX = '监控记录_'        # 巡检自动落盘的文件名前缀（区别于手动保存的）
try:
    _HOSTNAME = socket.gethostname() or ''
except Exception:
    _HOSTNAME = ''
# 多机同目录时按主机名区分（对齐 Mac 版现状；不带后缀则与 Mac 版命名不一致、两台 Windows 同目录会冲突）
REC_SUFFIX = ('-' + _HOSTNAME) if _HOSTNAME else ''

DEFAULT_INTERVAL = 900          # 15 分钟
RATE_PER_SEC = 3.0              # 令牌桶平均速率（请求/秒）
RATE_BURST = 5                  # 令牌桶突发上限
CONCURRENCY = 3                 # 巡检并发数（默认值；设置页的「并发线程」可覆盖，见 set_concurrency）
LIST_TTL = 3600                 # 设备列表缓存时长（秒）
BACKOFF_429 = 1800              # 触发风控后的暂停时长（秒）
MAX_BACKOFF = 3600              # 连续失败的最大退避（秒）

STATUS_LEVEL = {'离线': 'critical', '数据陈旧': 'critical',
                '采集异常': 'warn', '在线': 'ok'}

# 状态显示文本必须与 webui_server._attach_shots / 前端 renderDevices 保持一致
# （前端用 '在线'/'数据陈旧' 做字符串比较，不能带 ✓○✗ 之类的装饰符）
ST_TEXT = {'online': '在线', 'stale': '数据陈旧',
           'offline': '离线', 'blank': '采集异常'}


def _wmatch(pat, s):
    """规则里的账号/设备名/变量名匹配。返回 True/False。

    为什么要放宽：设备返回的变量名是「供水温度T1」这种带编码的机器名，
    而人写规则时习惯只写「供水温度」四个字。原来只做「归一化后完全相等」，
    等于要求用户去背设备编码 —— 而且匹配不上**不报错**，
    只表现为「规则永远不触发」，是最难查的一类故障。

    规则只有两条，都按字面来，**不做同义词猜测**：
      1. 精确 / 通配：完全相等，或 * 通配（原行为，一字不改）
      2. 双向包含：去掉 * 后的词，与实际变量名互为子串
         · 写「供水温度」→ 命中「供水温度T1」  ✓
         · 写「回水温度」→ **不**命中「供水温度T1」（互不为子串）✓

    长度下限 2 是刻意设的：单字「温」会同时命中所有温度类变量，
    那是错配而不是匹配，比匹配失败更危险。
    """
    if not pat or pat == '*':
        return True
    pat, s = str(pat), str(s or '')
    # 1) 精确 / 通配（保持原语义）
    rx = re.escape(pat).replace(r'\*', '.*')
    if re.fullmatch(rx, s) is not None:
        return True
    # 2) 双向包含
    core = pat.replace('*', '').strip()
    if not core:
        return True                     # pat 形如「**」，上面已处理，这里兜底
    if len(core) < 2:
        return False                    # 单字不做宽松匹配，避免错配
    return core in s or s in core


def _to_float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# 阈值规则支持的条件（between = 区间内：大于左值且小于右值，两端都不含）
OPS_SINGLE = ('<', '>', '<=', '>=', '=', '!=')
OPS_ALL = OPS_SINGLE + ('between',)


def _range_of(rule):
    """取区间条件的 (左值, 右值)，按大小排好序；缺值或非法返回 (None, None)。
    用户左右两个框填反了不算错，这里自动纠正。"""
    a, b = _to_float(rule.get('value')), _to_float(rule.get('value2'))
    if a is None or b is None:
        return None, None
    return (a, b) if a <= b else (b, a)


def _rule_cond(point_name, rule):
    """规则命中后写进异常列表的条件文案。设备读回来的只有纯数字，不拼单位。"""
    op = rule.get('op')
    if op == 'between':
        a, b = rule.get('value'), rule.get('value2')
        fa, fb = _to_float(a), _to_float(b)
        if fa is not None and fb is not None and fa > fb:
            a, b = b, a
        return '{} > {} 且 < {}'.format(point_name, a, b)
    return '{} {} {}'.format(point_name, op, rule.get('value'))


class RateLimiter:
    """令牌桶限速器（跨线程安全）"""

    def __init__(self, rate=RATE_PER_SEC, burst=RATE_BURST):
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.ts = time.time()
        self.lock = threading.Lock()

    def acquire(self, stop=None):
        while True:
            with self.lock:
                now = time.time()
                self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                need = (1.0 - self.tokens) / self.rate
            if stop is not None and stop.is_set():
                return False
            time.sleep(min(need, 0.25))


class ApiError(Exception):
    """带风控标记的接口错误"""

    def __init__(self, msg, throttled=False):
        super().__init__(msg)
        self.throttled = throttled


class WatchEngine:

    def __init__(self, base, log=None, concurrency=None):
        self.base = base
        self.rules_path = os.path.join(base, RULES_FILE)
        self._log = log
        # 并发数：默认 CONCURRENCY；设置页改「并发线程」后由 set_concurrency 覆盖
        try:
            self.concurrency = max(1, min(8, int(concurrency)))
        except (TypeError, ValueError):
            self.concurrency = CONCURRENCY
        self._stop = threading.Event()
        self._thread = None
        self._scan_lock = threading.Lock()
        self._limiter = RateLimiter()
        self._rules_lock = threading.Lock()
        self._dev_lock = threading.Lock()
        self._rule_since = {}        # (rule_id, dev_key) -> 首次命中时间戳
        self._rule_diag = {}         # rule_id -> 本轮命中诊断（供前端显示「为什么没报」）
        self._last_push = {}         # anom_id -> 上次推送时间戳（repeat_mode=always 时按间隔限流）
        self._notify_lock = threading.Lock()
        self._notify_events = []     # 新异常/恢复通知事件（给 Electron 壳轮询做系统提醒）
        self._counters = {'requests': 0, 'reset_at': time.time()}

        self._devices = {}           # dev_key -> 设备实时状态
        self._prev_status = {}       # dev_key -> 上一轮状态文本
        self._prev_anom_ids = set()  # 上一轮异常 id 集合（判"新告警"用）
        self._anoms = []             # 当前异常清单
        self._new_anoms = []         # 本轮新出现的异常（id 列表）
        self._recovered = []         # 本轮恢复的（设备名 + 说明）
        self._list_cache = {}        # 账号名 -> (取到的时间, 设备列表)
        self._pts_cache = {}         # dev_key -> (取到的时间, 变量点定义)
        self._hist_cache = {}        # (acc,no,hours,want) -> (取到的时间, 响应)
        self._rec_val = {}          # (设备编号,变量) -> 已落盘的最新值（去重+心跳用，内存态）
        self._rec_ep = {}            # (设备编号,变量) -> 上次落盘墙钟时间(time.time()，心跳间隔判据)
        self._scope = None           # 巡检范围：None=全部账号，否则只巡这些账号（含站点过滤白名单）

        self.rules = self._load_rules()

        self.state = {
            'enabled': True,
            'interval': DEFAULT_INTERVAL,
            'running': False,
            'baseline_done': False,
            'last_run': None,
            'last_duration': None,
            'last_error': None,
            'next_run': None,
            'progress': [0, 0],
            'stage': '',
            'paused_reason': None,
            'requests_last_round': 0,
            'consecutive_fail': 0,
            'history': [],           # 最近几轮摘要
        }

    # ---------------- 日志 ----------------

    def _say(self, msg):
        line = '[监控] {}'.format(msg)
        if self._log:
            try:
                self._log(line)
                return
            except Exception:
                pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    # ---------------- 规则读写 ----------------

    def _default_rules(self):
        return {
            'version': VERSION,
            'stale_minutes': 30,      # 数据停更超过 N 分钟 -> 判为离线（客观规则）
            'flag_blank': True,       # 变量表为空 -> 采集异常
            'notify_builtin': False,  # 内置规则（离线/数据停更/采集异常）是否发系统通知；
                                      # 默认 False = 只推「自定义规则」命中的提醒，
                                      # 内置事件仍在数据监控页显示，只是不打扰。
            'notify_on_change_only': True,   # 只在状态突变时告警（推荐保持开启）
            'repeat_mode': 'change',  # change=只在状态变化时提醒 / always=每轮巡检命中都提醒
            'repeat_gap': 0,          # 同设备重复提醒最短间隔（分钟），0=不限制
            'rules': [],              # 自定义阈值规则，见 add_rule 说明
        }

    def _load_rules(self):
        if os.path.isfile(self.rules_path):
            try:
                with open(self.rules_path, encoding='utf-8') as f:
                    d = json.load(f)
                base = self._default_rules()
                base.update(d or {})
                if not isinstance(base.get('rules'), list):
                    base['rules'] = []
                return base
            except Exception as e:
                self._say('规则文件读取失败（用默认规则继续）: {}'.format(repr(e)[:100]))
        return self._default_rules()

    def save_rules(self, rules):
        """保存规则（前端提交的完整对象）"""
        base = self._default_rules()
        for k in ('stale_minutes', 'flag_blank', 'notify_builtin',
                  'notify_on_change_only', 'repeat_mode', 'repeat_gap', 'rules'):
            if k in (rules or {}):
                base[k] = rules[k]
        try:
            base['stale_minutes'] = max(1, int(base['stale_minutes']))
        except (TypeError, ValueError):
            base['stale_minutes'] = 30
        base['flag_blank'] = bool(base['flag_blank'])
        # 内置规则提醒开关：接受 true/false 或 "true"/"false"/"1"/"0" 等字符串写法
        nb = base.get('notify_builtin', False)
        if isinstance(nb, str):
            nb = nb.strip().lower() in ('1', 'true', 'yes', 'on')
        base['notify_builtin'] = bool(nb)
        base['notify_on_change_only'] = bool(base.get('notify_on_change_only', True))
        # 重复提醒：change=只在状态变化时提醒；always=每轮命中都提醒（配合 repeat_gap 限流）
        base['repeat_mode'] = 'always' if str(base.get('repeat_mode')) == 'always' else 'change'
        try:
            base['repeat_gap'] = max(0, int(base.get('repeat_gap') or 0))
        except (TypeError, ValueError):
            base['repeat_gap'] = 0
        clean = []
        for i, r in enumerate(base.get('rules') or []):
            if not isinstance(r, dict):
                continue
            clean.append({
                'id': str(r.get('id') or 'r{}'.format(i + 1)),
                'enabled': bool(r.get('enabled', True)),
                'name': str(r.get('name') or '未命名规则')[:40],
                'account': str(r.get('account') or '*'),
                'device': str(r.get('device') or '*'),
                'point': str(r.get('point') or '*'),
                'op': str(r.get('op') or '<') if str(r.get('op') or '<') in OPS_ALL else '<',
                'value': r.get('value'),
                'value2': r.get('value2'),
                'level': 'critical' if str(r.get('level')) == 'critical' else 'warn',
                'note': str(r.get('note') or '')[:80],
            })
        base['rules'] = clean
        self.rules = base
        try:
            with open(self.rules_path, 'w', encoding='utf-8') as f:
                json.dump(base, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._say('规则保存失败: {}'.format(repr(e)[:120]))
            return False
        return True

    # ---------------- 账号 / 设备 ----------------

    def _accounts(self):
        try:
            return R.load_accounts() or []
        except SystemExit:
            return []
        except Exception as e:
            self._say('账号表读取失败: {}'.format(repr(e)[:100]))
            return []

    def _in_scope(self, cfg, dev_name):
        """该设备是否属于这个账号的管辖范围（accounts.csv 的站点过滤）。
        没配关键词 = 全管。任务与告警都按这个白名单走。"""
        kws = cfg.get('station_kw') or []
        return (not kws) or any(k in (dev_name or '') for k in kws)

    def set_scope(self, acc_names):
        """设置巡检范围（账号白名单）。None/空 = 全部账号。"""
        if isinstance(acc_names, str):
            acc_names = [acc_names] if acc_names else None
        names = [str(x) for x in (acc_names or []) if str(x)]
        self._scope = names or None
        self._say('巡检范围更新：{}'.format('、'.join(names) if names else '全部账号'))
        return self._scope

    def _get_devices(self, cfg, tok, uid, force=False):
        """带 TTL 缓存的设备列表"""
        nm = cfg['name']
        hit = self._list_cache.get(nm)
        if hit and not force and time.time() - hit[0] < LIST_TTL:
            return hit[1]
        self._limiter.acquire(self._stop)
        r = R.api_json(R.DEVS_URL, {'pageNum': 1, 'pageSize': 500}, tok)
        self._counters['requests'] += 1
        if r.get('status') != 0:
            raise ApiError('设备列表失败: {}'.format(str(r)[:150]))
        lst = (r.get('data') or {}).get('cusdeviceResponseDTO') or []
        devs = [{'no': d.get('cusdeviceNo'), 'name': d.get('cusdeviceName')} for d in lst]
        self._list_cache[nm] = (time.time(), devs)
        return devs

    # ---------------- 单台取数（自带限速） ----------------

    def _api(self, url, payload, tok):
        """过限速 + 计数的接口调用"""
        self._limiter.acquire(self._stop)
        self._counters['requests'] += 1
        try:
            return R.api_json(url, payload, tok)
        except Exception as e:
            txt = repr(e)
            throttled = any(k in txt for k in ('429', '403', 'Too Many', '频繁', 'forbidden'))
            raise ApiError(txt[:160], throttled=throttled)

    def _fetch_device(self, dev_no, tok):
        """三步取数，返回 rows（与 run_usrcloud 一致）"""
        info = (self._api(R.INFO_URL, {'cusdeviceNo': dev_no}, tok) or {}).get('data') or {}
        pts = self._api(R.POINTS_URL, {
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
                last = self._api(R.LAST_URL, {'dataPoints': dps}, tok)
                for r in (last.get('data') or {}).get('list') or []:
                    last_by_rel[r.get('dataPointId')] = r

        rows = []
        for p in plst:
            unit = (p.get('unit') or '').strip()
            r = last_by_rel.get(p.get('dataPointRelId'))
            tm, cur = '-', '-'
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
        return rows, info

    # ---------------- 规则求值 ----------------

    def _match_rules(self, cfg, dev, rows):
        """对一台设备跑所有阈值规则，返回命中的异常列表。

        本轮读到的数据越过阈值就立即返回，不做「持续 N 分钟」等待——
        巡检间隔 15 分钟，任何 ≥15 的等待都等于把提醒推迟一整轮。

        同时把每条规则的「为什么没报」写进 self._rule_diag，供异常提醒页显示，
        避免用户写了规则却不知道是没匹配上变量名、还是数值没到。"""
        out = []
        now = time.time()
        dev_key = '{}|{}'.format(cfg['name'], dev['no'])
        for rule in self.rules.get('rules') or []:
            rid = rule.get('id')
            if not rule.get('enabled', True):
                continue
            d = self._rule_diag.get(rid)
            if d is None:
                d = self._rule_diag[rid] = {
                    'id': rid, 'name': rule.get('name') or '未命名规则',
                    'scanned': 0, 'point_hit': 0, 'hit': 0,
                    'near_val': None, 'near_point': '', 'near_dev': '',
                    'near_gap': None, 'near_read': '',
                    'reason': '', 'at': now,
                    'dev_points': None,   # 该设备实际有哪些变量名（命中 0 时提示用）
                }
            if not _wmatch(rule.get('account'), cfg['name']):
                continue
            if not _wmatch(rule.get('device'), dev['name']):
                continue
            d['scanned'] += 1
            op = rule.get('op')
            limit = _to_float(rule.get('value'))
            lo, hi = (None, None)
            if op == 'between':
                lo, hi = _range_of(rule)
                if lo is None:
                    d['reason'] = '阈值没填全'
                    continue          # 两个阈值没填全，这条等用户补上再说
            elif limit is None:
                d['reason'] = '阈值不是数字'
                continue
            hit_point, hit_val = None, None
            near = None            # 最接近阈值时的**差值**（用于诊断「还差多少」）
            near_pt = near_dev = near_read = ''
            # 记下这台设备实际有哪些变量名，规则一个都没命中时列给用户看。
            # 原来匹配不上只显示「变量名可能写错了」，用户根本不知道该填什么。
            if d['dev_points'] is None and rows:
                d['dev_points'] = [r[1] for r in rows if r[1]][:8]
            for r in rows:
                pname = r[1]
                if not _wmatch(rule.get('point'), pname):
                    continue
                d['point_hit'] += 1
                v = _to_float(r[4].split(' ')[0] if r[4] else '')
                if v is None:
                    continue
                if op == 'between':
                    ok = (lo < v < hi)
                    # 距离：落在区间外时取到最近边界外的距离
                    gap = (v - hi) if v >= hi else ((lo - v) if v <= lo else 0)
                else:
                    ok = ((op == '<' and v < limit) or (op == '>' and v > limit) or
                          (op == '<=' and v <= limit) or (op == '>=' and v >= limit) or
                          (op == '=' and v == limit) or (op == '!=' and v != limit))
                    gap = abs(v - limit)
                if ok:
                    hit_point, hit_val = pname, r[4]
                    break
                if near is None or gap < near:
                    near, near_pt, near_dev = gap, pname, dev['name']
                    near_read = r[4]
            if near is not None and d['near_gap'] is None:
                # near_gap = 差多少；near_read = 当时的实际读数。
                # 这两个必须分开：前端文案里「最接近的是 24.5」指的是读数，
                # 而「还差 24.5」指的是差值，混用会直接误导用户。
                d['near_gap'] = near
                d['near_val'] = near
                d['near_read'] = near_read
                d['near_point'] = near_pt
                d['near_dev'] = near_dev
            key = (rule['id'], dev_key)
            if hit_point is not None:
                # 本轮读到的数据已经越线 → 立即产生异常，不做「持续 N 分钟」等待。
                #
                # 为什么没有等待：巡检间隔 15 分钟，要求「连续命中 N 分钟」在N≥15 时
                # 必然要等到下一轮，等于把提醒硬生生推迟一整轮。
                # 而阈值本身已经是连续采集的读数，单轮越线就是真实故障。
                # 若担心数值抖动，用「区间内」条件把上下限留出余量即可，
                # 比拿时间做二次确认更直接。
                since = self._rule_since.setdefault(key, now)
                d['hit'] += 1
                d['reason'] = ''
                out.append({
                    'kind': 'threshold',
                    'rule_id': rule['id'],
                    'rule_name': rule.get('name') or '自定义规则',
                    'level': rule.get('level') or 'warn',
                    'point': hit_point,
                    'val': hit_val,
                    'cond': _rule_cond(hit_point, rule),
                    'since': since,
                    'note': rule.get('note') or '',
                })
            else:
                self._rule_since.pop(key, None)
        return out

    # ---------------- 一轮巡检 ----------------

    def _scan(self):
        """执行一轮巡检（同步阻塞，调用方负责并发保护）"""
        t0 = time.time()
        self._counters = {'requests': 0, 'reset_at': t0}
        self.state['running'] = True
        self.state['last_error'] = None
        self.state['paused_reason'] = None
        self._new_anoms = []
        self._recovered = []
        # 每轮重置规则诊断（前端在两轮之间还能看到上一轮结果，_rule_diag_at 记录时间）
        self._rule_diag = {}

        accounts = self._accounts()
        if not accounts:
            self.state['last_error'] = '账号表里没有可用账号'
            self.state['running'] = False
            return

        # 先把每个账号的设备清单拿到，算总数（用于进度显示）
        # 范围：一律按 accounts.csv 的站点过滤（白名单）收窄——
        #       「全部账号」= 三个账号白名单之和；选中具体账号 = 该账号白名单
        jobs, total, skipped = [], 0, []
        for cfg in accounts:
            if self._scope and cfg['name'] not in self._scope:
                continue
            try:
                tok, uid = R.ensure_login(cfg)
                devs = self._get_devices(cfg, tok, uid)
            except ApiError as e:
                if e.throttled:
                    self._pause_for_throttle(str(e))
                    self.state['running'] = False
                    return
                skipped.append('{}（{}）'.format(cfg['name'], str(e)[:60]))
                continue
            except Exception as e:
                skipped.append('{}（{}）'.format(cfg['name'], repr(e)[:60]))
                continue
            in_scope = [d for d in devs if self._in_scope(cfg, d.get('name'))]
            if len(in_scope) < len(devs):
                self._say('{}: 白名单内 {} / {} 台'.format(cfg['name'], len(in_scope), len(devs)))
            for d in in_scope:
                jobs.append((cfg, tok, d))
            total += len(in_scope)
            self._say('{}: 待巡检 {} 台'.format(cfg['name'], len(in_scope)))

        if skipped:
            self._say('跳过账号: {}'.format('; '.join(skipped)))
        if not jobs:
            self.state['last_error'] = '没有可巡检的设备'
            self.state['running'] = False
            return

        self.state['progress'] = [0, total]
        self._say('本轮开始：{} 台设备，间隔 {} 分钟，限速 {:.1f} 请求/秒'
                  .format(total, int(self.state['interval'] // 60), RATE_PER_SEC))

        done, errors, throttled = 0, 0, False
        results = []

        def work(cfg, tok, dev):
            rows, info = self._fetch_device(dev['no'], tok)
            return cfg, tok, dev, rows, info

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futs = {}
            for cfg, tok, d in jobs:
                if self._stop.is_set():
                    break
                futs[pool.submit(work, cfg, tok, d)] = d
            for f in as_completed(futs):
                done += 1
                self.state['progress'] = [done, total]
                try:
                    results.append(f.result())
                except ApiError as e:
                    errors += 1
                    if e.throttled:
                        throttled = True
                        self._say('！！平台拒绝服务（疑似风控）：{}'.format(str(e)[:120]))
                        break
                    if errors <= 3:
                        self._say('取数失败 {}: {}'.format(futs[f]['name'][:20], str(e)[:80]))
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        self._say('取数异常 {}: {}'.format(futs[f]['name'][:20], repr(e)[:80]))

        if throttled:
            self._pause_for_throttle('平台返回限流')
            self.state['running'] = False
            return

        # 汇总本轮结果
        res_by_key = {}
        fresh = {}
        try:
            want_min = int(self.rules.get('stale_minutes') or 30)
        except (TypeError, ValueError):
            want_min = 30
        for cfg, tok, dev, rows, info in results:
            key = '{}|{}'.format(cfg['name'], dev['no'])
            res_by_key[key] = rows
            st = R.judge_status(rows)
            age = R.data_age_minutes(rows)
            if st == 'online' and age is not None and age > want_min:
                st = 'stale'
            if st == 'blank' and not self.rules.get('flag_blank', True):
                st = 'offline'
            times = [r[3] for r in rows if r[3] and r[3] != '-']
            fresh[key] = {
                'acc': cfg['name'], 'no': dev['no'], 'name': dev['name'],
                'status': ST_TEXT.get(st, str(st)), 'age_min': age,
                'newest': max(times) if times else '-',
                'vals': {r[1]: {'v': r[4], 't': r[3]} for r in rows},
                'n_points': len(rows), 'ts': time.time(),
                'info': {'org': info.get('projectName'), 'addr': info.get('address')},
            }

        # 本轮采集结果自动落盘（历史记录页的本地数据源）
        self._record_rows(results)

        # 跨轮比较 -> 生成告警（核心：只在状态突变时算"新告警"）
        # 首轮（baseline_done=False）同样正常推送，不再有"先建基准、下一轮才提醒"这一套。
        # 用户明确要求：命中就提醒。多花一轮只为了"看个基线"没有意义 ——
        # 阈值判断本身就是基于本轮读数，单轮越线即真实故障。
        # first_time 现在只用于日志措辞，不再抑制任何推送。
        first_time = not self.state['baseline_done']
        now = time.time()
        anomalies = []
        for key, dev in fresh.items():
            cfg = next((c for c in accounts if c['name'] == dev['acc']), None)
            scoped = self._in_scope(cfg, dev['name']) if cfg else True
            st = dev['status']
            anoms = []
            if scoped and st in ('离线', '数据陈旧', '采集异常'):
                anoms.append({
                    'kind': ('offline' if st == '离线' else
                             ('blank' if st == '采集异常' else 'stale')),
                    'rule_id': '',
                    'rule_name': ('设备离线' if st == '离线' else
                                  ('采集异常' if st == '采集异常' else '数据停更')),
                    'level': STATUS_LEVEL.get(st, 'warn'),
                    'point': '', 'val': R.fmt_age(dev['age_min']),
                    'cond': '数据停更超过 {} 分钟'.format(want_min),
                    'since': self._rule_since.setdefault(('__stale__', key), now),
                    'note': '',
                })
            if scoped and cfg and res_by_key.get(key) is not None:
                anoms.extend(self._match_rules(cfg, dev, res_by_key[key]))
            prev = self._prev_status.get(key)
            if st == '在线':
                self._rule_since.pop(('__stale__', key), None)
                if prev and prev != '在线':
                    self._recovered.append({'acc': dev['acc'], 'name': dev['name'],
                                            'was': prev})
            dev['anoms'] = anoms
            for a in anoms:
                a['id'] = '{}|{}|{}'.format(key, a.get('rule_id') or a['kind'],
                                            a.get('point') or '')
                a['acc'] = dev['acc']
                a['dev_name'] = dev['name']
                a['dev_no'] = dev['no']
                a['duration_min'] = (now - a.get('since', now)) / 60.0
                # 首轮 _prev_anom_ids 为空，所以本轮全部命中项都是"新的"，会正常推送。
                a['is_new'] = a['id'] not in self._prev_anom_ids
                anomalies.append(a)
            self._prev_status[key] = st

        with self._dev_lock:
            self._devices.update(fresh)
        self._anoms = sorted(anomalies, key=lambda a: (a['level'] != 'critical',
                                                       -a.get('duration_min', 0)))
        self._prev_anom_ids = {a['id'] for a in anomalies}
        self._new_anoms = [a['id'] for a in anomalies if a['is_new']]
        # 新告警/恢复事件入队，Electron 壳轮询 /api/notifypoll 后做任务栏闪烁或系统通知。
        # 首轮也照常入队（不再等第二轮）。
        # 两种推送口径：
        #   change（默认）—— 只在「这条异常刚出现」时推一次，避免同一故障每 15 分钟重复打扰；
        #   always        —— 每轮巡检只要还在命中就再推一次（按 repeat_gap 限流），
        #                    用于「必须反复确认」的值班场景。
        always = str(self.rules.get('repeat_mode') or 'change') == 'always'
        try:
            gap_min = max(0, int(self.rules.get('repeat_gap') or 0))
        except (TypeError, ValueError):
            gap_min = 0
        # 内置规则（rule_id 为空 = 设备离线 / 数据停更 / 采集异常）是否也推系统通知。
        # 关闭时只推「自定义规则」命中的提醒；内置异常仍照常出现在数据监控页与异常清单，
        # 只是不弹通知、不打扰。恢复通知同样属于内置判定，一并受此开关控制。
        nb = self.rules.get('notify_builtin', True)
        if isinstance(nb, str):
            nb = nb.strip().lower() in ('1', 'true', 'yes', 'on')
        notify_builtin = bool(nb)
        with self._notify_lock:
            for a in self._anoms:
                if not a.get('rule_id') and not notify_builtin:
                    continue   # 内置规则命中，且用户选择了「不推送」
                if not (a.get('is_new') or always):
                    continue
                if not a.get('is_new') and gap_min > 0:
                    last = self._last_push.get(a['id'])
                    # 本轮之前已出现过，说明不是新异常；按间隔决定是否再推
                    if last is not None and (now - last) < gap_min * 60.0:
                        continue
                self._last_push[a['id']] = now
                self._notify_events.append({
                    't': now,
                    'level': a.get('level') or 'warn',
                    # 点击系统通知要跳到对应站点：带上账号/设备编号/设备名
                    'acc': a.get('acc') or '',
                    'no': a.get('dev_no') or '',
                    'name': a.get('dev_name') or '',
                    'title': '【{}】{} · {}'.format(
                        '严重' if a.get('level') == 'critical' else '警告',
                        a.get('dev_name') or '', a.get('rule_name') or ''),
                    'body': '{} {}'.format(a.get('acc') or '',
                                           a.get('cond') or '').strip(),
                })
            if notify_builtin:
                for rv in self._recovered:
                    self._notify_events.append({
                        't': now, 'level': 'ok',
                        'acc': rv.get('acc') or '',
                        'no': '',
                        'name': rv.get('name') or '',
                        'title': '【恢复】{}'.format(rv.get('name') or ''),
                        'body': '{} 由 {} 恢复在线'.format(rv.get('acc') or '', rv.get('was') or ''),
                    })
            del self._notify_events[:-200]

        self.state['baseline_done'] = True
        self.state['last_run'] = time.time()
        self.state['last_duration'] = time.time() - t0
        self.state['requests_last_round'] = self._counters['requests']
        self.state['progress'] = [total, total]
        if not errors:
            self.state['consecutive_fail'] = 0

        self._say('本轮完成：{}/{} 台成功（失败 {}），请求 {} 次，耗时 {:.0f} 秒'
                  .format(len(results), done, errors, self.state['requests_last_round'],
                          self.state['last_duration']))
        if first_time:
            self._say('首次巡检完成：当前命中 {} 条，已按设置推送'
                      '（此后状态突变继续提醒）'.format(len(self._anoms)))
        else:
            if self._new_anoms:
                self._say('★ 发现 {} 条新异常'.format(len(self._new_anoms)))
            if self._recovered:
                self._say('{} 台已恢复正常'.format(len(self._recovered)))

        self.state['history'].append({
            't': self.state['last_run'], 'total': total, 'ok': len(results),
            'errors': errors, 'anoms': len(self._anoms),
            'new': len(self._new_anoms), 'req': self.state['requests_last_round'],
            'dur': self.state['last_duration'],
        })
        del self.state['history'][:-20]
        self.state['running'] = False
        self.state['stage'] = ''

    def _pause_for_throttle(self, why):
        until = time.time() + BACKOFF_429
        self.state['paused_reason'] = '疑似触发平台限流，已暂停至 {}（{}）'.format(
            datetime.fromtimestamp(until).strftime('%H:%M'), why[:80])
        self.state['next_run'] = until
        self._say(self.state['paused_reason'])

    # ---------------- 后台线程 ----------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='watch-engine')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # 启动后先缓一下，别和界面加载、用户点击抢资源
        time.sleep(8)
        while not self._stop.is_set():
            if not self.state['enabled']:
                self.state['next_run'] = None
                if self._stop.wait(2):
                    return
                continue
            with self._scan_lock:
                try:
                    self._scan()
                except Exception as e:
                    self.state['last_error'] = repr(e)[:300]
                    self._say('巡检异常: {}'.format(repr(e)[:200]))
                    self.state['consecutive_fail'] += 1
                    self.state['running'] = False
            if self._stop.is_set():
                return
            # 计算下一轮
            base = self.state['interval']
            cf = self.state['consecutive_fail']
            if cf:
                back = min(MAX_BACKOFF, base * (2 ** min(cf, 4)))
                self._say('连续失败 {} 次，退避到 {} 分钟后再试'
                          .format(cf, int(back // 60)))
                base = max(base, back)
            delay = base + random.uniform(-60, 60)
            delay = max(30, delay)
            self.state['next_run'] = time.time() + delay
            self._stop.wait(delay)

    # ---------------- 对外接口 ----------------

    def scan_async(self):
        """立即巡检（异步，不阻塞请求）"""
        if self.state['running']:
            return False
        self.state['enabled'] = True
        self.state['paused_reason'] = None

        def run():
            with self._scan_lock:
                try:
                    self._scan()
                except Exception as e:
                    self.state['last_error'] = repr(e)[:300]
                    self.state['running'] = False
                    self._say('手动巡检异常: {}'.format(repr(e)[:200]))
        threading.Thread(target=run, daemon=True).start()
        return True

    def set_enabled(self, on):
        self.state['enabled'] = bool(on)
        if on:
            self.state['paused_reason'] = None
            if not self.state['baseline_done']:
                self.scan_async()
        return self.state['enabled']

    def set_interval(self, sec):
        try:
            sec = int(sec)
        except (TypeError, ValueError):
            return self.state['interval']
        sec = max(300, min(86400, sec))
        self.state['interval'] = sec
        return sec

    def set_concurrency(self, n):
        """设置页改「并发线程」时同步过来（巡站截图用的是同一份设置）。
        返回生效值；带进来的值不合法就保持原样。"""
        try:
            n = int(n)
        except (TypeError, ValueError):
            return self.concurrency
        self.concurrency = max(1, min(8, n))
        return self.concurrency

    def device_state(self, acc, no):
        return self._devices.get('{}|{}'.format(acc, no))

    def drain_notify(self, since):
        """返回 since 之后产生的通知事件（Electron 壳轮询用，事件保留最近 200 条）。"""
        with self._notify_lock:
            return [e for e in self._notify_events if e.get('t', 0) > since]

    def push_notify_test(self):
        """塞一条测试提醒事件，验证任务栏闪烁 / 系统通知链路是否通畅。
        test=True：外壳收到后即使用户正看着软件，也强制走真实的系统通知投递，
        否则“窗口聚焦时跳过系统通知”的设计会让测试按钮永远测不到该通道。"""
        now = time.time()
        with self._notify_lock:
            self._notify_events.append({
                't': now,
                'test': True,
                'title': '【测试】异常提醒通道正常',
                'body': '这是一条测试提醒，看到它说明当前提醒方式已生效。'})
            del self._notify_events[:-200]

    def catalog(self):
        """监控设备目录（账号/设备名/变量名清单），供规则编辑时的候选与实时匹配。

        ⚠ 变量名以前只从「已巡检到的变量值」收集：设备离线 / 数据陈旧时一个都收不到，
        规则页的下拉与「匹配到 N 个」提示就全是空的（用户实际反馈过）。
        现在补两条来源：
          ① 已巡到的设备与变量值（原有）
          ② **平台变量点定义**（_point_defs，1 小时缓存）——每个账号挑最多 2 台设备取样，
             同一套模板下变量名通用，因此不必逐台拉
        ③ 设备清单：还没巡检过时也先按账号拉一次（_get_devices 有 TTL 缓存），
           否则"设备名"候选在首轮巡检前也是空的
        """
        accs, devs, pts = [], [], set()
        cfgs = []
        try:                       # 账号名先从配置取（首轮巡检前也有）
            cfgs = self._accounts()
            for c in cfgs:
                if c['name'] not in accs:
                    accs.append(c['name'])
        except Exception:
            cfgs = []
        with self._dev_lock:
            items = list(self._devices.values())
        for d in items:
            if d['acc'] not in accs:
                accs.append(d['acc'])
            devs.append({'acc': d['acc'], 'name': d['name'], 'no': d['no']})
            for p in (d.get('vals') or {}):
                pts.add(str(p))

        picks = {}                 # 账号 -> [设备号...]（取样用）
        for d in devs:
            lst = picks.setdefault(d['acc'], [])
            if len(lst) < 2 and d.get('no'):
                lst.append(d['no'])
        for c in cfgs:
            if c['name'] in picks:
                continue
            try:
                tok, uid = R.ensure_login(c)
                ds = self._get_devices(c, tok, uid)
            except Exception:
                continue
            nos = []
            for d in ds:
                if not d.get('no'):
                    continue
                if not self._in_scope(c, d.get('name')):   # 只列巡检范围内（白名单）的设备
                    continue
                devs.append({'acc': c['name'], 'name': d.get('name'), 'no': d['no']})
                if len(nos) < 2:
                    nos.append(d['no'])
            picks[c['name']] = nos
        for acc, nos in picks.items():
            try:
                tok = self._token_for(acc)
            except Exception:
                continue
            for no in nos[:2]:
                try:
                    for p in (self._point_defs(acc, no, tok) or []):
                        nm = p.get('name')
                        if nm:
                            pts.add(str(nm))
                except Exception:
                    pass

        uniq = {}
        for d in devs:
            uniq['{}|{}'.format(d['acc'], d['no'])] = d
        return {'ok': True, 'accounts': sorted(accs),
                'devices': sorted(uniq.values(), key=lambda x: (x['acc'], x['name'])),
                'points': sorted(pts)}

    def status(self):
        with self._dev_lock:
            n_dev = len(self._devices)
        anoms = getattr(self, '_anoms', [])
        now = time.time()
        return {
            'ok': True,
            'enabled': self.state['enabled'],
            'running': self.state['running'],
            'interval': self.state['interval'],
            'baseline_done': self.state['baseline_done'],
            'last_run': self.state['last_run'],
            'last_run_text': (datetime.fromtimestamp(self.state['last_run'])
                              .strftime('%H:%M:%S') if self.state['last_run'] else None),
            'last_duration': self.state['last_duration'],
            'last_error': self.state['last_error'],
            'next_run': self.state['next_run'],
            'next_in': max(0, int(self.state['next_run'] - now)) if self.state['next_run'] else None,
            'progress': self.state['progress'],
            'stage': self.state['stage'],
            'paused_reason': self.state['paused_reason'],
            'requests_last_round': self.state['requests_last_round'],
            'device_count': n_dev,
            'scope': self._scope,
            'anomalies': anoms,
            'new_ids': self._new_anoms,
            'recovered': self._recovered,
            'history': self.state['history'][-10:],
            'rules': self.rules,
            'rule_diag': list(self._rule_diag.values()),
            'counters': self._counters,
        }

    def device_summary(self, acc):
        """某账号下所有设备的实时状态（供列表页覆盖快照）"""
        out = {}
        with self._dev_lock:
            for key, d in self._devices.items():
                if d['acc'] == acc:
                    out[d['no']] = d
        return out

    def fetch_one(self, acc, dev_no):
        """手动获取单台设备最新数据（与巡检共用限速器，取完即时更新状态）。
        返回与 _devices 里同构的设备状态字典。"""
        cfg = next((c for c in self._accounts() if c['name'] == acc), None)
        if cfg is None:
            raise ApiError('没找到账号: {}'.format(acc))
        tok, uid = R.ensure_login(cfg)
        # 设备名：优先用已知状态，其次设备清单
        known = self.device_state(acc, dev_no)
        name = known['name'] if known else dev_no
        if known is None:
            try:
                devs = self._get_devices(cfg, tok, uid)
                name = next((d['name'] for d in devs if d['no'] == dev_no), dev_no)
            except Exception:
                pass
        rows, info = self._fetch_device(dev_no, tok)
        st = R.judge_status(rows)
        age = R.data_age_minutes(rows)
        try:
            want_min = int(self.rules.get('stale_minutes') or 30)
        except (TypeError, ValueError):
            want_min = 30
        if st == 'online' and age is not None and age > want_min:
            st = 'stale'
        if st == 'blank' and not self.rules.get('flag_blank', True):
            st = 'offline'
        times = [r[3] for r in rows if r[3] and r[3] != '-']
        now = time.time()
        key = '{}|{}'.format(acc, dev_no)
        entry = {
            'acc': acc, 'no': dev_no, 'name': name,
            'status': ST_TEXT.get(st, str(st)), 'age_min': age,
            'newest': max(times) if times else '-',
            'vals': self._vals_with_rw(acc, dev_no, tok, rows),
            'n_points': len(rows), 'ts': now,
            'info': {'org': info.get('projectName'), 'addr': info.get('address')},
        }
        # 该设备的异常重算（与 _scan 汇总段同一套逻辑）
        anoms = []
        scoped = self._in_scope(cfg, name)
        if scoped and entry['status'] in ('离线', '数据陈旧', '采集异常'):
            anoms.append({
                'kind': ('offline' if entry['status'] == '离线' else
                         ('blank' if entry['status'] == '采集异常' else 'stale')),
                'rule_id': '',
                'rule_name': ('设备离线' if entry['status'] == '离线' else
                              ('采集异常' if entry['status'] == '采集异常' else '数据停更')),
                'level': STATUS_LEVEL.get(entry['status'], 'warn'),
                'point': '', 'val': R.fmt_age(age),
                'cond': '数据停更超过 {} 分钟'.format(want_min),
                'since': self._rule_since.setdefault(('__stale__', key), now),
                'note': '',
            })
        if scoped:
            anoms.extend(self._match_rules(cfg, entry, rows))
        for a in anoms:
            a['id'] = '{}|{}|{}'.format(key, a.get('rule_id') or a['kind'],
                                        a.get('point') or '')
            a['acc'] = acc
            a['dev_name'] = name
            a['dev_no'] = dev_no
            a['duration_min'] = (now - a.get('since', now)) / 60.0
            # 不再要求 baseline_done：手动点「获取」拿到命中就应算新异常
            a['is_new'] = a['id'] not in self._prev_anom_ids
        with self._dev_lock:
            self._devices[key] = entry
        self._anoms = [a for a in getattr(self, '_anoms', [])
                       if not (a.get('acc') == acc and a.get('dev_no') == dev_no)] + anoms
        self._anoms.sort(key=lambda a: (a['level'] != 'critical',
                                        -a.get('duration_min', 0)))
        self._prev_status[key] = entry['status']
        self._prev_anom_ids = {a['id'] for a in self._anoms}
        self._say('手动获取 {}: {}，{} 个变量，数据 {}'.format(
            name, entry['status'], len(rows), R.fmt_age(age)))
        return entry

    # ---------------- 历史数据（折线图页，按需拉取） ----------------
    # 只在用户打开历史页 / 点刷新时调用，绝不参与周期巡检 —— 不增加平台请求压力。
    # 实测（2026-10-01）：/history/cusdevice/dataPoint 必须用 dataPointRelId
    # 传进 dataPointId 字段（与 lastDataPoint 相同），文档示例里的真 dataPointId 查不到数据。

    HISTORY_URL = 'https://history.usr.cn:7002/history/cusdevice/dataPoint'
    PTS_TTL = 3600            # 变量点定义缓存时长（秒），定义几乎不变
    HIST_PAGE_SIZE = 2000     # 平台单页上限（实测 5000/10000 返回空）
    HIST_MAX_PAGES = 25       # 最多翻 25 页 = 5 万点，覆盖分钟级设备 30 天
    HIST_CACHE_TTL = 90       # 同一设备同一范围的短期缓存（秒）

    def _token_for(self, acc):
        for a in self._accounts():
            if a['name'] == acc:
                tok, uid = R.ensure_login(a)   # 有缓存，命中时不发请求
                return tok
        raise ApiError('没找到账号: {}'.format(acc))

    def _tok_uid_for(self, acc):
        for a in self._accounts():
            if a['name'] == acc:
                return R.ensure_login(a)       # (tok, uid)
        raise ApiError('没找到账号: {}'.format(acc))

    def control_points(self, acc, dev_no, points):
        """向设备下发变量值 / 开关，并等设备回报确认真实生效。
        points: [{'dataPointId': int, 'identifier': str, 'value': str}, ...]
        ⚠️ 调用方必须已经完成二次确认；这里只管发。
        返回 {'ok', 'msg', 'confirmed', 'topic', 'payload', 'replies'}"""
        tok, uid = self._tok_uid_for(acc)
        return usr_control.write_points(tok, uid, dev_no, points, acc=acc)

    def point_query(self, acc, dev_no, points):
        """单个变量刷新：让设备立刻上报这些变量的当前值（**只读**，不写任何值）。
        points: [{'name': 变量名, 'dataPointId': relId, 'identifier': 'variableN'}, ...]
        返回 {'ok', 'msg', 'confirmed', 'vals': [{'name','v'}]}
        —— 拿到的值同时并进内存快照，这样 8 秒一轮的自动重画不会把它顶回旧值。"""
        if not points:
            raise ApiError('没有要刷新的变量')
        tok, uid = self._tok_uid_for(acc)
        by_id = {}
        q = []
        for p in points:
            try:
                pid = int(p.get('dataPointId'))
            except (TypeError, ValueError):
                continue
            by_id[str(pid)] = p.get('name') or ''
            q.append({'dataPointId': pid, 'identifier': p.get('identifier') or ''})
        if not q:
            raise ApiError('变量没有可用的点位 ID（可能没取到变量点定义）')
        r = usr_control.probe_read(tok, uid, dev_no, q, timeout=8, acc=acc)
        if not r.get('ok'):
            return {'ok': False, 'msg': r.get('msg') or '刷新失败', 'vals': []}
        out = []
        st = self.device_state(acc, dev_no)
        for pid, val in (r.get('vals') or {}).items():
            nm = by_id.get(pid)
            if not nm:
                continue
            out.append({'name': nm, 'v': val})
            try:                              # 并进内存快照，避免自动重画时又变回旧值
                if st is not None:
                    st.setdefault('vals', {})[nm] = {'v': val, 't': '刚刚'}
                    # 设备刚刚回报过，最新数据时间就是现在 —— 前端「数据时间」
                    # 按 newest 实时算，不同步的话会一直停在上一轮巡检的旧时间上。
                    nowstr = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    prev = st.get('newest')
                    if not prev or prev == '-' or prev < nowstr:
                        st['newest'] = nowstr
                    st['age_min'] = 0.0
                    st['ts'] = time.time()
            except Exception:
                pass
        return {'ok': True, 'confirmed': bool(out), 'vals': out, 'msg': r.get('msg')}

    def live_vals_rw(self, acc, dev_no):
        """{变量名: {rw, id, ident}}——控制信息（懒加载：只在打开设备实时卡时拉点定义）。
        不参与巡检，避免给每轮巡检增加请求。"""
        try:
            tok = self._token_for(acc)
            pdefs = self._point_defs(acc, dev_no, tok)
        except Exception:
            return {}
        return {(p.get('name') or ''): {
                    'rw': 1 if p.get('readWrite') == 1 else 0,
                    # ★ 下发用的是运行时的 dataPointRelId，不是 dataPointId
                    #   （读值 lastDataPoint 用的也是 relId；传 dataPointId 平台匹配不到点、静默无效）
                    'id': p.get('dataPointRelId'),
                    'ident': p.get('dataIdentifier') or '',
                } for p in pdefs}

    def _vals_with_rw(self, acc, dev_no, tok, rows):
        """给每个变量补「是否可写 / 下发用 ID / identifier」，供前端渲染开关或输入框。
        变量点定义走 _point_defs（有 TTL 缓存，正常不额外发请求）。"""
        try:
            pdefs = self._point_defs(acc, dev_no, tok)
        except Exception:
            pdefs = []
        by_name = {p.get('name') or '': p for p in pdefs}
        out = {}
        for r in rows:
            p = by_name.get(r[1]) or {}
            out[r[1]] = {
                'v': r[4], 't': r[3],
                'rw': 1 if p.get('readWrite') == 1 else 0,
                'id': p.get('dataPointRelId'),   # 下发用 relId（见 live_vals_rw 注释）
                'ident': p.get('dataIdentifier') or '',
            }
        return out

    def _point_defs(self, acc, dev_no, tok):
        """变量点定义（含 dataPointRelId / unit），带 TTL 缓存"""
        key = '{}|{}'.format(acc, dev_no)
        hit = self._pts_cache.get(key)
        if hit and time.time() - hit[0] < self.PTS_TTL:
            return hit[1]
        pts = self._api(R.POINTS_URL, {
            'dataPointType': '', 'dataPointName': '', 'dataPointIdList': [], 'slaveId': '',
            'cusdeviceNo': dev_no, 'getOperable': 0, 'getTimestampDatas': 1,
            'getLocationDatas': 1, 'getStringDatas': 0, 'getBitDatas': 0,
            'pageNo': 1, 'pageSize': 500}, tok)
        plst = (pts.get('data') or {}).get('cusdeviceDataPointList') or []
        self._pts_cache[key] = (time.time(), plst)
        return plst

    # 【对齐手机版 v0.28】去重水位改为内存态（self._rec_val / self._rec_ep），
    # 不再从 CSV 恢复：进程重启后首轮会全量写一遍当基线（约 30KB，可接受），
    # 之后进入"值去重 + 心跳"瘦写。原 _rec_load_last 已移除。

    def _record_rows(self, results):
        """把本轮巡检采集到的最新值追加到本地月度 CSV（监控记录_YYYY-MM[-host].csv）。
        【对齐手机版 v0.28】去重判据由"平台时间戳"改为"值"：
          - 值变了 -> 立刻写
          - 值没变，但距上次落盘已超心跳(默认 60 分钟) -> 也写（保住折线图最少点数）
          - 其余 -> 不写
        心跳用 time.time() 墙钟计时（不用平台时间戳 tm，避免设备时间回拨导致永写/永不写）；
        CSV 的"时间"列仍写平台时间戳 tm，语义不变。
        水位仅存内存(self._rec_val/_rec_ep)，进程重启首轮全量写一遍当基线，之后进入瘦写。"""
        buckets = {}
        seen_round = set()             # 同一轮内 (设备,变量,时间) 去重
        now = time.time()
        for cfg, tok, dev, rows, info in results:
            for r in rows:
                if len(r) < 5:
                    continue
                tm = (r[3] or '').strip()
                val = str(r[4]).strip() if r[4] is not None else ''
                if not tm or tm == '-' or val in ('', '-'):
                    continue
                k = (dev['no'], r[1])
                rk = (dev['no'], r[1], tm)
                if rk in seen_round:
                    continue
                seen_round.add(rk)
                # 值去重 + 心跳：内存态，重启后 prev 为 None 必写（首轮基线）
                prev = self._rec_val.get(k)
                ep = self._rec_ep.get(k)
                if prev is not None and prev == val and ep is not None \
                        and (now - ep) < 3600:
                    continue
                mon = tm[:7]
                buckets.setdefault(mon, []).append(
                    (tm, cfg['name'], dev['name'], dev['no'], r[1], val))
        if not buckets:
            return
        out_dir = os.path.join(self.base, HIST_DIR)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            self._say('监控记录目录创建失败: {}'.format(str(e)[:80]))
            return
        n_total = 0
        for mon in sorted(buckets):
            rows_mon = sorted(buckets[mon])
            path = os.path.join(out_dir, '{}{}{}.csv'.format(REC_PREFIX, mon, REC_SUFFIX))
            is_new = not os.path.exists(path)
            try:
                with open(path, 'a', encoding='utf-8-sig', newline='') as f:
                    w = csv.writer(f)
                    if is_new:
                        w.writerow(['时间', '账号', '设备名称', '设备编号', '变量', '值'])
                    w.writerows(rows_mon)
            except Exception as e:
                self._say('监控记录写入失败 {}: {}'.format(mon, str(e)[:80]))
                return
            for _tm, _acc, _nm, no, var, _val in rows_mon:
                k = (no, var)
                self._rec_val[k] = _val
                self._rec_ep[k] = now
            n_total += len(rows_mon)
        self._say('监控记录: 本轮落盘 {} 行'.format(n_total))

    # 活动区间合并阈值：两次记录间隔不超过它就算"设备一直活着"。
    # 本地巡检约 30 分钟一轮、平台按变量瘦身（值没变就不落点），
    # 单看一个变量的缺口根本分不清"值没变"和"设备离线"，
    # 必须拿「这台设备任何变量有没有记录」当活着的证据。
    ALIVE_MERGE = 60 * 60 * 1000

    @staticmethod
    def _alive_spans(timestamps):
        """把一串时刻合并成 [[start, end], ...] 活动区间（start/end 为 ms）。
        间隔 > ALIVE_MERGE 的断开——那才是设备真没说话的窗口。"""
        ts = sorted({int(t) for t in timestamps if t})
        spans = []
        for t in ts:
            if spans and t - spans[-1][1] <= WatchEngine.ALIVE_MERGE:
                spans[-1][1] = t
            else:
                spans.append([t, t])
        return spans

    def local_history(self, acc, dev_no, hours=24, want=None, max_vars=6):
        """从本地监控记录 CSV 读某台设备的历史（不请求平台）。
        数据来源：每轮巡检自动落盘的 监控记录_YYYY-MM.csv + 手动保存的 历史数据_YYYY-MM.csv"""
        try:
            hours = max(1, min(720, int(hours)))
        except (TypeError, ValueError):
            hours = 24
        start_s = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        start_mon = start_s[:7]
        out_dir = os.path.join(self.base, HIST_DIR)
        seen = set()
        agg = {}                       # 变量名 -> {tm: val}（去重：本地文件可能重叠）
        order = []                     # 变量出现顺序
        dev_ts = set()                 # 这台设备所有变量的记录时刻（活着的证据）
        run_ts = set()                 # 全部账号全部设备的记录时刻（程序在跑的证据）
        if os.path.isdir(out_dir):
            for fn in sorted(os.listdir(out_dir)):
                if not fn.endswith('.csv'):
                    continue
                if not (fn.startswith(REC_PREFIX) or fn.startswith('历史数据_')):
                    continue
                pre = REC_PREFIX if fn.startswith(REC_PREFIX) else '历史数据_'
                mon = fn[len(pre):len(pre) + 7]   # 取 YYYY-MM（兼容可选的主机名后缀）
                if mon < start_mon:    # 整个文件都在范围外
                    continue
                try:
                    with open(os.path.join(out_dir, fn), 'r', encoding='utf-8-sig',
                              newline='') as f:
                        rdr = csv.reader(f)
                        header = next(rdr, None)
                        ix = {n: i for i, n in enumerate(header or [])}
                        if any(n not in ix for n in
                               ('时间', '账号', '设备名称', '设备编号', '变量', '值')):
                            continue
                        c = tuple(ix[n] for n in
                                  ('时间', '账号', '设备名称', '设备编号', '变量', '值'))
                        for row in rdr:
                            if len(row) <= max(c):
                                continue
                            tm, a, _nm, no, nm, val = (row[i] for i in c)
                            if tm < start_s:
                                continue
                            try:
                                ms = int(datetime.strptime(
                                    tm, '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
                            except ValueError:
                                continue
                            # 任何账号任何设备的一行，都证明「程序当时在跑」——
                            # 折线图要靠它区分「设备离线」和「程序没开」
                            run_ts.add(ms)
                            if a != acc or no != dev_no:
                                continue
                            if val.strip() in ('', '-'):
                                continue
                            k = (nm, tm)
                            if k in seen:
                                continue
                            seen.add(k)
                            if nm not in agg:
                                agg[nm] = []
                                order.append(nm)
                            dev_ts.add(ms)
                            agg[nm].append([ms, val])
                except Exception:
                    continue
        if want:
            wset = set(want)
            # 本地数据不花平台请求，不按 max_vars 截断（云端模式才需要省请求）；
            # 变量多时由前端按优先级排序 + chips 滚动展示
            sel = [n for n in order if n in wset]
        else:
            sel = list(order)
        series = {}
        for nm in sel:
            rows = sorted(agg.get(nm, []), key=lambda t: t[0])
            if rows:
                series[nm] = rows
        # 心跳窗口：写入规则是固定 60 分钟（与手机版 v0.28 一致，见 _record_rows），
        # 但那一行要等到「下一轮巡检」才补写，所以值不变时相邻两行的观测间隔
        # 最大逼近 60 分钟 + 巡检周期（15 分钟一轮→60，25 分钟一轮→75）。
        # 折线图拿它当「不需要再找证据的缺口上限」——注意这是观测上限，不是心跳本身。
        hb_ms = int(self.state.get('interval', 900)) * 1000 + 60 * 60000
        return {'ok': True, 'vars': [{'name': n, 'unit': ''} for n in order],
                'series': series, 'hours': hours, 'source': 'local',
                'alive': self._alive_spans(dev_ts),
                'run': self._alive_spans(run_ts), 'hb': hb_ms}

    def history(self, acc, dev_no, hours=24, want=None, max_vars=6):
        """按需拉某台设备的历史数据（折线图）。
        want: 变量名列表；None/空 则取前 max_vars 个变量。
        返回 {'ok':True, 'vars':[{name,unit}...], 'series':{name:[[ms,val]...]}, 'hours':h}
        """
        try:
            hours = max(1, min(720, int(hours)))
        except (TypeError, ValueError):
            hours = 24
        wkey = tuple(sorted(want)) if want else None
        ckey = (acc, dev_no, hours, wkey)
        # 短期缓存：同一设备同一范围 90s 内重复查看不重复拉平台（平台对连续
        # 大拉取会软限流——返回空列表，表现为「明明有数据却查不到」）
        c = self._hist_cache.get(ckey)
        if c and time.time() - c[0] < self.HIST_CACHE_TTL:
            return c[1]
        res = self._history_fetch(acc, dev_no, hours, want, max_vars)
        has_data = any(res.get('series', {}).values())
        if has_data:
            self._hist_cache[ckey] = (time.time(), res)
        elif c and any(c[1].get('series', {}).values()):
            # 刚拉到空但之前有数据：多半是被限流了，先回旧数据顶住
            old = dict(c[1])
            old['stale'] = True
            return old
        return res

    def _history_fetch(self, acc, dev_no, hours, want, max_vars):
        tok = self._token_for(acc)
        plst = self._point_defs(acc, dev_no, tok)
        meta = [{'name': p.get('name') or '', 'unit': (p.get('unit') or '').strip()}
                for p in plst]
        if not plst:
            return {'ok': False, 'msg': '该设备没有变量定义', 'vars': [], 'series': {}}
        if want:
            wset = set(want)
            sel = [p for p in plst if (p.get('name') or '') in wset][:max_vars]
        else:
            sel = plst[:max_vars]
        if not sel:
            return {'ok': True, 'vars': meta, 'series': {}, 'hours': hours}
        name_by_rel = {}
        for p in sel:
            if p.get('dataPointRelId') is not None:
                name_by_rel[p.get('dataPointRelId')] = p.get('name') or ''
        dps = [{'cusdeviceNo': dev_no, 'dataPointId': p.get('dataPointRelId')}
               for p in sel if p.get('dataPointRelId') is not None]
        end = int(time.time() * 1000)
        start = end - hours * 3600 * 1000
        # 分页拉全量：平台单页上限 2000 点（实测 5000/10000 返回空），
        # 分钟级上报的设备 30 天约 3 万点，只取第 1 页会看不到更早的历史
        agg = {}                      # dataPointId -> [[time, value], ...]
        page, empty_try = 1, 0
        while page <= self.HIST_MAX_PAGES:
            r = self._api(self.HISTORY_URL, {
                'start': start, 'end': end, 'timeSort': 'desc',
                'pageNo': page, 'pageSize': self.HIST_PAGE_SIZE, 'dataPoints': dps}, tok)
            data = (r.get('data') or {})
            got = 0
            for x in (data.get('list') or []):
                dp = x.get('dataPointId')
                for it in (x.get('list') or []):
                    if it.get('error') or it.get('time') is None:
                        continue
                    v = it.get('value')
                    if v is None or str(v).strip() in ('', '-'):
                        continue
                    agg.setdefault(dp, []).append([it['time'], str(v)])
                    got += 1
            if got == 0:
                # 空页可能是平台限流的瞬时行为：稍候重试同一页，再空才收工
                empty_try += 1
                if empty_try >= 2:
                    break
                time.sleep(0.8)
                continue
            empty_try = 0
            page += 1
            # 已取到所选范围的起点就不必再翻页了
            if agg:
                oldest = min(row[0] for rows in agg.values() for row in rows)
                if oldest <= start + 60000:
                    break
        series = {}
        alive_ts = set()               # 全部已取变量的时刻并集（设备活着的证据）
        for dp, rows in agg.items():
            nm = name_by_rel.get(dp)
            if not nm:
                continue
            rows.sort(key=lambda t: t[0])
            series[nm] = rows
            for row in rows:
                alive_ts.add(row[0])
        # 平台按变量瘦身（值没变就不落点），单变量缺口不代表离线；
        # 但只取了入选的那几个变量，并集是"尽力而为"的活着证据。
        # 平台不补心跳，hb=0：并集静默就当成机器没开。
        return {'ok': True, 'vars': meta, 'series': series, 'hours': hours,
                'alive': self._alive_spans(alive_ts), 'hb': 0}

    def save_history(self, acc, dev_no, hours=24, want=None, max_vars=6):
        """拉取历史数据并留档到本地：历史数据/历史数据_YYYY-MM.csv。
        按数据点时间所在月份分文件（一个月一个新表，避免单文件无限膨胀）；
        按「设备编号+变量+时间」去重，重复保存只写新行。
        返回 {'ok', 'saved', 'skipped', 'files', ...}"""
        res = self.history(acc, dev_no, hours=hours, want=want, max_vars=max_vars)
        if not res.get('ok'):
            return res
        known = self.device_state(acc, dev_no)
        dev_name = (known or {}).get('name') or dev_no
        if dev_name == dev_no:
            # 引擎还没扫到这台：用缓存的设备清单反查名称（命中缓存不发请求）
            try:
                cfg = next((c for c in self._accounts() if c['name'] == acc), None)
                if cfg:
                    tok, uid = R.ensure_login(cfg)
                    devs = self._get_devices(cfg, tok, uid)
                    dev_name = next((d['name'] for d in devs if d['no'] == dev_no), dev_no)
            except Exception:
                pass
        out_dir = os.path.join(self.base, HIST_DIR)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return {'ok': False, 'msg': '创建目录失败: {}'.format(str(e)[:100]),
                    'saved': 0, 'skipped': 0, 'files': []}
        # 展平 + 按月份分桶（以数据点自身时间决定归哪个表）
        buckets = {}
        for nm, rows in (res.get('series') or {}).items():
            for ms, val in rows:
                try:
                    dt = datetime.fromtimestamp(int(ms) / 1000.0)
                except (TypeError, ValueError, OSError, OverflowError):
                    continue
                buckets.setdefault(dt.strftime('%Y-%m'), []).append(
                    (dt.strftime('%Y-%m-%d %H:%M:%S'), nm, str(val)))
        if not buckets:
            return {'ok': True, 'saved': 0, 'skipped': 0, 'files': [],
                    'msg': '该时间范围内没有可保存的数据点',
                    'series': res.get('series'), 'vars': res.get('vars'),
                    'hours': res.get('hours')}
        saved = skipped = 0
        files = []
        for mon in sorted(buckets):
            path = os.path.join(out_dir, '历史数据_{}.csv'.format(mon))
            old = set()
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                        rdr = csv.reader(f)
                        header = next(rdr, None)
                        ix = {n: i for i, n in enumerate(header or [])}
                        c_no, c_v, c_t = ix.get('设备编号'), ix.get('变量'), ix.get('时间')
                        if None not in (c_no, c_v, c_t):
                            for row in rdr:
                                if len(row) > max(c_no, c_v, c_t):
                                    old.add((row[c_no], row[c_v], row[c_t]))
                except Exception:
                    old = set()
            new_rows = []
            for t, nm, val in buckets[mon]:
                k = (dev_no, nm, t)
                if k in old:
                    skipped += 1
                else:
                    old.add(k)
                    new_rows.append((t, nm, val))
            if not new_rows:
                continue
            is_new = not os.path.exists(path)
            try:
                with open(path, 'a', encoding='utf-8-sig', newline='') as f:
                    w = csv.writer(f)
                    if is_new:
                        w.writerow(['时间', '账号', '设备名称', '设备编号', '变量', '值'])
                    for t, nm, val in sorted(new_rows):
                        w.writerow([t, acc, dev_name, dev_no, nm, val])
            except Exception as e:
                return {'ok': False, 'msg': '写入 {} 失败: {}'.format(
                    os.path.basename(path), str(e)[:100]),
                    'saved': saved, 'skipped': skipped, 'files': files}
            saved += len(new_rows)
            files.append(os.path.basename(path))
        self._say('历史留档 {}: 新增 {} 行{}，文件: {}'.format(
            dev_name, saved,
            '，跳过 {} 行已有数据'.format(skipped) if skipped else '',
            '、'.join(files) if files else '无新增'))
        return {'ok': True, 'saved': saved, 'skipped': skipped, 'files': files,
                'series': res.get('series'), 'vars': res.get('vars'),
                'hours': res.get('hours')}
