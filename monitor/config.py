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
    "ingest_token": "",       # Pi 連入的存取權杖；空字串 = 不驗證（僅限開發環境）
    "max_devices": 16,        # 裝置數上限，超過即拒絕新裝置（防範記憶體被塞爆）
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
