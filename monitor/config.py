"""
設定載入 — 唯一讀取 config.json 的地方
========================================
其他模組一律接收已載入的 cfg dict，禁止自行讀檔或寫死 IP/port。
"""

import json
import logging
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

DEFAULTS = {
    "ingest_port": 8765,      # Pi 連入的 TCP port
    "web_port": 8080,         # 瀏覽器網頁 port
    "offline_timeout": 5.0,   # 秒，無資料判定裝置離線
    "ingest_token": "",       # Pi 連入的存取權杖（單一共用，退回模式）；空字串 = 不驗證（僅限開發環境）
    "devices_file": "devices.json",  # 每台裝置獨立權杖檔（tools/make_device.py 建立）；不存在則退回 ingest_token
    "max_devices": 16,        # 裝置數上限，超過即拒絕新裝置（防範記憶體被塞爆）
    # ── ingest 連線防護（防範區網內異常/惡意連線耗盡資源）──
    "ingest_max_conns": 64,       # 同時 TCP 連線數上限，超過直接拒絕新連線
    "ingest_hello_timeout": 10.0,  # 秒，連線後這麼久沒收到合法 hello 就斷線
    "ingest_idle_timeout": 60.0,   # 秒，hello 通過後這麼久沒收到任何訊息就斷線（Pi 每 2 秒 ping，此值留寬裕）
    "max_viewers": 50,         # 同時瀏覽器觀看端（WebSocket）數上限，超過拒絕新連線
    # Pi 系統狀態（sys）：記憶體保留的樣本數（趨勢圖用；720 ≈ 1 小時 @ 5s）
    "sys_history_max": 720,
    # 系統狀態長期落地目錄（相對專案根目錄；空字串 = 不落地，只留記憶體歷史）
    "sys_log_dir": "sys_logs",
    # 秒，CSV 寫入節流間隔：只影響長期落地檔，不影響即時畫面/記憶體歷史（仍隨 Pi 送出頻率更新）
    "sys_csv_interval": 60.0,
    # ── TLS 加密（tools/make_certs.py 產生憑證；兩者皆填才啟用，網頁與 ingest 同時加密）──
    "tls_cert": "",           # 伺服器憑證，例如 "certs/server.pem"（相對專案根目錄）
    "tls_key": "",            # 伺服器私鑰，例如 "certs/server.key"
    # ── 瀏覽器登入（tools/make_user.py 建帳號；停用僅限開發環境）──
    "auth_enabled": True,
    "accounts_file": "accounts.json",
    "session_idle_minutes": 30.0,   # 閒置逾時（sliding）自動登出；0 = 不逾時（護理站看板用）
    "session_absolute_hours": 12.0,  # 登入後最長可用時數，即使一直有操作也會過期；0 = 不限（看板帳號用）
    "session_max": 200,        # session 總量上限，超過淘汰最久沒動作的一筆
}


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """讀取設定檔；不存在或格式錯誤 → 用預設值（記 log，不崩潰）"""
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logging.getLogger(__name__).warning(f"config.json 讀取失敗，使用預設值: {e}")
    return cfg
