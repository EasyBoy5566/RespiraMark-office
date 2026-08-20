"""
fake_pi — 模擬一台 Pi 推送擬真呼吸波形（開發測試用，純標準庫）
================================================================
用法：
    python fake_pi.py                                   # 本機自動登記 pi-01 並連線
    python fake_pi.py --device fake-02 --patient TEST002
    python fake_pi.py --number 20                       # 一個指令開 20 台（pi-01..pi-20）
    python fake_pi.py --device test-01 -n 20 --alarms
    python fake_pi.py --host 192.168.0.50               # 跨機器測試（在真 Pi 上也能跑）
    python fake_pi.py --pair --device pi-new            # 模擬配對申請（演練 /admin 核可畫面）

--number/-n 大於 1 時，會依 --device / --patient 尾碼自動編號同時開多台（各一條執行緒）。
不想用 --number 也可以自己多開幾個不同 --device 的行程。Ctrl+C 全部結束。

連本機、未傳 --token 且伺服器已有 devices.json 時，模擬器會自動為本次執行產生
一組共用臨時 token，將雜湊寫入 devices.json 後立即連線，明文不落地。此自動流程只適用
127.0.0.1/::1/localhost，且不會覆寫不是由 fake_pi 建立的既有裝置。
"""

import argparse
import copy
import ipaddress
import json
import math
import os
import random
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from monitor.crypto import hash_password  # noqa: E402

SAMPLE_RATE = 100        # Hz，與 Pi 端實際波形速率同量級
FLUSH_INTERVAL = 0.15    # 秒，打包間隔（與 telemetry_client 相同）
PARAMS_INTERVAL = 5.0    # 秒，慢數據間隔
SYS_INTERVAL = 5.0       # 秒，系統狀態間隔（與 telemetry_client 相同）
DEFAULT_DEVICES_FILE = os.path.join(PROJECT_ROOT, "devices.json")
AUTO_REGISTER_NOTE = "fake_pi 本機自動註冊"
TOKEN_BYTES = 24

ALARM_POOL = (
    {"prio": 28, "code": "10", "cp": 1, "text": "PAW HIGH"},
    {"prio": 3, "code": "9C", "cp": 2, "text": "LEAKAGE"},
    {"prio": 12, "code": "93", "cp": 2, "text": "APNEA VENT"},
)


class FakeSys:
    """模擬 Pi 系統狀態隨機漫步（CPU/溫度緩慢變動）；--sys-hot 模擬過熱高載"""

    def __init__(self, hot=False):
        self.hot = hot
        self.start = time.time() - 3600     # 假裝已開機 1 小時
        self.cpu = 60.0 if hot else 15.0

    def sample(self):
        self.cpu = max(3.0, min(99.0, self.cpu + random.uniform(-6, 6)))
        temp = (72.0 if self.hot else 50.0) + random.uniform(-3, 7)
        return {
            "type": "sys",
            "cpu": round(self.cpu, 1),
            "mem": round(random.uniform(36, 48), 1),
            "temp": round(temp, 1),
            "disk_pct": round(random.uniform(60, 64), 1),
            "disk_free": round(random.uniform(11.5, 12.5), 1),
            "throttled": "0x50005" if self.hot else "0x0",
            "uptime": round(time.time() - self.start, 0),
        }


class BreathModel:
    """簡化的呼吸力學模型：吸氣上升 → 平台 → 呼氣指數衰減"""

    def __init__(self, rr=15.0, peep=5.0, pip=22.0, vt=450.0):
        self.rr, self.peep, self.pip, self.vt = rr, peep, pip, vt
        self._new_cycle()
        self.t = random.uniform(0, self.period)   # 各床相位錯開

    def _new_cycle(self):
        self.period = 60.0 / self.rr * random.uniform(0.97, 1.03)
        self.ti = self.period * 0.33
        self.cur_pip = self.pip * random.uniform(0.96, 1.04)
        self.cur_vt = self.vt * random.uniform(0.95, 1.05)

    def step(self, dt):
        """回傳 (壓力 cmH2O, 流量 L/min, 容積 mL, trigger)"""
        self.t += dt
        trigger = False
        if self.t >= self.period:
            self.t -= self.period
            self._new_cycle()
            trigger = True
        n = lambda a: random.uniform(-a, a)
        if self.t < self.ti:                       # 吸氣
            k = self.t / 0.25
            p = self.peep + (self.cur_pip - self.peep) * (1 - math.exp(-k))
            f = 42.0 * math.exp(-self.t / 0.45)
            v = self.cur_vt * (1 - math.exp(-self.t / 0.35))
        else:                                      # 呼氣
            te = self.t - self.ti
            p = self.peep + (self.cur_pip - self.peep) * math.exp(-te / 0.22)
            f = -48.0 * math.exp(-te / 0.35)
            v = self.cur_vt * math.exp(-te / 0.45)
        return p + n(0.25), f + n(0.6), max(0.0, v + n(2.0)), trigger

    def params(self):
        return {
            "type": "params",
            "mode": "VC-SIMV",
            "features": [],          # 特性旗標（如 /AF AutoFlow）；本模擬器暫不送，避免模式字串過長
            "settings": {
                "FiO2": "40", "VTi": "0.450", "RR": f"{self.rr:.0f}",
                "PEEP": f"{self.peep:.0f}", "Ti": "1.3",
            },
            "measured": {
                "VT": f"{self.cur_vt:.0f}", "RR": f"{self.rr + random.uniform(-0.6, 0.6):.1f}",
                "PEEP": f"{self.peep + random.uniform(-0.2, 0.2):.1f}",
                "PIP": f"{self.cur_pip:.1f}",
                "Pmean": f"{(self.peep + self.cur_pip) / 2 - 3 + random.uniform(-0.4, 0.4):.1f}",
                "FiO2": f"{40 + random.uniform(-0.5, 0.5):.0f}",
                "MVe": f"{self.cur_vt * self.rr / 1000 + random.uniform(-0.2, 0.2):.1f}",
                "etCO2": f"{38 + random.uniform(-1.5, 1.5):.0f}",
                "Pplat": f"{self.cur_pip - 3:.1f}",
                "Cdyn": f"{random.uniform(38, 44):.0f}",
                "R": f"{random.uniform(9, 12):.1f}",
                "leak": f"{random.uniform(0, 3):.0f}",
            },
        }


def send_lines(sock, msgs):
    ts = round(time.time(), 3)
    payload = "".join(
        json.dumps(dict(m, ts=ts), ensure_ascii=False, separators=(",", ":")) + "\n"
        for m in msgs
    )
    sock.sendall(payload.encode("utf-8"))


def random_alarms():
    """隨機挑選至少一種作用中的警報，避免每次都顯示相同組合。"""
    count = random.randint(1, len(ALARM_POOL))
    return [dict(alarm) for alarm in random.sample(ALARM_POOL, count)]


def random_alarm_interval(args):
    """下一次警報狀態變化前的隨機秒數。"""
    return random.uniform(args.alarm_interval_min, args.alarm_interval_max)


def should_trigger_alarm(probability, force=False):
    """無作用中警報時，依機率決定是否觸發；force 供 smoke test 強制首發。"""
    return force or random.random() < probability


def run_session(args, model, sysmodel):
    sock = socket.create_connection((args.host, args.port), timeout=3.0)
    if args.tls_ca:
        # 與 Pi 端 telemetry_client 相同的 CA 釘選 TLS（測試伺服器加密路徑用）
        ctx = ssl.create_default_context(cafile=args.tls_ca)
        sock = ctx.wrap_socket(sock, server_hostname=args.host)
    sock.settimeout(2.0)
    # 關閉 Nagle：小批次立即送出，避免黏包造成到達時間抖動
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"[{args.device}] 已連線 {args.host}:{args.port}")
    # app_version 刻意標成 fake：管理頁一眼就能分出哪幾台是模擬器
    hello = {"type": "hello", "v": 1, "device": args.device,
             "patient": args.patient, "app_version": "1.0.0-fake"}
    if args.token:
        hello["token"] = args.token
    send_lines(sock, [
        hello,
        {"type": "status", "state": "connected", "msg": "已連線"},
        {"type": "device_info", "info": {"id": "5030", "name": "Savina 300 (Fake)",
                                         "revision": "9.99", "medibus": "6.00"}},
        model.params(),
        sysmodel.sample(),          # 立刻發一則 sys，讓 snapshot 即有系統狀態
    ])

    batch = []
    next_sample = time.monotonic()
    next_flush = next_sample + FLUSH_INTERVAL
    next_params = next_sample + PARAMS_INTERVAL
    next_sys = next_sample + SYS_INTERVAL
    next_alarm = None
    if args.alarms:
        # 預設讓每台裝置錯開發生；smoke test 可用 --alarm-immediate 保留立即觸發。
        first_delay = 0.0 if args.alarm_immediate else random_alarm_interval(args)
        next_alarm = next_sample + first_delay
    alarm_on = False
    force_alarm = args.alarm_immediate
    dt = 1.0 / SAMPLE_RATE

    while True:
        now = time.monotonic()
        while next_sample <= now:                  # 補齊到當下的所有樣本
            batch.append(model.step(dt))
            next_sample += dt
        if now >= next_flush and batch:
            send_lines(sock, [{
                "type": "wave",
                "p": [round(s[0], 2) for s in batch],
                "f": [round(s[1], 2) for s in batch],
                "v": [round(s[2], 1) for s in batch],
                "trig": [i for i, s in enumerate(batch) if s[3]],
            }])
            batch = []
            next_flush = now + FLUSH_INTERVAL
        if now >= next_params:
            send_lines(sock, [model.params()])
            next_params = now + PARAMS_INTERVAL
        if now >= next_sys:
            send_lines(sock, [sysmodel.sample()])
            next_sys = now + SYS_INTERVAL
        if next_alarm is not None and now >= next_alarm:
            alarms = None
            if alarm_on:
                alarm_on = False
                alarms = []
            elif should_trigger_alarm(args.alarm_probability, force_alarm):
                alarm_on = True
                alarms = random_alarms()
            force_alarm = False
            if alarms is not None:
                send_lines(sock, [{"type": "alarm", "alarms": alarms}])
                print(f"[{args.device}] 警報 {'觸發' if alarm_on else '解除'}: "
                      f"{[a['text'] for a in alarms] or '—'}")
            next_alarm = now + random_alarm_interval(args)
        time.sleep(0.01)


def run_device(args):
    """單台裝置的連線主迴圈：斷線自動重連（KeyboardInterrupt 交給呼叫端收尾）"""
    model = BreathModel(rr=args.rr)
    sysmodel = FakeSys(hot=args.sys_hot)
    while True:
        try:
            run_session(args, model, sysmodel)
        except OSError as e:
            print(f"[{args.device}] 連線失敗/中斷（2 秒後重試）: {e}")
            time.sleep(2.0)


def _expand_series(base, count):
    """把 base 依尾碼數字展開成 count 個名稱（保留原本的位數寬度）：
    test-01 → test-01, test-02, …；TEST001 → TEST001, TEST002, …；
    沒有尾碼數字則補 -01, -02（如 ICU → ICU-01, ICU-02）"""
    m = re.match(r"^(.*?)(\d+)$", base)
    if m:
        prefix, num = m.group(1), m.group(2)
        start, width = int(num), len(num)
        return [f"{prefix}{i:0{width}d}" for i in range(start, start + count)]
    return [f"{base}-{i:02d}" for i in range(1, count + 1)]


def build_device_args(args):
    """--number > 1 時，依 --device / --patient 尾碼自動編號成多台各自的 args"""
    names = _expand_series(args.device, args.number)
    patients = _expand_series(args.patient, args.number)
    out = []
    for dev, pat in zip(names, patients):
        da = copy.copy(args)
        da.device, da.patient = dev, pat
        # 多床時各床 RR 微幅錯開，波形不整齊劃一（更接近真實多床畫面）
        da.rr = max(8.0, args.rr + random.uniform(-3, 3))
        out.append(da)
    return out


def _is_loopback_host(host):
    """--register-local 只能改本機伺服器的權杖檔，禁止拿來連遠端主機。"""
    host = str(host or "").strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def register_local_devices(device_args, devices_file):
    """為本次本機 fake_pi 建立一組共用臨時 token，並寫回各裝置 args。

    devices.json 只保存 PBKDF2 雜湊；明文 token 僅存在本次程序記憶體。
    為避免誤換正式 Pi 權杖，既有項目只有帶 AUTO_REGISTER_NOTE 才能覆寫。
    模擬裝置不需要正式 Pi 的逐台權杖隔離，因此整批只計算一次昂貴的
    PBKDF2 雜湊，避免啟動 20 台時重複計算 20 次。
    """
    if not device_args or any(not _is_loopback_host(da.host) for da in device_args):
        raise ValueError("--register-local 只允許 --host 127.0.0.1、::1 或 localhost")
    if not os.path.isfile(devices_file):
        raise ValueError(
            f"找不到裝置權杖檔 {devices_file}；"
            "此選項不會自行開啟 devices.json 模式")

    try:
        with open(devices_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"無法讀取裝置權杖檔 {devices_file}: {e}") from e

    devices = data.get("devices")
    if not isinstance(devices, list):
        raise ValueError(f"裝置權杖檔格式錯誤：{devices_file} 缺少 devices 陣列")

    by_id = {d.get("device_id"): (i, d) for i, d in enumerate(devices)
             if isinstance(d, dict)}
    conflicts = []
    for da in device_args:
        found = by_id.get(da.device)
        if found is not None and found[1].get("note") != AUTO_REGISTER_NOTE:
            conflicts.append(da.device)
    if conflicts:
        names = ", ".join(conflicts)
        raise ValueError(
            f"拒絕覆寫既有裝置 {names}；請改用 fake-/test- 開頭的新 ID，"
            "或使用該裝置原有的 --token")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    token_hash = hash_password(token)
    for da in device_args:
        entry = {
            "device_id": da.device,
            "note": AUTO_REGISTER_NOTE,
            "enabled": True,
            "token_hash": token_hash,
        }
        found = by_id.get(da.device)
        if found is None:
            devices.append(entry)
        else:
            devices[found[0]] = entry

    try:
        with open(devices_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as e:
        raise ValueError(f"無法寫入裝置權杖檔 {devices_file}: {e}") from e

    for da in device_args:
        da.token = token


def should_register_local(args):
    """本機已有 devices.json 且未手動給 token 時，自動準備測試裝置權杖。"""
    return bool(
        args.register_local
        or (not args.token
            and _is_loopback_host(args.host)
            and os.path.isfile(args.devices_file))
    )


def run_pairing(host: str, web_port: int, device: str, tls_ca: str = ""):
    """模擬 Pi 端的配對申請，讓開發者不用真機也能演練 /admin 的核可畫面。

    刻意不共用 respiramark-pi 的 pairing.py（兩個專案彼此獨立，唯一的耦合是
    PROTOCOL.md）；這裡只是最精簡的協議實作，不含 Pi 端的重試與狀態機。

    給了 --tls-ca 就走 https 並以該 CA 驗證伺服器憑證，與真機一致。
    """
    ctx = ssl.create_default_context(cafile=tls_ca) if tls_ca else None
    base = f"{'https' if ctx else 'http'}://{host}:{web_port}"

    def call(path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data, method="POST" if data else "GET",
            headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=5.0, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        pair = call("/api/pair/request", {"device_id": device, "note": "fake_pi 模擬配對"})
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"配對申請失敗（伺服器未啟動或未開放配對？）: {e}")
        sys.exit(1)

    print(f"已送出配對申請：{device}")
    print(f"  確認碼：{pair['code']}   （請到 {base}/admin 核對並核可）")
    print("  等待管理員處理…（Ctrl+C 取消）")

    deadline = time.time() + float(pair.get("expires_in") or 600)
    while time.time() < deadline:
        time.sleep(float(pair.get("poll_interval") or 3))
        try:
            body = call(f"/api/pair/poll/{pair['pair_id']}")
        except (urllib.error.URLError, OSError, ValueError):
            continue                      # 短暫網路問題 → 下一輪再試
        status = body.get("status")
        if status == "approved":
            print("\n配對成功！真正的 Pi 會把這些自動寫進自己的 telemetry.json：")
            print(f'  "device_id": "{body["device_id"]}"')
            print(f'  "server_port": {body["server_port"]}')
            print(f'  "token": "{body["token"]}"')
            print("\n接著可以用它連線送波形：")
            print(f"  python tools/fake_pi.py --device {body['device_id']} "
                  f"--token {body['token']} --host {host}")
            return
        if status == "denied":
            print("\n管理員拒絕了這次配對申請")
            return
        if status == "expired":
            print("\n配對已失效（逾時或伺服器重啟），請重新申請")
            return
    print("\n配對逾時")


def main():
    ap = argparse.ArgumentParser(description="模擬一台或多台 Pi 推送呼吸波形")
    ap.add_argument("--device", default="pi-01")
    ap.add_argument("--patient", default="TEST001")
    ap.add_argument("--number", "-n", type=int, default=1,
                    help="一次模擬幾台裝置（依尾碼自動編號，如 --device test-01 -n 20 → test-01..test-20）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--rr", type=float, default=15.0, help="呼吸頻率 (預設 15；多床時各床會微幅錯開)")
    ap.add_argument("--token", default="",
                    help="hello 的存取權杖（共用或該 --device 的獨立 token）")
    ap.add_argument("--register-local", action="store_true",
                    help="明確啟用本機測試裝置註冊；一般本機 devices.json 模式會自動啟用")
    ap.add_argument("--devices-file", default=DEFAULT_DEVICES_FILE,
                    help="--register-local 寫入的裝置權杖檔（預設：專案根目錄 devices.json）")
    ap.add_argument("--alarms", action="store_true",
                    help="模擬警報：每次檢查依機率觸發，警報種類隨機（開發警報 UI 用）")
    ap.add_argument("--alarm-probability", type=float, default=0.03,
                    help="無作用中警報時，每次檢查的觸發機率（預設 0.03 = 3%%）")
    ap.add_argument("--alarm-interval-min", type=float, default=5.0,
                    help="警報觸發或解除前的最短秒數（預設 5）")
    ap.add_argument("--alarm-interval-max", type=float, default=20.0,
                    help="警報觸發或解除前的最長秒數（預設 20）")
    ap.add_argument("--alarm-immediate", action="store_true",
                    help="第一則警報立即觸發；後續仍使用隨機間隔（自動測試用）")
    ap.add_argument("--sys-hot", action="store_true",
                    help="模擬 Pi 過熱高載：溫度/CPU 偏高且降頻旗標非 0（測系統狀態示警配色用）")
    ap.add_argument("--tls-ca", default="",
                    help="TLS 連線：伺服器自建 CA 憑證（ca.pem）路徑；省略 = 明文")
    ap.add_argument("--pair", action="store_true",
                    help="不送波形，改走裝置配對流程：申請 → 顯示確認碼 → 等管理員在 "
                         "/admin 核可 → 印出領到的 token（供演練配對 UI）")
    ap.add_argument("--web-port", type=int, default=0,
                    help="--pair 用的網頁 port（伺服器 config.json 的 web_port）；"
                         "省略時依有無 --tls-ca 取 443 或 8080")
    args = ap.parse_args()

    if args.pair:
        web_port = args.web_port or (443 if args.tls_ca else 8080)
        run_pairing(args.host, web_port, args.device, args.tls_ca)
        return

    if (not math.isfinite(args.alarm_interval_min)
            or not math.isfinite(args.alarm_interval_max)
            or args.alarm_interval_min <= 0
            or args.alarm_interval_max < args.alarm_interval_min):
        ap.error("警報間隔必須大於 0，且 --alarm-interval-max 不得小於 --alarm-interval-min")
    if (not math.isfinite(args.alarm_probability)
            or not 0.0 <= args.alarm_probability <= 1.0):
        ap.error("--alarm-probability 必須介於 0 與 1 之間")
    if args.number < 1:
        ap.error("--number 必須至少為 1")
    if args.register_local and args.token:
        ap.error("--register-local 與 --token 不能同時使用")

    dev_args = build_device_args(args) if args.number > 1 else [args]
    if should_register_local(args):
        try:
            register_local_devices(dev_args, args.devices_file)
        except ValueError as e:
            ap.error(str(e))
        print(f"已為 {len(dev_args)} 台本機模擬裝置建立共用的本次臨時權杖"
              "（devices.json 僅保存雜湊）")

    # 單台：直接在主執行緒跑，Ctrl+C 立即結束（行為與原本相同）
    if args.number <= 1:
        try:
            run_device(dev_args[0])
        except KeyboardInterrupt:
            print(f"\n[{args.device}] 結束")
        return

    # 多台：每台一條 daemon 執行緒，Ctrl+C 由主執行緒統一收尾
    for da in dev_args:
        threading.Thread(target=run_device, args=(da,), daemon=True).start()
        time.sleep(0.05)   # 錯開連線與波形相位，避免同時湧入
    print(f"已啟動 {len(dev_args)} 台模擬裝置："
          f"{dev_args[0].device} … {dev_args[-1].device}（Ctrl+C 全部結束）")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n全部結束")


if __name__ == "__main__":
    main()
