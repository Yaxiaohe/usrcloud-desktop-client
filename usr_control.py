"""有人云「控制下发」——MQTT over WebSocket。

平台网页写值不是走 HTTP，而是前端 JS SDK 走 MQTT：
    setDataPoint(设备号, [{dataPointId, identifier, value}])
    → Mqtt.publish("/app/cusdeviceData/<设备号>/d",
                   {"version":0,"setDataPoint":[...]})

本模块把这条链路在 Python 侧复刻（已完成只读验证：能取到 MQTT 地址并成功 CONNECT）：
  - get_mqtt_uri(token, uid)      POST https://hub.usr.cn/open/getResourceUri {type:2}
  - write_points(token, uid, ...) 连接 → publish → 断开

⚠️ 一旦 publish 就会真的下发到设备，调用方必须已完成二次确认。
"""
import base64
import json
import os
import random
import string
import threading
import time
import urllib.request

RESOURCE_URL = 'https://hub.usr.cn/open/getResourceUri'
_URI_CACHE = {}          # token -> (ts, uri)；平台侧该接口有频率限制，缓存 10 分钟
_URI_LOCK = threading.Lock()
URI_TTL = 600


def jwt_payload(token):
    """解析 JWT（取 sub / uid）；失败返回 {}"""
    try:
        p = token.split('.')[1]
        p += '=' * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def get_mqtt_uri(token, uid=None, timeout=20):
    """取 MQTT 服务器地址（wss://…），带缓存。"""
    with _URI_LOCK:
        hit = _URI_CACHE.get(token)
        if hit and time.time() - hit[0] < URI_TTL:
            return hit[1]
    if uid is None:
        uid = jwt_payload(token).get('uid') or 0
    headers = {
        'Content-Type': 'application/json',
        'token': token,
        'u-source': 'jssdk-unknown',
        'sdk-version': '2.4.1',
        'traceId': base64.b64encode(str(uid).encode()).decode(),
    }
    req = urllib.request.Request(
        RESOURCE_URL, data=json.dumps({'type': 2}).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode())
    uri = (j.get('data') or {}).get('uri') or ''
    if not uri:
        raise RuntimeError('未取到 MQTT 地址: {}'.format(j))
    with _URI_LOCK:
        _URI_CACHE[token] = (time.time(), uri)
    return uri


def _log_line(acc, dev_no, text):
    """把每次下发写进 控制下发日志.txt（与程序同目录），便于事后排查。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '控制下发日志.txt')
        with open(p, 'a', encoding='utf-8') as f:
            f.write('[{}] {}\n'.format(
                time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass


def _publish_and_wait(token, uid, dev_no, payload_obj, match_ids, timeout, acc=''):
    """连上 MQTT → 订阅该设备的上行 → 下发 → 等设备回报。

    实测（2026-10-10）：
      · 下发主题 = /app/cusdeviceData/<设备号>/d
      · 回报主题 = /app/cusdeviceData/<账号>/<设备号>/u
      · 官方规范（cloud.usr.cn/document/880.html）：
        [{dataPointId(数据点关联id) 或 identifier, value}]，二选一，这里两个都带

    返回 {'topic','payload','rc','err','replies','error'}
    """
    try:
        import paho.mqtt.client as mqtt
    except Exception as e:
        return {'error': '缺少 MQTT 组件（paho-mqtt）: {}'.format(e)}
    try:
        uri = get_mqtt_uri(token, uid)
    except Exception as e:
        return {'error': '获取下发地址失败: {}'.format(str(e)[:120])}

    rest = uri.replace('wss://', '').replace('ws://', '')
    host = rest.split('/')[0]
    path = '/' + rest.split('/', 1)[1] if '/' in rest else '/mqtt'
    secure = uri.startswith('wss://')
    user = jwt_payload(token).get('sub') or ''      # 实测：MQTT 用户名 = JWT 的 sub
    if not user:
        return {'error': 'token 里取不到用户名'}

    tag = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    payload = json.dumps(payload_obj, ensure_ascii=False)
    topic = '/app/cusdeviceData/{}/d'.format(dev_no)                     # 下发（实测主题）
    up = '/app/cusdeviceData/{}/{}/u'.format(user, dev_no)               # 回报（设备上报）
    replies = []
    st = {'rc': None, 'err': None, 'done': False}

    def on_connect(c, u, flags, rc, props=None):
        if rc == 0:
            c.subscribe(up, 0)          # ★ 先订阅再下发：否则设备的回报可能收不到
        else:
            st['err'] = '连接被拒: {}'.format(rc)
            st['done'] = True

    def on_subscribe(c, u, mid, rcs, props=None):
        try:
            info = c.publish(topic, payload, qos=0)     # 订阅成功后再下发
            st['rc'] = getattr(info, 'rc', None)
        except Exception as e:
            st['err'] = '下发异常: {}'.format(str(e)[:120])
        st['done'] = True

    def on_message(c, u, msg):
        try:
            j = json.loads(msg.payload.decode('utf-8', 'ignore'))
        except Exception:
            return
        for dp in (j.get('dataPoints') or []):
            k = str(dp.get('dataPointId'))
            if match_ids is None or k in match_ids:
                replies.append({'dataPointId': k, 'value': dp.get('value'),
                                'err': dp.get('err'), 'at': time.time()})

    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id='APP:' + tag + user,
                        protocol=mqtt.MQTTv311, transport='websockets')
        c.username_pw_set(user, 't:' + token)
        c.ws_set_options(path=path)
        if secure:
            c.tls_set()
        c.on_connect = on_connect
        c.on_subscribe = on_subscribe
        c.on_message = on_message
        c.connect(host, 443 if secure else 80, timeout)
        c.loop_start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if st['done'] and replies:
                break
            if st['err']:
                break
            time.sleep(0.2)
        c.loop_stop()
        try:
            c.disconnect()
        except Exception:
            pass
    except Exception as e:
        return {'topic': topic, 'payload': payload, 'rc': st['rc'], 'err': st['err'],
                'replies': replies, 'error': 'MQTT 连接/下发异常: {}'.format(str(e)[:150])}
    return {'topic': topic, 'payload': payload, 'rc': st['rc'], 'err': st['err'],
            'replies': replies, 'error': None}


def probe_read(token, uid, dev_no, point, timeout=8, acc=''):
    """通道自检：发一条「主动采集」（只读，不改设备任何数据），等设备回报。
    point: {'dataPointId': relId, 'identifier': 'variableN'}
    返回 {'ok', 'msg', 'replies'}"""
    if not point:
        return {'ok': False, 'msg': '缺少变量点'}
    r = _publish_and_wait(token, uid, dev_no,
                          {'version': 0, 'queryDataPoint': [point]},
                          {str(point.get('dataPointId')): None}, timeout, acc)
    if r.get('error'):
        return {'ok': False, 'msg': r['error'], 'replies': r.get('replies') or []}
    if r['replies']:
        return {'ok': True, 'confirmed': True, 'replies': r['replies'],
                'msg': '通道正常（设备已回报）'}
    return {'ok': True, 'confirmed': False, 'replies': [],
            'msg': '已发出读取请求，但 {} 秒内没收到设备回报'.format(timeout)}


def write_points(token, uid, dev_no, points, timeout=10, acc=''):
    """向设备下发变量值 / 开关，并等待设备回报以确认是否真的生效。

    points: [{'dataPointId': int, 'identifier': str, 'value': str}, ...]
    返回 {'ok', 'msg', 'confirmed', 'topic', 'payload', 'replies'}
    """
    if not points:
        return {'ok': False, 'msg': '没有要下发的变量'}
    want = {str(p.get('dataPointId')): str(p.get('value')) for p in points}
    payload_obj = {'version': 0, 'setDataPoint': points}
    r = _publish_and_wait(token, uid, dev_no, payload_obj, want, timeout, acc)
    names = [str(p.get('identifier') or p.get('dataPointId')) for p in points]
    vals = [str(p.get('value')) for p in points]
    detail = '、'.join('{}={}'.format(a, b) for a, b in zip(names, vals))
    replies = r.get('replies') or []
    if r.get('error'):
        res = {'ok': False, 'confirmed': False, 'msg': r['error']}
    else:
        hit = [x for x in replies if want.get(x['dataPointId']) == str(x['value'])]
        bad = [x for x in replies if x.get('err') not in (0, None)]
        if hit:
            res = {'ok': True, 'confirmed': True,
                   'msg': '已生效：{}（设备已回报新值 {}）'.format(detail, hit[0]['value'])}
        elif bad:
            res = {'ok': True, 'confirmed': False,
                   'msg': '已下发 {}，但设备回报错误 err={}（值超范围或该点不允许写）'.format(
                       detail, bad[0].get('err'))}
        else:
            res = {'ok': True, 'confirmed': False,
                   'msg': '已下发 {}；{} 秒内没看到设备回报新值（不一定是失败，设备上报有周期）'
                          .format(detail, timeout)}
    res.update(topic=r.get('topic'), payload=r.get('payload'), replies=replies[:6])
    _log_line(acc, dev_no,
              '下发 {} | topic={} | payload={} | rc={} | 结果={} | 回报={}'.format(
                  dev_no, r.get('topic'), r.get('payload'), r.get('rc'), res['msg'],
                  json.dumps(replies[:4], ensure_ascii=False)))
    return res
