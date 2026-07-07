"""
領域層 — 警報事件歷史落地（Hub 的訂閱者）
============================================
`alarm` 訊息是全量快照（見 PROTOCOL.md：目前所有警報，空陣列 = 全部
解除）；這裡跟上一次已知集合比對，把「出現」「解除」兩種事件寫進
CSV，供事後回溯查核（IMPROVEMENT_PLAN.md W-302，對應 G-02）。

**只記機台編號與警報內容，絕不記病人代碼**——警報與病人的對應由院方
以機台編號+時間，自行查對照紙本/HIS 病歷系統。
"""

import csv
import logging
import os
import time

CSV_FIELDS = ("time", "event", "cp", "code", "prio", "text")


def _safe_name(device: str) -> str:
    """機台編號 → 可安全放進檔名的字串（比照 hub.py 既有的 _safe_name）"""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in device) or "device"


class AlarmLog:
    """單一實例由 TelemetryHub 持有；on_alarm() 在每則 alarm 訊息時呼叫。"""

    def __init__(self, log_dir: str = ""):
        self.log_dir = log_dir
        self.log = logging.getLogger("alarm_log")
        self._active = {}   # device -> {(cp, code): alarm_dict}——目前已知作用中的警報
        if log_dir:
            try:
                os.makedirs(log_dir, exist_ok=True)
            except OSError as e:
                self.log.warning(f"警報歷史目錄無法建立，停用落地: {e}")
                self.log_dir = ""

    def forget(self, device: str):
        """裝置被管理員移除時呼叫：清掉記憶體中的作用中警報集合（CSV 檔案保留，
        歷史紀錄不受影響；裝置重新連上會從 hello 之後的第一則 alarm 重新建立）"""
        self._active.pop(device, None)

    def csv_path(self, device: str):
        """該裝置警報歷史 CSV 的檔案路徑（供下載端點）；未啟用落地回傳 None"""
        if not self.log_dir:
            return None
        return os.path.join(self.log_dir, f"alarm_{_safe_name(device)}.csv")

    def on_alarm(self, device: str, alarms: list):
        """收到一則 alarm 全量快照：跟上次已知集合比對，記錄新增/解除的部分。
        用 (cp, code) 當識別鍵——同一 (cp, code) 同時間只會有一個作用中實例。"""
        prev = self._active.get(device, {})
        cur = {}
        for a in alarms:
            key = (str(a.get("cp", "")), str(a.get("code", "")))
            cur[key] = a
        self._active[device] = cur
        for key, a in cur.items():
            if key not in prev:
                self._write(device, "appeared", a)
        for key, a in prev.items():
            if key not in cur:
                self._write(device, "cleared", a)

    def _write(self, device: str, event: str, a: dict):
        path = self.csv_path(device)
        if not path:
            return
        try:
            is_new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(CSV_FIELDS)
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), event,
                           a.get("cp", ""), a.get("code", ""),
                           a.get("prio", ""), a.get("text", "")])
        except OSError as e:
            self.log.warning(f"{device} 警報歷史寫入失敗: {e}")
