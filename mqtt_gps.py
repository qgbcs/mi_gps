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

# 手机端实时定位：直接问 Android LocationManager 要 last known location。
# Application Context 可能来自 appctx（宿主注入过才有），也可能是 App 重启后
# appctx 不在 RPC 命名空间里——此时用 Chaquopy 官方入口拿 Application。
#
# 顺带把「为什么拿不到」的诊断一起回传：Android 10+ 后台进程没有定位访问权
# （ACCESS_BACKGROUND_LOCATION 未授予且进程不在前台时，任何 provider 的
# getLastKnownLocation 都返回 null），这类原因必须显式告诉页面，不能只说
# 「手机没有可用的 last known location」。
LIVE_CODE = r"""
import json
try:
    ctx = appctx
except NameError:
    from com.chaquo.python import Python
    ctx = Python.getPlatform().getApplication()

pkg = ctx.getPackageName()
diag = {'pkg': pkg}

def _perm(name):
    try:
        return ctx.getPackageManager().checkPermission(name, pkg) == 0
    except Exception:
        return None

diag['fine'] = _perm('android.permission.ACCESS_FINE_LOCATION')
diag['coarse'] = _perm('android.permission.ACCESS_COARSE_LOCATION')
diag['background'] = _perm('android.permission.ACCESS_BACKGROUND_LOCATION')

lm = ctx.getSystemService('location')
try:
    diag['location_enabled'] = bool(lm.isLocationEnabled())
except Exception:
    diag['location_enabled'] = None

providers = {}
for _p in ('gps', 'network', 'passive', 'fused'):
    try:
        providers[_p] = bool(lm.isProviderEnabled(_p))
    except Exception:
        providers[_p] = None
diag['providers'] = providers

try:
    am = ctx.getSystemService('activity')
    procs = am.getRunningAppProcesses()
    imp = None
    for _i in range(procs.size()):
        _pi = procs.get(_i)
        if _pi.processName == pkg:
            imp = _pi.importance
    diag['importance'] = imp
except Exception:
    diag['importance'] = None

best = None
for prov in ('fused', 'gps', 'network', 'passive'):
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
print(json.dumps({'loc': best, 'diag': diag}))
"""

# 进程重要性 -> 中文（Android ActivityManager.RunningAppProcessInfo.IMPORTANCE_*）
_IMPORTANCE_TEXT = {
    100: "前台（可见）",
    125: "前台服务",
    150: "可感知",
    200: "后台服务",
    230: "后台",
    250: "空进程",
    300: "已缓存（后台）",
    325: "服务（后台）",
    400: "已缓存",
}

_last_probe = {"diag": None}


def last_probe_diag():
    """最近一次实时定位探测到的手机端诊断信息（页面展示用，可能为 None）。"""
    return _last_probe.get("diag")


def _diag_reason(diag):
    """把手机端诊断拼成一句人能看懂的原因 + 处理建议。"""
    if not isinstance(diag, dict):
        return "手机端没有返回定位诊断信息"
    bits = []
    imp = diag.get("importance")
    imp_text = _IMPORTANCE_TEXT.get(imp, "未知(%s)" % imp if imp is not None else "未知")
    bits.append("进程状态 %s" % imp_text)
    if diag.get("fine") is False and diag.get("coarse") is False:
        bits.append("前台定位权限未授予")
    elif diag.get("background") is False:
        bits.append("后台定位权限未授予（ACCESS_BACKGROUND_LOCATION）")
    if diag.get("location_enabled") is False:
        bits.append("系统定位开关已关闭")
    provs = diag.get("providers") or {}
    opened = [k for k, v in provs.items() if v]
    if opened:
        bits.append("可用 provider %s" % "/".join(opened))
    reason = "手机端拿不到任何 last known location：" + "，".join(bits)
    if imp is not None and imp >= 200 and diag.get("background") is False:
        reason += ("。Android 10+ 的定位访问按进程状态收紧：后台进程即使有前台定位权限也拿不到位置。"
                   "处理：设置→应用→Xime→权限→位置 改为「始终允许」，或弹起 Xime 键盘/打开 App 让其回到前台")
    elif diag.get("location_enabled") is False:
        reason += "。处理：打开系统「位置信息」开关"
    return reason



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


def _short_err(text):
    """错误串只留最后一行有用信息（RPC 回包里的多行 traceback 太长，页面放不下）。"""
    text = str(text).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else text


def live(topic, key, timeout=8):
    """实时定位：LocationManager.getLastKnownLocation（WGS-84）。"""
    raw = _rpc_text(LIVE_CODE, topic, timeout, key, prefer_stdout=True)
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError("手机端返回的不是 JSON：%s" % _short_err(raw)[:200])
    if isinstance(data, dict) and "loc" in data:
        item, diag = data.get("loc"), data.get("diag")
    else:                      # 兼容旧版只回传 location 的格式
        item, diag = data, None
    _last_probe["diag"] = diag
    if not isinstance(item, dict) or item.get("lat") is None or item.get("lng") is None:
        raise RuntimeError(_diag_reason(diag))
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
            # 自家 RuntimeError 的面板文案已经是中文整句，不再重复类名
            live_err = str(exc) if isinstance(exc, RuntimeError) else \
                "%s: %s" % (type(exc).__name__, exc)

    if point is None:
        try:
            res = _rpc_text(_read_tail_code(), topic, timeout, key)
            point = parse_gps_text(res)
            if not point:
                raise RuntimeError("文件里没有可用的 lat/lon 记录")
        except Exception as exc:  # 两路都失败：把真实原因一起报出来，别让文件错误盖住定位原因
            log_err = _short_err("%s: %s" % (type(exc).__name__, exc))
            if live_err:
                raise RuntimeError("实时定位失败：%s；gps.txt(%s) 兜底也失败：%s"
                                   % (_short_err(live_err), GPS_FILE, log_err))
            raise
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
