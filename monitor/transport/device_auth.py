"""
傳輸層 — Pi 裝置身分驗證（devices.json，每台獨立 token）
==========================================================
比照 monitor/web/auth.py 的 LocalAuthenticator 模式：帳號檔每次驗證時
重讀（tools/make_device.py 改動後不需重啟伺服器），token 只存 PBKDF2
雜湊（monitor/crypto.py，與瀏覽器密碼共用同一套函式）。

沒有 devices.json 時（exists() 回 False），ingest.py 退回舊版單一
`ingest_token` 比對，向後相容單機/開發環境。
"""

import json
import logging
import os

from monitor.crypto import verify_password

DEFAULT_PATH = "devices.json"


class DeviceRegistry:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self.log = logging.getLogger("device_auth")

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def _load_devices(self) -> list:
        """每次重讀：make_device.py 改動後不需重啟伺服器（同 LocalAuthenticator 模式）"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            devices = data.get("devices")
            return devices if isinstance(devices, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            self.log.warning(f"裝置權杖檔讀取失敗: {e}")
            return []

    def verify(self, device_id: str, token: str) -> bool:
        """驗證成功回傳 True；裝置不存在、被停用、token 錯誤一律回傳 False
        （不透露是哪一種原因，避免洩漏裝置是否存在）"""
        for d in self._load_devices():
            if d.get("device_id") == device_id:
                if not d.get("enabled", True):
                    return False
                return verify_password(token, d.get("token_hash", ""))
        return False

    def list_devices(self) -> list:
        """裝置唯讀清單（管理用途）：只回 device_id/note/enabled，絕不回 token"""
        return [{"device_id": d.get("device_id") or "?",
                 "note": d.get("note") or "",
                 "enabled": bool(d.get("enabled", True))}
                for d in self._load_devices()]
