# -*- coding: utf-8 -*-
"""小米云“查找设备”客户端：凭据缓存 + 登录 + 拉取设备状态/GPS。

- 凭据（账号、密码、serviceToken、过期时间）统一存在本文件同目录 credentials.json。
- 优先用缓存 serviceToken 直接调接口；401 自动用内置浏览器登录续期。
- 接口（与 i.mi.com 网页完全一致）：
    GET https://i.mi.com/find/v3/device/status/list?ts=<ms>
    Cookie: serviceToken=<...>; userId=<...>
"""
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(HERE, "credentials.json")
PROFILE_DIR = os.path.join(HERE, "browser_profile")
DETAIL_URL = (
    "https://i.mi.com/find/fe/afde28eb879a2bfce1f279bb23121958ed4b0bb97f4b/"
    "phone~a9d22fe8879a2ffbe2a60c991db150468a03420aec1ad8935f11cab40dc67e5a48e164fa8b7591a5829f29dc42e3628c0599036d6f68ccf4c07d82a6b359115919b2c71c5edd598ddccd24ac23f02b97231dee74f1bcbfa115fe415e29587dc053fd1631c0cd3ebda2e035a2075fda31aad2ee35c0/0/detail"
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
API_LIST = "https://i.mi.com/find/v3/device/status/list"
API_STATUS = "https://i.mi.com/find/v3/device/status"


def _detail_target():
    """从详情页 URL 里取出 (targetUserId, targetFid)。"""
    parts = DETAIL_URL.split("?")[0].rstrip("/").split("/")
    try:
        i = parts.index("fe")
        return parts[i + 1], parts[i + 2]
    except (ValueError, IndexError):
        return None, None


TARGET_USER_ID, TARGET_FID = _detail_target()


class MiLoginError(RuntimeError):
    pass


def load_credentials():
    try:
        with open(CRED_PATH, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def save_credentials(**kw):
    data = load_credentials()
    data.update({k: v for k, v in kw.items() if v is not None})
    tmp = CRED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, CRED_PATH)
    return data


def _find_browser():
    for p in (
        os.environ.get("MI_BROWSER"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if p and os.path.exists(p):
            return p
    return None


def browser_login(account=None, password=None, headless=None, timeout=120):
    """内置浏览器登录小米账号并取回 serviceToken（保存到 credentials.json）。"""
    cred = load_credentials()
    account = account or cred.get("account") or os.environ.get("MI_ACCOUNT")
    password = password or cred.get("password") or os.environ.get("MI_PASSWORD")
    if not account or not password:
        raise MiLoginError("缺少账号/密码：用 --account/--password 或写进 credentials.json")
    if headless is None:
        headless = bool(int(os.environ.get("MI_HEADLESS", "0")))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise MiLoginError(
            "未安装 playwright，无法自动登录：pip install playwright；"
            "或把浏览器里的 serviceToken/userId 填进 credentials.json") from exc

    exe = _find_browser()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=headless,
            executable_path=exe,
            args=["--no-first-run", "--no-default-browser-check"],
            ignore_https_errors=True,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(DETAIL_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            if "account.xiaomi.com" in page.url:
                page.fill("input[name=account]", account)
                page.fill("input[name=password]", password)
                page.click("button[type=submit]")
                page.wait_for_timeout(2500)
                btn = page.get_by_role("button", name="同意并继续")
                if btn.count():
                    btn.first.click()
                end = time.time() + timeout
                while time.time() < end and "account.xiaomi.com" in page.url:
                    page.wait_for_timeout(1000)
            if "i.mi.com" not in page.url:
                raise MiLoginError(f"登录未完成，当前页面：{page.url}")
            cookies = {c["name"]: c for c in ctx.cookies("https://i.mi.com")}
            token = cookies.get("serviceToken", {}).get("value")
            uid = cookies.get("userId", {}).get("value")
            expires = cookies.get("serviceToken", {}).get("expires") or -1
            if not token or not uid:
                raise MiLoginError("登录后没取到 serviceToken/userId")
            save_credentials(
                account=account, password=password, service_token=token,
                user_id=str(uid), expires_at=float(expires) if expires > 0 else None,
                saved_at=time.time())
            return token, str(uid)
        finally:
            ctx.close()


class MiCloud(object):
    def __init__(self, account=None, password=None, headless=None):
        self.account = account
        self.password = password
        self.headless = headless
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Referer": "https://i.mi.com/find/"})

    # ---------- 凭据 ----------
    def _cached(self):
        cred = load_credentials()
        return cred.get("service_token"), cred.get("user_id"), cred.get("expires_at")

    def ensure_login(self):
        token, uid, exp = self._cached()
        if token and uid:
            if exp and exp < time.time():
                pass  # 过期就让接口 401 去触发重新登录
            else:
                return token, uid
        token, uid = browser_login(self.account, self.password, self.headless)
        return token, uid

    # ---------- 接口 ----------
    def _get(self, url, params=None, token=None, uid=None):
        headers = {"Cookie": f"serviceToken={token}; userId={uid}"}
        return self.session.get(url, params=params, headers=headers, timeout=20)

    def raw_devices(self, retry=True):
        token, uid = self.ensure_login()
        r = self._get(API_LIST, {"ts": int(time.time() * 1000)}, token, uid)
        if r.status_code == 401 and retry:
            # serviceToken 失效：重新登录一次
            token, uid = browser_login(self.account, self.password, self.headless)
            r = self._get(API_LIST, {"ts": int(time.time() * 1000)}, token, uid)
        r.raise_for_status()
        js = r.json()
        if js.get("result") != "ok":
            raise MiLoginError(f"接口返回异常：{js}")
        return js.get("data", {})

    def devices(self, retry=True):
        return [parse_device(d) for d in self.raw_devices(retry).get("deviceList", [])]

    def raw_status(self, retry=True):
        """单台设备详情（网页 /0/detail 用的同一接口，比 list 更贴近当前状态）。"""
        token, uid = self.ensure_login()
        params = {"ts": int(time.time() * 1000), "targetUserId": TARGET_USER_ID,
                  "targetFid": TARGET_FID, "syncMode": 2}
        r = self._get(API_STATUS, params, token, uid)
        if r.status_code == 401 and retry:
            token, uid = browser_login(self.account, self.password, self.headless)
            r = self._get(API_STATUS, params, token, uid)
        r.raise_for_status()
        js = r.json()
        if js.get("result") != "ok":
            raise MiLoginError(f"接口返回异常：{js}")
        return js.get("data") or {}

    def device_status(self, retry=True):
        return parse_device(self.raw_status(retry))

    def find_device(self, name=None, index=0, retry=True):
        devs = self.devices(retry)
        if not devs:
            raise MiLoginError("账号下没有设备")
        if name:
            key = str(name).strip().lower()
            for d in devs:
                if key in (d.get("name") or "").lower() or key in (d.get("fid") or "").lower():
                    return d
            raise MiLoginError(f"找不到设备：{name}（可用：{', '.join(d.get('name','?') for d in devs)}）")
        # 未指定设备：优先选有定位数据的那台
        for d in devs:
            if d.get("gps"):
                return d
        return devs[min(index, len(devs) - 1)]

    def credentials_state(self):
        return credentials_state()


def credentials_state():
    cred = load_credentials()
    exp = cred.get("expires_at")
    left = None if not exp else int(exp - time.time())
    return {"has_token": bool(cred.get("service_token")), "expires_at": exp,
            "expires_in_sec": left, "saved_at": cred.get("saved_at")}


def _model_name(dev):
    info = (dev.get("componentModelInfo") or {}).get("0") or {}
    raw = ((info.get("modelName") or {}).get("zhCN") or "")
    try:
        return json.loads(raw).get("modelName") or raw
    except ValueError:
        return raw


def parse_device(dev):
    comp = (dev.get("componentList") or [{}])[0]
    locs = comp.get("locationList") or []
    gps = {}
    address = ""
    updated = None
    accuracy = None
    if locs:
        updated = locs[0].get("clientUpdateTime")
        for item in locs[0].get("gpsInfoTransformed") or []:
            t = item.get("coordinateType")
            if t:
                gps[t] = {"lat": item.get("latitude"), "lng": item.get("longitude")}
            if not address:
                address = item.get("address") or ""
            if accuracy is None:
                accuracy = item.get("accuracy")
    battery = comp.get("batteryInfo") or {}
    return {
        "fid": dev.get("fid"),
        "name": _model_name(dev) or dev.get("deviceType"),
        "type": dev.get("deviceType"),
        "online": bool(comp.get("online")),
        "status": dev.get("status"),
        "battery": battery.get("level"),
        "address": address,
        "updated_ms": updated,
        "accuracy": accuracy,
        "gps": gps,          # {'autonavi': gcj02, 'baidu': bd09, 'google': wgs84?}
        "share_location": dev.get("shareLocation"),
    }


if __name__ == "__main__":
    cloud = MiCloud()
    if "--login" in sys.argv:
        print("登录结果：", browser_login())
    name = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    print(json.dumps(cloud.find_device(name), ensure_ascii=False, indent=2))
    print("凭据状态：", json.dumps(credentials_state(), ensure_ascii=False))
