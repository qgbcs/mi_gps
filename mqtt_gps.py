# -*- coding: utf-8 -*-
"""通过 MQTT 读取手机端实时定位（优先），gps.txt 日志兜底。

手机端 gps.txt 是「移动超过阈值才追加一行」的轨迹日志，不是实时位置：
静止时最后一行可能是几小时前的点。因此这里优先直接问 Android
LocationManager 要 last known location，拿不到才回落读 gps.txt。

gps.txt 每行形如：
  2026-11-11 17:11:01.313 | location_moved | dist_m=600 | ... | speed=5.23 | lat=28.262193 | lon=112.947503
其中的 lat/lon 是设备原始 GPS 输出（WGS-84），与小米云取到的坐标同一坐标系。
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MULTI_MQTT = os.environ.get("MULTI_MQTT_DIR", r"C:\test\github\multi_mqtt")
if MULTI_MQTT not in sys.path:
    sys.path.insert(0, MULTI_MQTT)

GPS_FILE = "/sdcard/Alarms/gps.txt"
_LINE = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?)\s*\|\s*([^|]*)\|(.*)$")
_LAT = re.compile(r"lat=(-?\d+(?:\.\d+)?)")
_LON = re.compile(r"lon=(-?\d+(?:\.\d+)?)")
_ACC = re.compile(r"acc=(-?\d+(?:\.\d+)?)")
_SPD = re.compile(r"speed=(-?\d+(?:\.\d+)?)")
_DIST = re.compile(r"(?<![a-z_])dist_m=(-?\d+(?:\.\d+)?)")
_TOTAL = re.compile(r"distance_total_m=(-?\d+(?:\.\d+)?)")

_cache = {"ts": 0.0, "key": None, "data": None}

# 手机端实时定位：直接问 Android LocationManager 要 last known location
# （运行在 com.qgb.xime 的 Python 环境里，appctx 是当前 Application Context）
LIVE_CODE = r"""
import json
lm = appctx.getSystemService('location')
best = None
for prov in ('gps', 'network', 'fused', 'passive'):
    try:
        loc = lm.getLastKnownLocation(prov)
    except Exception:
        continue
    if loc is None:
        continue
    item = {'provider': prov, 'lat': loc.getLatitude(), 'lng': loc.getLongitude(),
            'acc': loc.getAccuracy(), 'ts': loc.getTime()}
    if best is None or item['ts'] > best['ts']:
        best = item
print(json.dumps(best))
"""


def _read_tail_code(lines=400):
    return (
        "with open(%r, 'r', encoding='utf-8', errors='ignore') as f:\n"
        "    r = f.read()\n"
        "r = '\\n'.join(r.splitlines()[-%d:])\n" % (GPS_FILE, lines)
    )


def _parse_line(line):
    """解析 gps.txt 的一行，拿不到 lat/lon 返回 None。"""
    line = line.strip()
    if "lat=" not in line or "lon=" not in line:
        return None
    m = _LINE.match(line)
    body = m.group(3) if m else line
    lat, lon = _LAT.search(body), _LON.search(body)
    if not lat or not lon:
        return None
    acc, spd = _ACC.search(body), _SPD.search(body)
    dist, total = _DIST.search(body), _TOTAL.search(body)
    tstr = (m.group(1) if m else "")
    ts = None
    if tstr:
        try:
            ts = time.mktime(time.strptime(tstr[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            ts = None
    return {
        "time": tstr,
        "ts": ts,
        "event": (m.group(2).strip() if m else ""),
        "lat": float(lat.group(1)),
        "lng": float(lon.group(1)),
        "acc": float(acc.group(1)) if acc else None,
        "speed": float(spd.group(1)) if spd else None,
        "dist": float(dist.group(1)) if dist else None,
        "total": float(total.group(1)) if total else None,
        "raw": line,
    }


def parse_gps_text(text):
    """从 gps.txt 文本里取最后一条带 lat/lon 的记录。"""
    last = None
    for line in (text or "").splitlines():
        p = _parse_line(line)
        if p:
            last = p
    return last


def parse_history(text, limit=300):
    """把 gps.txt 解析成历史轨迹点（按文件顺序，最多保留最后 limit 条）。"""
    points = [p for p in (_parse_line(ln) for ln in (text or "").splitlines()) if p]
    return points[-limit:]


def _rpc_text(code, topic, timeout, key, prefer_stdout=False):
    """下发一段 Python，取手机端结果。

    文件读取类代码把结果放在 ``r``（赋值表达式的值）；print 类代码的结果在
    ``stdout``。按 ``prefer_stdout`` 决定优先取哪一个，另一个作兜底。
    """
    from client import client_mqtt
    d = client_mqtt.rpc(code, topic, timeout=timeout, k=key, a=1)
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(f"MQTT RPC 失败：{d.get('error')}")
    res = d.get("r") if isinstance(d, dict) else d
    out = (d.get("stdout") if isinstance(d, dict) else None) or ""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    stdout = lines[-1].strip() if lines else ""
    candidates = (stdout, res) if prefer_stdout else (res, stdout)
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item
    raise RuntimeError("手机端没有返回结果")


def live(topic, key, timeout=8):
    """实时定位：LocationManager.getLastKnownLocation（WGS-84）。"""
    item = json.loads(_rpc_text(LIVE_CODE, topic, timeout, key, prefer_stdout=True))
    if not isinstance(item, dict) or item.get("lat") is None or item.get("lng") is None:
        raise RuntimeError("手机没有可用的 last known location")
    ts = item.get("ts")
    ts = ts / 1000.0 if ts else None
    return {
        "source": "live",
        "provider": item.get("provider"),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None,
        "ts": ts,
        "event": "live:%s" % item.get("provider"),
        "lat": float(item["lat"]),
        "lng": float(item["lng"]),
        "acc": item.get("acc"),
        "speed": None,
        "raw": json.dumps(item, ensure_ascii=False),
    }


def latest(topic, key, timeout=8, use_cache=True, cache_sec=5, prefer_live=True):
    """返回最新点（WGS-84）。优先实时定位，失败回落 gps.txt 日志。"""
    ck = (topic, key, GPS_FILE, bool(prefer_live))
    if use_cache and _cache["data"] and _cache["key"] == ck and time.time() - _cache["ts"] < cache_sec:
        return _cache["data"]

    point, live_err = None, None
    if prefer_live:
        try:
            point = live(topic, key, timeout=timeout)
        except Exception as exc:  # 实时定位不可用只降级到日志，不影响页面
            live_err = "%s: %s" % (type(exc).__name__, exc)

    if point is None:
        res = _rpc_text(_read_tail_code(), topic, timeout, key)
        point = parse_gps_text(res)
        if not point:
            raise RuntimeError("gps.txt 里没有可用的 lat/lon 记录")
        point["source"] = "log"
        point["provider"] = None

    if live_err:
        point["live_error"] = live_err
    point["topic"] = topic
    point["fetched_at"] = time.time()
    _cache.update(ts=time.time(), key=ck, data=point)
    return point


_history_cache = {"ts": 0.0, "key": None, "data": None}


def history(topic, key, timeout=8, lines=2000, limit=300, use_cache=True, cache_sec=30):
    """读 gps.txt 全部历史点（移动日志，WGS-84），用于在地图上画轨迹。"""
    ck = (topic, key, GPS_FILE, limit)
    if (use_cache and _history_cache["data"] is not None
            and _history_cache["key"] == ck
            and time.time() - _history_cache["ts"] < cache_sec):
        return _history_cache["data"]
    res = _rpc_text(_read_tail_code(lines), topic, timeout, key)
    points = parse_history(res, limit)
    for p in points:
        p["source"] = "log"
    _history_cache.update(ts=time.time(), key=ck, data=points)
    return points
