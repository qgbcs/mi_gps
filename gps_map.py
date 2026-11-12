# -*- coding: utf-8 -*-
"""小米云 GPS vs 手机 MQTT GPS 对照地图页（仿 multi_mqtt 的 /xxx(p) HTTP RPC 形式）。

启动：
    python gps_map.py --port 5566
访问：
    http://127.0.0.1:5566/            -> 跳转到 /map_html(p)
    http://127.0.0.1:5566/map_html(p) -> 地图页面
    http://127.0.0.1:5566/data_json(p)-> 页面用的 JSON
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MULTI_MQTT = os.environ.get("MULTI_MQTT_DIR", r"C:\test\github\multi_mqtt")
if MULTI_MQTT not in sys.path:
    sys.path.insert(0, MULTI_MQTT)
sys.path.insert(0, HERE)

import coord  # noqa: E402
import mi_client  # noqa: E402
import mqtt_gps  # noqa: E402
from client import client_mqtt  # noqa: E402  连接参数名统一取 client_mqtt 的别名表

import server_http  # noqa: E402  (复用 multi_mqtt 的 HTTP RPC 服务器)

MAP_HTML = os.path.join(HERE, "map.html")
STATE = {"device": None, "topic": None, "key": None, "timeout": 8.0, "use_mi": False,
         "cache": None, "cache_ts": 0.0, "cache_sec": 5.0}
_lock = __import__("threading").Lock()


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except Exception as exc:  # 边缘功能绝不阻塞主页面：单项失败只降级
        return None, "%s: %s" % (type(exc).__name__, exc)


def _fmt_ms(ms):
    if not ms:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000.0))


def build_payload(force=False):
    now = time.time()
    with _lock:
        if not force and STATE["cache"] and now - STATE["cache_ts"] < STATE["cache_sec"]:
            return STATE["cache"]

    if STATE["use_mi"]:
        cloud = mi_client.MiCloud()
        dev, mi_err = _safe(cloud.find_device, STATE["device"]) if STATE["device"] else _safe(cloud.device_status)
    else:
        dev, mi_err = None, None          # 默认不加载小米云，省掉登录与外部请求
    mq, mq_err = _safe(mqtt_gps.latest, STATE["topic"], STATE["key"], STATE["timeout"])
    hist, hist_err = _safe(mqtt_gps.history, STATE["topic"], STATE["key"], STATE["timeout"])
    cred = mi_client.credentials_state()

    out = {
        "ok": True,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "topic": STATE["topic"],
        "mi": {"ok": dev is not None, "error": mi_err,
               "disabled": not STATE["use_mi"]},
        "mqtt": {"ok": mq is not None, "error": mq_err},
        "history": {"ok": hist is not None, "error": hist_err, "points": []},
        "account": {"user_id": (mi_client.load_credentials() or {}).get("user_id"),
                    "has_token": cred.get("has_token"),
                    "expires_at": _fmt_ms((cred.get("expires_at") or 0) * 1000) if cred.get("expires_at") else None},
        "delta": {},
    }

    if dev:
        g = dev.get("gps") or {}
        out["mi"].update({
            "name": dev.get("name"), "online": dev.get("online"), "battery": dev.get("battery"),
            "address": dev.get("address"), "accuracy": dev.get("accuracy"),
            "updated": _fmt_ms(dev.get("updated_ms")), "updated_ms": dev.get("updated_ms"),
            "age_sec": int(now - (dev.get("updated_ms") or 0) / 1000.0) if dev.get("updated_ms") else None,
            "gcj": g.get("autonavi"), "bd": g.get("baidu"), "google": g.get("google"),
        })
    if mq:
        gcj = coord.wgs84_to_gcj02(mq["lat"], mq["lng"])
        bd = coord.wgs84_to_bd09(mq["lat"], mq["lng"])
        age = int(now - mq["ts"]) if mq.get("ts") else None
        out["mqtt"].update({
            "lat": mq["lat"], "lng": mq["lng"], "acc": mq.get("acc"), "speed": mq.get("speed"),
            "log_time": mq.get("time"), "event": mq.get("event"),
            "source": mq.get("source"), "provider": mq.get("provider"),
            "live_error": mq.get("live_error"),
            "age_sec": age,
            # 只有 gps.txt 日志点才按「滞后」提示；实时定位不用时间判断
            "stale": bool(mq.get("source") == "log" and age is not None and age > 600),
            "gcj": {"lat": gcj[0], "lng": gcj[1]}, "bd": {"lat": bd[0], "lng": bd[1]},
            "gcj_offset": {"dlat": gcj[0] - mq["lat"], "dlng": gcj[1] - mq["lng"]},
        })

    if hist:
        out["history"]["points"] = [
            {"time": p.get("time"), "ts": p.get("ts"), "event": p.get("event"),
             "lat": p["lat"], "lng": p["lng"], "acc": p.get("acc"), "speed": p.get("speed"),
             "dist": p.get("dist"), "total": p.get("total"), "raw": p.get("raw"),
             "gcj": dict(zip(("lat", "lng"), coord.wgs84_to_gcj02(p["lat"], p["lng"])))}
            for p in hist
        ]

    mi_gcj = (out["mi"].get("gcj") or None)
    if mi_gcj and mq:
        conv = out["mqtt"]["gcj"]
        raw_m = coord.haversine_m(mq["lat"], mq["lng"], mi_gcj["lat"], mi_gcj["lng"])
        gcj_m = coord.haversine_m(conv["lat"], conv["lng"], mi_gcj["lat"], mi_gcj["lng"])
        out["delta"] = {
            "raw_m": round(raw_m, 1),
            "gcj_m": round(gcj_m, 1),
            "bd_m": round(coord.haversine_m(out["mqtt"]["bd"]["lat"], out["mqtt"]["bd"]["lng"],
                                            (out["mi"].get("bd") or {}).get("lat", 0),
                                            (out["mi"].get("bd") or {}).get("lng", 0)), 1),
            "bearing": round(coord.bearing_deg(mq["lat"], mq["lng"], mi_gcj["lat"], mi_gcj["lng"]), 1),
            # 原始 WGS 点相对云端点的差
            "dlat": round(mi_gcj["lat"] - mq["lat"], 6),
            "dlng": round(mi_gcj["lng"] - mq["lng"], 6),
            # 坐标系校正后剩下的残差（云端 - 手机→GCJ）
            "res_dlat": round(mi_gcj["lat"] - conv["lat"], 6),
            "res_dlng": round(mi_gcj["lng"] - conv["lng"], 6),
            "res_lat_m": round(coord.haversine_m(conv["lat"], conv["lng"],
                                                 mi_gcj["lat"], conv["lng"]), 1),
            "res_lng_m": round(coord.haversine_m(conv["lat"], conv["lng"],
                                                 conv["lat"], mi_gcj["lng"]), 1),
            "fixed_m": round(raw_m - gcj_m, 1),
        }

    with _lock:
        STATE["cache"] = out
        STATE["cache_ts"] = now
    return out


def map_html(p):
    p.set_header("Content-Type", "text/html; charset=utf-8")
    p.set_header("Cache-Control", "no-store")
    with open(MAP_HTML, "r", encoding="utf-8") as fp:
        p.set_data(fp.read())


def data_json(p):
    p.set_header("Content-Type", "application/json; charset=utf-8")
    p.set_header("Cache-Control", "no-store")
    p.set_data(json.dumps(build_payload(force=True), ensure_ascii=False))


def index(p):
    map_html(p)


def main(argv=None):
    ap = argparse.ArgumentParser(description="小米云 GPS 对照地图")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5566)
    ap.add_argument("--device", default=None, help="设备名或 fid 片段，默认第一台")
    ap.add_argument(*client_mqtt._cli_opts(*client_mqtt.alias_request_topic),
                    dest="topic", required=True, help="手机 MQTT request_topic")
    ap.add_argument(*client_mqtt._cli_opts(*client_mqtt.alias_private_key),
                    dest="key", required=True,
                    help="MQTT RPC 私钥：整数 / 表达式 / PEM / 文件路径")
    ap.add_argument(*client_mqtt._cli_opts(*client_mqtt.alias_timeout),
                    dest="timeout", type=float, default=8.0,
                    help="单次 MQTT RPC 超时秒数")
    ap.add_argument("--mi", action="store_true",
                    help="同时加载小米云网页位置（默认不加载）")
    ap.add_argument("--cache", type=float, default=5.0, help="数据缓存秒数")
    args = ap.parse_args(argv)
    STATE.update(device=args.device, topic=args.topic, key=args.key,
                 timeout=args.timeout, use_mi=args.mi, cache_sec=args.cache)

    print("手机 GPS 页: http://127.0.0.1:%d/map_html(p)" % args.port)
    print("JSON: http://127.0.0.1:%d/data_json(p)  topic=%s  小米云=%s"
          % (args.port, args.topic, "加载" if args.mi else "不加载（--mi 可开启）"))
    server_http.start_rpc_server(
        port=args.port, ip=args.host, globals=globals(),
        redirect_root="/map_html(p)", persist_aliases=False)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
