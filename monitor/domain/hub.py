"""
TelemetryHub — 裝置狀態管理與瀏覽器廣播層
==========================================
接收層（server.py 的 TCP ingest）把解析好的訊息交給這裡；
這裡維護每台裝置的最新狀態，並廣播給所有瀏覽器 viewer。

日後擴充（歷史資料儲存、斷線通知、錄製檔接收…）都掛在這一層，
不動接收層。
"""

import asyncio
import json
import logging
import time

PROTO_VERSION = 1

# 有狀態的訊息類型：保留最新一則，供 snapshot 給新連入的瀏覽器。
# 擴充新類型（依 PROTOCOL.md 先定義）只需在此加一項，Hub 其餘程式不用動。
STATEFUL_TYPES = ("status", "device_info", "params", "alarm")

# 串流類型：即時轉發、不保留
STREAM_TYPES = ("wave",)


class DeviceState:
    """單一 Pi 裝置的最新狀態（供 snapshot 用）"""

    def __init__(self, device: str):
        self.device = device
        self.patient = ""
        self.online = False
        self.last_seen = 0.0
        self.conn_seq = 0          # 連線世代：舊 TCP 連線的殘留事件不得覆蓋新連線
        self.proto = None
        self.latest = {}           # type -> 最新一則訊息（僅 STATEFUL_TYPES）


class TelemetryHub:
    def __init__(self, offline_timeout: float = 5.0):
        self.log = logging.getLogger("hub")
        self.offline_timeout = offline_timeout
        self.devices = {}          # device_id -> DeviceState
        self.viewers = set()       # 每個瀏覽器一個 asyncio.Queue
        self._seq = 0

    # ── Pi 端事件（由 ingest 呼叫）──────────────────────────────────

    def device_hello(self, msg: dict):
        """裝置上線。回傳 (device_id, conn_seq) 給該連線後續使用。"""
        device = str(msg.get("device") or "unknown")
        st = self.devices.get(device)
        if st is None:
            st = self.devices[device] = DeviceState(device)
        self._seq += 1
        st.conn_seq = self._seq
        st.patient = str(msg.get("patient") or "")
        st.proto = msg.get("v")
        st.online = True
        st.last_seen = time.time()
        if st.proto != PROTO_VERSION:
            self.log.warning(f"{device} 協議版本 {st.proto} 與伺服器 {PROTO_VERSION} 不同")
        # 資安規則：病人代碼只顯示在儀表板畫面，禁止寫入 log
        self.log.info(f"裝置上線: {device}")
        self.broadcast({"type": "link", "device": device, "online": True,
                        "patient": st.patient, "v": st.proto})
        return device, st.conn_seq

    def device_message(self, device: str, conn_seq: int, msg: dict):
        st = self.devices.get(device)
        if st is None or st.conn_seq != conn_seq:
            return                       # 已被新連線取代的殘留訊息
        st.last_seen = time.time()
        if not st.online:                # watchdog 誤判後資料又進來 → 回復上線
            st.online = True
            self.broadcast({"type": "link", "device": device, "online": True,
                            "patient": st.patient, "v": st.proto})
        t = msg.get("type")
        if t == "ping":
            return                       # 心跳只更新 last_seen，不轉發
        if t in STATEFUL_TYPES:
            st.latest[t] = msg
        elif t not in STREAM_TYPES:
            self.log.info(f"{device} 未知訊息類型（忽略）: {t}")
            return
        self.broadcast(dict(msg, device=device))

    def device_disconnected(self, device, conn_seq):
        if device is None:
            return
        st = self.devices.get(device)
        if st is None or st.conn_seq != conn_seq or not st.online:
            return                       # 已被新連線取代
        st.online = False
        self.log.info(f"裝置離線: {device}")
        self.broadcast({"type": "link", "device": device, "online": False})

    async def watchdog(self):
        """TCP 沒斷但資料停了（例如 Wi-Fi 半死）→ 逾時判離線"""
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            for st in self.devices.values():
                if st.online and now - st.last_seen > self.offline_timeout:
                    st.online = False
                    self.log.warning(f"裝置逾時離線: {st.device}")
                    self.broadcast({"type": "link", "device": st.device, "online": False})

    # ── 瀏覽器端 ────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """新瀏覽器連上時的完整狀態（波形不補：幾秒內就會填滿）"""
        devs = []
        for st in self.devices.values():
            d = {"device": st.device, "patient": st.patient,
                 "online": st.online, "v": st.proto}
            d.update(st.latest)          # 各 STATEFUL_TYPES 的最新一則
            devs.append(d)
        return {"type": "snapshot", "devices": devs}

    def add_viewer(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=300)
        self.viewers.add(q)
        self.log.info(f"瀏覽器連入（目前 {len(self.viewers)} 個觀看端）")
        return q

    def remove_viewer(self, q: asyncio.Queue):
        self.viewers.discard(q)
        self.log.info(f"瀏覽器離開（目前 {len(self.viewers)} 個觀看端）")

    def broadcast(self, msg: dict):
        """非阻塞廣播：某個觀看端塞滿（網路慢）→ 丟它最舊的訊息，絕不回壓"""
        if not self.viewers:
            return
        line = json.dumps(msg, ensure_ascii=False)
        for q in list(self.viewers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(line)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
