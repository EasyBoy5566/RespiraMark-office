"""
fake_pi — 模擬一台 Pi 推送擬真呼吸波形（開發測試用，純標準庫）
================================================================
用法：
    python fake_pi.py                                   # 預設 pi-01 / TEST001 → 127.0.0.1
    python fake_pi.py --device pi-02 --patient A123456
    python fake_pi.py --host 192.168.0.50               # 跨機器測試（在真 Pi 上也能跑）

多開幾個（不同 --device）就能模擬多床畫面。Ctrl+C 結束。
"""

import argparse
import json
import math
import random
import socket
import time

SAMPLE_RATE = 100        # Hz，與 Pi 端實際波形速率同量級
FLUSH_INTERVAL = 0.15    # 秒，打包間隔（與 telemetry_client 相同）
PARAMS_INTERVAL = 5.0    # 秒，慢數據間隔


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
            "features": ["/AF"],
            "settings": {
                "FiO2": "40", "VTi": "0.450", "RR": f"{self.rr:.0f}",
                "PEEP": f"{self.peep:.0f}", "Ti": "1.3", "Pmax": "35",
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


def run_session(args, model):
    sock = socket.create_connection((args.host, args.port), timeout=3.0)
    sock.settimeout(2.0)
    # 關閉 Nagle：小批次立即送出，避免黏包造成到達時間抖動
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"[{args.device}] 已連線 {args.host}:{args.port}")
    send_lines(sock, [
        {"type": "hello", "v": 1, "device": args.device, "patient": args.patient},
        {"type": "status", "state": "connected", "msg": "已連線（模擬）"},
        {"type": "device_info", "info": {"id": "5030", "name": "Savina 300 (Fake)",
                                         "revision": "9.99", "medibus": "6.00"}},
        model.params(),
    ])

    batch = []
    next_sample = time.monotonic()
    next_flush = next_sample + FLUSH_INTERVAL
    next_params = next_sample + PARAMS_INTERVAL
    next_alarm = next_sample if args.alarms else None   # 啟用時立刻發第一則
    alarm_on = False
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
        if next_alarm is not None and now >= next_alarm:
            alarm_on = not alarm_on                # 每 20 秒切換 觸發/解除
            alarms = []
            if alarm_on:
                alarms.append({"prio": 28, "code": "10", "text": "PAW HIGH"})
                if random.random() < 0.5:
                    alarms.append({"prio": 12, "code": "4B", "text": "BATTERY LOW"})
            send_lines(sock, [{"type": "alarm", "alarms": alarms}])
            print(f"[{args.device}] 警報 {'觸發' if alarm_on else '解除'}: "
                  f"{[a['text'] for a in alarms] or '—'}")
            next_alarm = now + 20.0
        time.sleep(0.01)


def main():
    ap = argparse.ArgumentParser(description="模擬一台 Pi 推送呼吸波形")
    ap.add_argument("--device", default="pi-01")
    ap.add_argument("--patient", default="TEST001")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--rr", type=float, default=15.0, help="呼吸頻率 (預設 15)")
    ap.add_argument("--alarms", action="store_true",
                    help="模擬警報：啟動即觸發，之後每 20 秒切換觸發/解除（開發警報 UI 用）")
    args = ap.parse_args()

    model = BreathModel(rr=args.rr)
    while True:
        try:
            run_session(args, model)
        except KeyboardInterrupt:
            print(f"\n[{args.device}] 結束")
            return
        except OSError as e:
            print(f"[{args.device}] 連線失敗/中斷（2 秒後重試）: {e}")
            time.sleep(2.0)


if __name__ == "__main__":
    main()
