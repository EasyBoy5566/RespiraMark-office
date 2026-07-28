"""
領域層 — Pi 裝置配對（待核可狀態機 + devices.json 寫入）
==========================================================
取代「在伺服器產 token 再手動貼到 Pi」的佈建方式：Pi 送出配對申請 →
管理員在 /admin 核對 6 位確認碼後核可 → 伺服器產生 token 寫入
devices.json，Pi 一次性領走明文並寫進自己的 telemetry.json。
協議見 PROTOCOL.md「裝置配對」一節。

設計要點：
- **devices.json 讀寫分離**：驗證（讀）在 transport/device_auth.py 的
  DeviceRegistry，本模組只負責寫入。架構規則禁止 domain import transport，
  因此這裡自己讀檔判斷「是否為換發」，路徑由 main.py 注入（同一個檔）。
- 待核可資料**只存記憶體**（10 分鐘短命資料），伺服器重啟即失效，Pi 端
  重新配對的成本近乎零，不值得為此增加持久化與其失效邏輯。
- 過期清理採 **lazy**：每個公開方法入口清一次。待核可筆數本來就有上限，
  記憶體有界，不需要額外的背景 task 與其生命週期管理。
- token 明文**只在核可後、被領取前**存在於記憶體，領取的同時整筆刪除。

⚠️ devices.json 目前有三個獨立寫入者（本模組、tools/make_device.py、
tools/fake_pi.py），各自「整檔讀-改-寫」而無跨程序鎖。本模組已用
tmp + fsync + os.replace 確保單次寫入是原子的（不會讀到半截檔），但若在
伺服器執行配對的**同時**手動跑 make_device.py，仍可能互相覆蓋。院內單一
管理員的使用情境下屬可接受風險。
"""

import json
import logging
import os
import re
import secrets
import threading
import time

from monitor.crypto import hash_password

# 與 tools/make_device.py 的 TOKEN_BYTES 一致（產出約 32 字元）
TOKEN_BYTES = 24
PAIR_ID_BYTES = 16          # 真正的能力憑證：只有送出申請的那台 Pi 知道
POLL_INTERVAL = 3           # 建議 Pi 端的輪詢間隔（秒），寫在 request 回應裡

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
NOTE_MAX = 64


class PairingService:
    """配對狀態機。由 main.py 建立後注入 web 層（不經 Hub：配對狀態與遙測
    狀態無關，不需要廣播、不進 snapshot，掛上去只會汙染 Hub 的契約）。

    執行緒安全：approve() 的 PBKDF2 由 web 層丟到 executor 執行緒跑，
    其餘方法在 event loop 執行緒，因此所有共用狀態一律用 self._lock 保護。
    """

    def __init__(self, devices_path: str, ingest_port: int,
                 ttl: float = 600.0, max_pending: int = 5, now_fn=None):
        self.devices_path = devices_path
        self.ingest_port = int(ingest_port)
        self.ttl = float(ttl)
        self.max_pending = max(1, int(max_pending))
        self._now = now_fn or time.time
        self._lock = threading.Lock()
        self._pending = {}            # pair_id -> 條目（見下方 _new_entry）
        self.log = logging.getLogger("pairing")

    # ── Pi 端 ────────────────────────────────────────────────────────

    def request(self, device_id: str, note: str, ip: str):
        """Pi 送出配對申請。

        回傳 (result, data)：
          ("ok", {pair_id, code, expires_in, poll_interval, device_id, renew, replaced})
          ("bad_id", None)   device_id 不合格式
          ("limit", None)    待核可筆數已達上限
        （result 用字串比照 hub.remove_device 的 "ok"/"online"/"unknown" 慣例）
        """
        device_id = (device_id or "").strip()
        if not DEVICE_ID_RE.match(device_id):
            return "bad_id", None
        note = (note or "").strip()[:NOTE_MAX]

        renew = self._device_exists(device_id)
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            # 同一 IP 只留最新一筆：避免連按累積佔位，把上限吃光
            replaced = [e for e in self._pending.values() if e["ip"] == ip]
            for e in replaced:
                del self._pending[e["pair_id"]]
            if len(self._pending) >= self.max_pending:
                return "limit", None

            entry = self._new_entry(device_id, note, ip, renew, now)
            self._pending[entry["pair_id"]] = entry

        return "ok", {
            "pair_id": entry["pair_id"],
            "code": entry["code"],
            "expires_in": int(self.ttl),
            "poll_interval": POLL_INTERVAL,
            "device_id": device_id,
            "renew": renew,
            # 供 web 層決定是否要記一則 pair_replaced 審計
            "replaced": [e["device_id"] for e in replaced],
        }

    def poll(self, pair_id: str):
        """Pi 查詢配對結果。回傳 (回應 dict, 被領取的 device_id 或 None)。

        approved 的回應**只會產生一次**：組出回應的同時整筆刪除，token 明文
        隨之消失。未知 pair_id / 逾時 / 已領取 一律回 expired（不區分，
        避免洩漏某筆配對是否存在）。
        """
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            entry = self._pending.get(pair_id or "")
            if entry is None:
                return {"status": "expired"}, None
            if entry["state"] == "denied":
                return {"status": "denied"}, None
            if entry["state"] != "approved":
                return {"status": "pending"}, None     # 含 approving（核可處理中）
            del self._pending[pair_id]
            return {
                "status": "approved",
                "device_id": entry["device_id"],
                "token": entry["token"],
                "server_port": self.ingest_port,
            }, entry["device_id"]

    # ── 管理端 ───────────────────────────────────────────────────────

    def list_pending(self) -> list:
        """待核可清單（管理頁輪詢用）：絕不含 token。

        已處理（approving/approved/denied）的條目不列出——管理員按下核可或
        拒絕後該筆就從畫面消失，這本身就是「已處理」的回饋；那些條目仍留在
        記憶體等 Pi 來領結果，直到 TTL 過期。
        """
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            return [{"pair_id": e["pair_id"],
                     "device_id": e["device_id"],
                     "code": e["code"],
                     "ip": e["ip"],
                     "note": e["note"],
                     "renew": e["renew"],
                     "age_s": round(now - e["created"], 1),
                     "expires_in": round(e["expires_at"] - now, 1)}
                    for e in sorted(self._pending.values(),
                                    key=lambda x: x["created"])
                    if e["state"] == "pending"]

    def approve(self, pair_id: str):
        """核可：產生 token → 雜湊 → 寫入 devices.json。

        **同步阻塞**（PBKDF2 600k 迭代約數百毫秒 + 檔案 I/O），呼叫端必須用
        run_in_executor 執行，否則會卡住 event loop 讓所有波形轉發卡頓。

        回傳 (result, data)：
          ("ok", {"device_id":..., "renew":...})
          ("not_found", None) / ("already", None) / ("write_failed", None)
        """
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            entry = self._pending.get(pair_id or "")
            if entry is None:
                return "not_found", None
            if entry["state"] != "pending":
                return "already", None
            entry["state"] = "approving"     # 佔位：擋住第二位管理員同時核可
            device_id, note, renew = entry["device_id"], entry["note"], entry["renew"]

        # 鎖外算雜湊：PBKDF2 很慢，持鎖會擋住其他 Pi 的輪詢
        token = secrets.token_urlsafe(TOKEN_BYTES)
        token_hash = hash_password(token)

        with self._lock:
            entry = self._pending.get(pair_id)
            if entry is None:                # 期間逾時被清掉
                return "not_found", None
            if not self._write_device(device_id, note, token_hash):
                entry["state"] = "pending"   # 保持待核可，管理員可直接重按
                return "write_failed", None
            entry["state"] = "approved"
            entry["token"] = token
        return "ok", {"device_id": device_id, "renew": renew}

    def deny(self, pair_id: str):
        """拒絕：不動 devices.json；該筆保留至 TTL，讓 Pi 輪詢得知結果。"""
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            entry = self._pending.get(pair_id or "")
            if entry is None:
                return "not_found", None
            if entry["state"] != "pending":
                return "already", None
            entry["state"] = "denied"
            return "ok", {"device_id": entry["device_id"]}

    # ── 內部 ────────────────────────────────────────────────────────

    def _new_entry(self, device_id: str, note: str, ip: str,
                   renew: bool, now: float) -> dict:
        return {
            "pair_id": secrets.token_urlsafe(PAIR_ID_BYTES),
            "device_id": device_id,
            "note": note,
            "ip": ip,
            # 6 位碼只供人工核對「網頁上這筆是不是我手上那台」，不是憑證
            "code": f"{secrets.randbelow(1000000):06d}",
            "created": now,
            "expires_at": now + self.ttl,
            "state": "pending",     # pending / approving / approved / denied
            "token": None,          # 僅 approved 且尚未被領取時有值
            "renew": renew,
        }

    def _purge_expired(self, now: float) -> list:
        """lazy 清理（必須在鎖內呼叫）。回傳被清掉的條目供呼叫端記錄。"""
        dead = [e for e in self._pending.values() if e["expires_at"] <= now]
        for e in dead:
            del self._pending[e["pair_id"]]
            reason = "unclaimed" if e["state"] == "approved" else e["state"]
            self.log.info(f"配對逾時失效: {e['device_id']} ({reason})")
        return dead

    def _load_devices(self) -> dict:
        """讀 devices.json；讀不到或格式壞掉回空結構（與 make_device.py 一致）"""
        try:
            with open(self.devices_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("devices"), list):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.log.warning(f"裝置權杖檔讀取失敗（將重建）: {e}")
        return {"devices": []}

    def _device_exists(self, device_id: str) -> bool:
        """已登記過同名裝置 → 這次核可是「換發」，管理頁要顯示警告"""
        return any(d.get("device_id") == device_id
                   for d in self._load_devices().get("devices", []))

    def _write_device(self, device_id: str, note: str, token_hash: str) -> bool:
        """新增或覆寫一台裝置（欄位格式與 tools/make_device.py 相同），
        以 tmp + fsync + os.replace 原子寫入。失敗只記 log 回 False——
        配對失敗遠比伺服器崩潰好。"""
        data = self._load_devices()
        devices = data["devices"]
        entry = {"device_id": device_id, "note": note,
                 "enabled": True, "token_hash": token_hash}
        for i, d in enumerate(devices):
            if d.get("device_id") == device_id:
                # 換發：保留管理員原本填的備註（配對申請的 note 只在新增時採用）
                entry["note"] = d.get("note") or note
                devices[i] = entry
                break
        else:
            devices.append(entry)

        tmp = self.devices_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.devices_path)
            return True
        except OSError as e:
            self.log.error(f"裝置權杖檔寫入失敗: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
