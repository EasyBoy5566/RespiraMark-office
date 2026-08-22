"""
領域層 — 床號自動帶入（maya 位置查詢的排程與規則）
=====================================================
規則的完整敘述見 PROTOCOL.md「床號自動帶入」；本模組是那份規則的實作。
HTTP 與字串解析在 transport/maya_client.py，這裡只決定**誰要查、何時停、
查到要不要採用**。

每台裝置的狀態機：

    呼吸器連上（status=connected）─▶ 記下時刻 A，進入待查
        ├─ 記錄時間 ≥ A − 容差 ──▶ 寫入床號，結束
        ├─ 呼吸器斷線 ───────────▶ 取消（沒有新病人要記錄）
        ├─ Pi 離線 ──────────────▶ 暫停（回來後接續；總時限仍從 A 起算）
        └─ 超過 max_duration ────▶ 放棄並記 log（不再打擾院方正式系統）

**查詢函式與所有寫回動作都是注入的 callback**，本模組因此不 import 任何東西：

- `fetch` ← `transport/maya_client.fetch_positions`。領域層**不准 import 傳輸層**
  （CLAUDE.md §3.1 依賴方向只准 web/transport → domain），由 main.py 這個
  composition root 把兩者接起來。
- `apply_bed` / `is_online` ← Hub 的 `set_device_meta` / `is_online`。Hub 要先
  存在才能建 BedSync，反向 import 會是循環相依。

代價全是好處：單元測試用假的 fetch、假的 callback 與假時鐘就能完整驗證這個
狀態機，不必起伺服器、不必碰網路。

🚨 本模組完全不碰姓名與病歷號：maya_client 的回傳值裡就沒有那兩個欄位。
"""

import asyncio
import logging
import time

# 呼吸器連線狀態（PROTOCOL.md 的 `status.state`）：只有這個值代表「機器接著病人用」
VENT_CONNECTED = "connected"


class BedSync:
    def __init__(self, directory, apply_bed, is_online, fetch, url,
                 poll_interval=30.0, timeout=10.0, max_duration=3600.0,
                 tolerance=120.0, clock=None):
        """
        directory     DeviceDirectory，用來查每台的財編（清冊唯一真相）
        apply_bed     callable(device, bed) -> (result, data)，同 Hub.set_device_meta
        is_online     callable(device) -> bool，Pi 是否在線上
        fetch         async callable(url, timeout) -> {財編: Position} 或 None
                      （正式接 transport/maya_client.fetch_positions，見檔頭）
        clock         測試用假時鐘注入點；預設真實時間
        """
        self.log = logging.getLogger("bed_sync")
        self.directory = directory
        self.apply_bed = apply_bed
        self.is_online = is_online
        self.url = url
        self.poll_interval = max(5.0, float(poll_interval))
        self.timeout = float(timeout)
        self.max_duration = float(max_duration)
        self.tolerance = float(tolerance)
        self._fetch = fetch
        self._clock = clock or time.time
        self._pending = {}      # device -> A（該台呼吸器連線建立的時刻）
        self._vent_state = {}   # device -> 最近一次收到的呼吸器連線狀態
        self._warned = set()    # 已經抱怨過「沒登記財編」的裝置（避免每 30 秒洗版）

    # ── Hub 事件（由 hub.py 呼叫）──────────────────────────────────

    def on_hello(self, device: str):
        """裝置新連線：忘掉舊的呼吸器狀態。

        Pi 重開機或換病人後重連時，下一則 `status=connected` 才會被當成
        「新的一次接機」而重新起算 A；否則會誤以為狀態沒變而不去查。
        """
        self._vent_state.pop(device, None)

    def on_status(self, device: str, state: str):
        """呼吸器連線狀態變化（PROTOCOL.md `status`）。"""
        previous = self._vent_state.get(device)
        self._vent_state[device] = state
        if state != VENT_CONNECTED:
            self._cancel(device)                 # 沒有新病人要記錄
            return
        if previous == VENT_CONNECTED:
            return                               # 狀態沒真的變，不重新起算
        self._pending[device] = self._clock()    # A = 此刻
        self.log.info(f"{device} 呼吸器已連線，開始查詢床號")

    def forget(self, device: str):
        """管理員移除裝置（Hub.remove_device）。"""
        self._cancel(device)
        self._vent_state.pop(device, None)

    def _cancel(self, device: str):
        if self._pending.pop(device, None) is not None:
            self.log.info(f"{device} 停止查詢床號")
        self._warned.discard(device)

    # ── 輪詢 ────────────────────────────────────────────────────────

    async def run(self):
        """背景輪詢工作（main.py 啟動）。沒有待查裝置時完全不發請求。"""
        self.log.info(f"床號自動帶入已啟用：每 {self.poll_interval:.0f} 秒查詢一次，"
                      f"單台最長 {self.max_duration / 60:.0f} 分鐘")
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 任何未預期例外都不能讓這個背景工作死掉（CLAUDE.md §2.1）：
                # 床號查不到只是少一個便利功能，儀表板必須照常運作。
                self.log.exception("床號查詢發生未預期錯誤（本輪略過）")

    async def poll_once(self):
        """一輪查詢：全部待查裝置共用一個請求。"""
        self._expire()
        targets = self._targets()
        if not targets:
            return                              # 沒有查得動的裝置 → 不發請求
        positions = await self._fetch(self.url, timeout=self.timeout)
        if positions is None:
            return                              # 這輪沒問到，下輪再試
        # 財編比對去空白、不分大小寫：清冊是人工登記的，大小寫與空白不該影響比對
        by_asset = {str(k).strip().casefold(): v for k, v in positions.items()}
        for device, (asset, meta) in targets.items():
            started_at = self._pending.get(device)
            if started_at is None:
                continue                        # 查詢期間該台已斷線
            position = by_asset.get(asset.casefold())
            if position is None:
                continue                        # maya 上查無此財編 → 下輪再試
            if position.recorded_at < started_at - self.tolerance:
                continue                        # 還是上一位病人的紀錄
            self._adopt(device, position.bed, meta)

    def _targets(self) -> dict:
        """本輪查得動的裝置 → `{device: (財編, meta)}`。

        排除：Pi 離線（暫停）、清冊查無此台、沒登記財編（查詢的鍵就是財編）。
        清冊一次讀完再套到每台，避免每台各開一次檔。
        """
        if not self._pending:
            return {}
        metas = self.directory.all_meta() if self.directory else {}
        targets = {}
        for device in self._pending:
            if not self.is_online(device):
                continue                        # 暫停，不取消
            meta = metas.get(device)
            if meta is None:
                self._warn_once(device, "尚未登記在裝置清冊，床號無處可寫")
                continue
            asset = str(meta.get("asset") or "").strip()
            if not asset:
                self._warn_once(device, "尚未登記財編，無法向 maya 查詢床號")
                continue
            targets[device] = (asset, meta)
        return targets

    def _adopt(self, device: str, bed: str, meta: dict):
        """採用查到的床號並結束該台的查詢。"""
        self._cancel(device)
        if str(meta.get("bed") or "") == bed:
            self.log.info(f"{device} 床號與清冊相同（{bed}），不重複寫入")
            return
        result, _ = self.apply_bed(device, bed)
        if result == "ok":
            self.log.info(f"{device} 床號已自 maya 帶入: {bed}")
        else:
            self.log.warning(f"{device} 床號寫入失敗（{result}），本次不重試")

    def _expire(self):
        """超過時限仍等不到新紀錄 → 放棄，不對院方系統無止境發請求。"""
        now = self._clock()
        for device, started_at in list(self._pending.items()):
            if now - started_at > self.max_duration:
                del self._pending[device]
                self._warned.discard(device)
                self.log.warning(
                    f"{device} 查詢床號逾 {self.max_duration / 60:.0f} 分鐘仍無較新的紀錄，"
                    f"放棄查詢（請確認該台在 maya 上的使用位置是否已更新）")

    def _warn_once(self, device: str, reason: str):
        if device not in self._warned:
            self._warned.add(device)
            self.log.warning(f"{device} {reason}")
