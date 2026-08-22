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
import os
import time
from collections import deque

from monitor.audit import audit
from monitor.domain.alarm_log import AlarmLog
from monitor.domain.sys_log import SysLog

PROTO_VERSION = 1

# 有狀態的訊息類型：保留最新一則，供 snapshot 給新連入的瀏覽器。
# 擴充新類型（依 PROTOCOL.md 先定義）只需在此加一項，Hub 其餘程式不用動。
STATEFUL_TYPES = ("status", "device_info", "params", "alarm", "sys")

# 串流類型：即時轉發、不保留
STREAM_TYPES = ("wave",)

class DeviceState:
    """單一 Pi 裝置的最新狀態（供 snapshot 用）"""

    def __init__(self, device: str, sys_history_max: int = 720):
        self.device = device
        self.patient = ""
        self.online = False
        self.last_seen = 0.0
        # 本次連線建立（收到合法 hello）的時刻，供管理頁顯示連線時長。
        # 只在 hello 設定：watchdog 誤判後資料又進來而回復上線時不重設，
        # 否則一次網路抖動就會讓連線時長歸零而失真（見 PROTOCOL.md）。
        self.connected_at = 0.0
        self.app_version = ""      # Pi 端軟體版本；舊版 Pi 不送 → 保持空字串
        self.conn_seq = 0          # 連線世代：舊 TCP 連線的殘留事件不得覆蓋新連線
        self.proto = None
        self.latest = {}           # type -> 最新一則訊息（僅 STATEFUL_TYPES）
        self.sys_history = deque(maxlen=sys_history_max)  # 近段 sys 樣本（趨勢圖用）
        self.sys_persist_last_ts = 0.0  # 上次寫入 SQLite 的伺服器時間（節流用）


class TelemetryHub:
    def __init__(self, offline_timeout: float = 5.0, max_devices: int = 16,
                 sys_history_max: int = 720, sys_db_path: str = "",
                 sys_persist_interval: float = 60.0, sys_retention_days: int = 7,
                 max_viewers: int = 50,
                 alarm_db_path: str = "", alarm_retention_days: int = 7,
                 alarm_log_dir: str = None, directory=None):
        self.log = logging.getLogger("hub")
        # 裝置清冊（床號／財編）；None = 未提供，snapshot 一律回空字串
        self.directory = directory
        self.offline_timeout = offline_timeout
        self.max_devices = max_devices
        self.max_viewers = max_viewers
        self.sys_history_max = sys_history_max
        # 只放慢長期落地；即時畫面與記憶體歷史仍隨 Pi 送出頻率更新。
        self.sys_persist_interval = max(0.0, float(sys_persist_interval))
        self.sys_log = SysLog(sys_db_path, sys_retention_days)
        # alarm_log_dir 僅保留舊呼叫端相容性；新設定一律直接指定 SQLite 路徑。
        if not alarm_db_path and alarm_log_dir:
            alarm_db_path = os.path.join(alarm_log_dir, "alarm_history.sqlite3")
        self.alarm_log = AlarmLog(alarm_db_path, alarm_retention_days)
        self.devices = {}          # device_id -> DeviceState
        self.viewers = set()       # 每個瀏覽器一個 asyncio.Queue
        self._seq = 0
        # 床號自動帶入掛件（可為 None = 未啟用），見 attach_bed_sync()
        self.bed_sync = None

    def attach_bed_sync(self, bed_sync):
        """掛上床號自動帶入（domain/bed_sync.py）。

        不在建構子注入是因為 BedSync 需要 Hub 的 set_device_meta 與 is_online
        才能建立——先有 Hub 才有 BedSync，順序上只能在事後掛。
        """
        self.bed_sync = bed_sync

    # ── Pi 端事件（由 ingest 呼叫）──────────────────────────────────

    def device_hello(self, msg: dict):
        """裝置上線。回傳 (device_id, conn_seq) 給該連線後續使用；
        新裝置超過 max_devices 上限時回傳 None（既有裝置重連不受影響）。"""
        device = str(msg.get("device") or "unknown")
        st = self.devices.get(device)
        if st is None:
            if len(self.devices) >= self.max_devices:
                self.log.warning(f"裝置數已達上限 {self.max_devices}，拒絕新裝置 {device}")
                return None
            st = self.devices[device] = DeviceState(device, self.sys_history_max)
        self._seq += 1
        st.conn_seq = self._seq
        st.patient = str(msg.get("patient") or "")
        st.proto = msg.get("v")
        st.app_version = str(msg.get("app_version") or "")
        st.online = True
        st.last_seen = st.connected_at = time.time()
        if st.proto != PROTO_VERSION:
            self.log.warning(f"{device} 協議版本 {st.proto} 與伺服器 {PROTO_VERSION} 不同")
        # 資安規則：病人代碼只顯示在儀表板畫面，禁止寫入 log
        self.log.info(f"裝置上線: {device}（版本 {st.app_version or '未提供'}）")
        audit("device_online", device=device, app_version=st.app_version or "-")
        if self.bed_sync is not None:
            self.bed_sync.on_hello(device)   # 新連線 → 下一則呼吸器連線視為新的接機
        self.broadcast(self._link_online(st))
        return device, st.conn_seq

    def _link_online(self, st: DeviceState) -> dict:
        """上線用的 link 訊息（hello 與 watchdog 誤判回復共用同一份組法，
        避免兩處各組一次而漏掉新欄位）"""
        return {"type": "link", "device": st.device, "online": True,
                "patient": st.patient, "v": st.proto,
                "connected_at": st.connected_at, "app_version": st.app_version}

    def device_message(self, device: str, conn_seq: int, msg: dict):
        st = self.devices.get(device)
        if st is None or st.conn_seq != conn_seq:
            return                       # 已被新連線取代的殘留訊息
        st.last_seen = time.time()
        if not st.online:                # watchdog 誤判後資料又進來 → 回復上線
            st.online = True             # connected_at 不動：TCP 連線從未中斷
            self.broadcast(self._link_online(st))
        t = msg.get("type")
        if t == "ping":
            return                       # 心跳只更新 last_seen，不轉發
        if t in STATEFUL_TYPES:
            st.latest[t] = msg
            if t == "sys":
                st.sys_history.append(msg)   # 記憶體歷史（趨勢圖）
                self._append_sys_log(st, msg)  # 七天 SQLite；CSV 僅在下載時產生
            elif t == "alarm":
                self.alarm_log.on_alarm(device, msg.get("alarms", []), msg.get("ts"))
            elif t == "status" and self.bed_sync is not None:
                # 呼吸器接上病人 = 可能換了新病人 → 開始（或停止）查床號
                self.bed_sync.on_status(device, str(msg.get("state") or ""))
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
        self.alarm_log.on_disconnect(device)
        self.log.info(f"裝置離線: {device}")
        audit("device_offline", device=device, reason="disconnected")
        self.broadcast({"type": "link", "device": device, "online": False})

    def sys_history(self, device: str) -> list:
        """該裝置記憶體中的 sys 近段歷史（供 /history 端點；未知裝置回傳空清單）"""
        st = self.devices.get(device)
        return list(st.sys_history) if st else []

    def remove_device(self, device: str) -> str:
        """管理頁移除離線裝置（PROTOCOL.md /api/admin/devices）。
        回傳 "ok"｜"online"（線上不可移除）｜"unknown"。
        只清伺服器記憶體狀態並廣播 device_removed；SQLite 歷史保留到期限。
        裝置之後重新連上會走 device_hello 自動回來。"""
        st = self.devices.get(device)
        if st is None:
            return "unknown"
        if st.online:
            return "online"
        del self.devices[device]
        self.alarm_log.forget(device)
        if self.bed_sync is not None:
            self.bed_sync.forget(device)
        self.log.info(f"管理員移除離線裝置: {device}")
        audit("device_removed", device=device)
        self.broadcast({"type": "device_removed", "device": device})
        return "ok"

    def sys_history_csv(self, device: str):
        """從 SQLite 即時產生該裝置最近七天的系統狀態 CSV。"""
        return self.sys_log.export_csv(device)

    def alarm_history_csv(self, device: str):
        """從 SQLite 即時產生該裝置的七天警報 episode CSV。"""
        return self.alarm_log.export_csv(device)

    def alarm_history(self, device: str, limit: int = 50) -> list:
        """該裝置最新警報 episode（新到舊；詳細監測畫面使用）。"""
        return self.alarm_log.recent(device, limit)

    def close(self):
        """關閉 SQLite，供伺服器正常停止及測試清理。"""
        self.sys_log.close()
        self.alarm_log.close()

    def _append_sys_log(self, st: DeviceState, msg: dict):
        """節流後寫入 SQLite；只含系統指標，絕不寫病人代碼。"""
        if not self.sys_log.enabled:
            return
        now = time.time()
        if now - st.sys_persist_last_ts < self.sys_persist_interval:
            return
        st.sys_persist_last_ts = now
        self.sys_log.record(st.device, msg, received_at=now)

    async def watchdog(self):
        """TCP 沒斷但資料停了（例如 Wi-Fi 半死）→ 逾時判離線"""
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            for st in self.devices.values():
                if st.online and now - st.last_seen > self.offline_timeout:
                    st.online = False
                    self.alarm_log.on_disconnect(st.device)
                    self.log.warning(f"裝置逾時離線: {st.device}")
                    audit("device_offline", device=st.device, reason="timeout")
                    self.broadcast({"type": "link", "device": st.device, "online": False})

    def is_online(self, device: str) -> bool:
        """該裝置目前是否在線上（床號查詢用來判斷要不要暫停該台）。"""
        st = self.devices.get(device)
        return bool(st and st.online)

    def device_count(self) -> int:
        """目前追蹤的裝置數（含離線；供 /healthz 用，不含任何裝置名稱/病人資訊）"""
        return len(self.devices)

    # ── 瀏覽器端 ────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """新瀏覽器連上時的完整狀態（波形不補：幾秒內就會填滿）"""
        # 清冊一次讀完再套到每台，避免每台各開一次檔
        metas = self.directory.all_meta() if self.directory else {}
        devs = []
        for st in self.devices.values():
            meta = metas.get(st.device) or {}
            d = {"device": st.device, "patient": st.patient,
                 "online": st.online, "v": st.proto,
                 "connected_at": st.connected_at, "app_version": st.app_version,
                 "bed": meta.get("bed", ""), "ward": meta.get("ward", ""),
                 "asset": meta.get("asset", "")}
            d.update(st.latest)          # 各 STATEFUL_TYPES 的最新一則
            devs.append(d)
        return {"type": "snapshot", "devices": devs}

    def set_device_meta(self, device: str, bed=None, asset=None):
        """管理頁設定床號／財編（PROTOCOL.md「裝置床號與財編」）。

        回傳 DeviceDirectory.set_meta 的 (result, data)；成功時廣播
        device_meta，讓所有看板即時換標題與重新排序，不需重新整理。
        注意這裡是對「清冊」設定，裝置不必在線上（也不必曾經連過）。
        """
        if self.directory is None:
            return "no_registry", None
        result, data = self.directory.set_meta(device, bed=bed, asset=asset)
        if result == "ok":
            self.log.info(f"裝置清冊更新: {device}")
            self.broadcast({"type": "device_meta", "device": device,
                            "bed": data["bed"], "ward": data["ward"],
                            "asset": data["asset"]})
        return result, data

    def add_viewer(self):
        """新增一個觀看端佇列；超過 max_viewers 上限回傳 None（拒絕新連線，
        沿用 device_hello 的既有慣例：回傳 None 代表拒絕）"""
        if len(self.viewers) >= self.max_viewers:
            self.log.warning(f"觀看端數已達上限 {self.max_viewers}，拒絕新連線")
            return None
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
