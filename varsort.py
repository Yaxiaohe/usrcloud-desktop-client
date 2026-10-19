# -*- coding: utf-8 -*-
"""变量排序（置顶）——数据监控 / 巡站截图 / 历史数据三处共用同一份规则。

为什么要有这个模块
------------------------------------------------------------------
v1.3 之前三处各自排序：
  · 数据监控：完全按平台返回顺序，平台每次返回的变量次序不一定相同；
  · 巡站截图：run_usrcloud.water_temp_first 只保证「水温在一组」，组内仍按平台序；
  · 历史数据：前端 HVAR_PRIO 正则分级，出水/进水同属一级，同级看原序。
于是「出水温度」和「进水温度」谁先谁后全看平台心情 —— 用户看到的就是
两个变量位置反复跳。

现在统一为一张有序的「关键词」列表（默认：出水温度 → 进水温度 → 供水温度 →
回水温度），按列表顺序逐条匹配，先命中者排前面；没命中的保持原相对顺序。
列表对用户可编辑（设置页），可增删、上下移动、置顶 —— 顺序一旦定下就固定，
除非平台真换了变量名。

匹配口径
------------------------------------------------------------------
· 关键词包含在变量名里即算命中（「出水温度」命中「出水温度1」）；
· 但带 修正/原值/设定/系数/补偿/偏置 的一律不参与置顶（这些是工程参数、
  设定值、修正量，不是读数）；
· 大小写不敏感（英文关键词也能用，如 t1）。

文件
------------------------------------------------------------------
变量排序.json（与 accounts.csv 同目录）
    {"version": 1, "rules": [{"key": "出水温度", "on": true}, ...]}
文件不存在 = 用默认规则（新装即用，不需要预置文件）。
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(BASE, '变量排序.json')

# 默认置顶顺序：出水/进水一组，供水/回水一组。
# 用户 2026-10-19 明确要求：出水温度固定第 1、进水固定第 2；供水固定第 1、回水固定第 2。
DEFAULT_RULES = [
    {'key': '出水温度', 'on': True},
    {'key': '进水温度', 'on': True},
    {'key': '供水温度', 'on': True},
    {'key': '回水温度', 'on': True},
]

# 工程参数/设定值：名字里带这些字样的一律不置顶（否则「回水温度修正」「T1修正」
# 这类会跟着关键词一起被提到最前，屏幕上反而更难读）。
SKIP_WORDS = ('修正', '原值', '设定', '系数', '补偿', '偏置')

MAX_RULES = 40          # 关键词条数上限（防止误粘贴几百条把列表拖垮）
UNMATCHED = 10 ** 6     # 未命中规则的排序值：稳定排序下 = 保持原顺序

_cache = {'mtime': None, 'rules': None}


def default_rules():
    """默认规则（深拷贝，调用方随便改不会污染模块常量）。"""
    return [dict(r) for r in DEFAULT_RULES]


def normalize(rules):
    """把任意输入整理成 [{key, on}]：去空、去重（保留首次出现）、截断超长。"""
    out, seen = [], set()
    for r in rules or []:
        if isinstance(r, dict):
            key = str(r.get('key') or '').strip()
            on = bool(r.get('on', True))
        else:
            key, on = str(r or '').strip(), True
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append({'key': key, 'on': on})
        if len(out) >= MAX_RULES:
            break
    return out


def load():
    """读规则；文件不存在 / 读坏了都用默认规则（不能让排序把功能搞挂）。"""
    try:
        st = os.stat(FILE)
        if _cache['rules'] is not None and _cache['mtime'] == st.st_mtime:
            return [dict(r) for r in _cache['rules']]
        with open(FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        rules = normalize(data.get('rules') if isinstance(data, dict) else data)
        if not rules:
            rules = default_rules()
        _cache['mtime'], _cache['rules'] = st.st_mtime, rules
        return [dict(r) for r in rules]
    except Exception:
        return default_rules()


def save(rules):
    """原子写入（先写临时文件再替换），避免断电/并发留下半个 JSON。"""
    rules = normalize(rules)
    if not rules:
        rules = default_rules()
    tmp = FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'rules': rules}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)
    _cache['mtime'], _cache['rules'] = os.stat(FILE).st_mtime, rules
    return rules


def skip_name(name):
    return any(w in str(name or '') for w in SKIP_WORDS)


def rank(name, rules=None):
    """变量名 → 排序值。0 最靠前（第一条规则），未命中 = UNMATCHED。"""
    rules = load() if rules is None else rules
    n = str(name or '')
    if not n or skip_name(n):
        return UNMATCHED
    low = n.lower()
    for i, r in enumerate(rules):
        if not r.get('on', True):
            continue
        k = str(r.get('key') or '').strip().lower()
        if k and k in low:
            return i
    return UNMATCHED


def order(items, name_of=None, rules=None):
    """按规则稳定排序（只改顺序，不增删）。

    items  : 变量名列表，或任意对象列表（此时用 name_of 取名字）
    name_of: items 元素 → 变量名；为 None 时元素本身就是名字
    rules  : 不传则读文件
    """
    seq = list(items or [])
    if not seq:
        return seq
    rules = load() if rules is None else rules
    get = name_of or (lambda x: x)
    # Python 的 sorted 是稳定排序：同 rank 的项保持原相对顺序，
    # 所以「没配置的变量」不会因为排序而互相换位置。
    return sorted(seq, key=lambda x: rank(get(x), rules))
