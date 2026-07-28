"""
領域層 — 裝置清冊（devices.json 的唯一寫入者）
================================================
`devices.json` 有兩種用途：**驗證**（token_hash，由 transport/device_auth.py
唯讀比對）與**清冊**（床號、呼吸器財編、備註，管理頁維護）。本模組負責整份
檔案的讀取與寫入，配對核發與床號編輯都走這裡，避免多處各寫一份讀-改-寫。

架構規則禁止 domain import transport，所以這裡不重用 DeviceRegistry；
兩邊各自讀檔（DeviceRegistry 每次驗證都重讀，改動立刻生效，不需通知）。

⚠️ **只對已存在的檔案做寫入**（`exists()` 為 False 時 set_meta 直接拒絕）：
`devices.json` 一旦被建立，伺服器就從「單一共用 ingest_token」切換成「每台
獨立驗證」，原本用共用權杖的裝置會在下次重連時被拒。建檔這件事只能由
配對核可（明確的佈建動作）或 tools/make_device.py 觸發，不能被「順手改個
床號」意外引發。

⚠️ 與 tools/make_device.py、tools/fake_pi.py 併發寫入的風險見 pairing.py 檔頭。
"""

import json
import logging
import os

BED_MAX = 32
ASSET_MAX = 32
NOTE_MAX = 64


class DeviceDirectory:
    def __init__(self, path: str):
        self.path = path
        self.log = logging.getLogger("device_directory")

    def exists(self) -> bool:
        return os.path.exists(self.path)

    # ── 讀取 ────────────────────────────────────────────────────────

    def load(self) -> dict:
        """整份讀出；讀不到或格式壞掉回空結構（與 make_device.py 一致）"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("devices"), list):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.log.warning(f"裝置清冊讀取失敗（視為空清冊）: {e}")
        return {"devices": []}

    def has_device(self, device_id: str) -> bool:
        return self.find(device_id) is not None

    def find(self, device_id: str):
        for d in self.load().get("devices", []):
            if d.get("device_id") == device_id:
                return d
        return None

    def meta(self, device_id: str) -> dict:
        """單台的顯示用資料（絕不含 token_hash）；未登記回空字串欄位"""
        d = self.find(device_id) or {}
        return {"bed": str(d.get("bed") or ""),
                "asset": str(d.get("asset") or ""),
                "note": str(d.get("note") or "")}

    def all_meta(self) -> dict:
        """device_id -> {bed, asset, note}；Hub 組 snapshot 時一次取用，
        避免每台各讀一次檔"""
        return {d.get("device_id"): {"bed": str(d.get("bed") or ""),
                                     "asset": str(d.get("asset") or ""),
                                     "note": str(d.get("note") or "")}
                for d in self.load().get("devices", []) if d.get("device_id")}

    # ── 寫入 ────────────────────────────────────────────────────────

    def set_meta(self, device_id: str, bed=None, asset=None):
        """設定床號／財編（None = 保留原值）。

        回傳 (result, data)：
          ("ok", {"device":..., "bed":..., "asset":...})
          ("no_registry", None)  devices.json 不存在（見檔頭警告）
          ("not_found", None)    該裝置尚未登記
          ("write_failed", None)
        """
        if not self.exists():
            return "no_registry", None
        data = self.load()
        for d in data["devices"]:
            if d.get("device_id") == device_id:
                if bed is not None:
                    d["bed"] = str(bed).strip()[:BED_MAX]
                if asset is not None:
                    d["asset"] = str(asset).strip()[:ASSET_MAX]
                if not self.save(data):
                    return "write_failed", None
                return "ok", {"device": device_id,
                              "bed": d.get("bed", ""), "asset": d.get("asset", "")}
        return "not_found", None

    def upsert_device(self, device_id: str, note: str, token_hash: str) -> bool:
        """新增或換發一台裝置（配對核可用）。換發時保留管理員已填的
        備註、床號與財編——只有權杖是新的，實體機器沒有換。"""
        data = self.load()
        devices = data["devices"]
        for d in devices:
            if d.get("device_id") == device_id:
                d["token_hash"] = token_hash
                d["enabled"] = True
                break
        else:
            devices.append({"device_id": device_id,
                            "note": str(note or "")[:NOTE_MAX],
                            "enabled": True, "token_hash": token_hash,
                            "bed": "", "asset": ""})
        return self.save(data)

    def save(self, data: dict) -> bool:
        """tmp + fsync + os.replace 原子寫入：斷電只會留下完整的新檔或完整的
        舊檔，不會出現半截 JSON 讓所有裝置在下次啟動時被拒。
        失敗只記 log 回 False——寫不進去遠比伺服器崩潰好。"""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            self.log.error(f"裝置清冊寫入失敗: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
