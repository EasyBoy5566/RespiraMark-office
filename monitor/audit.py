"""
審計日誌 — 中性共用模組（不屬於三層任何一層）
==================================================
與 monitor/config.py 同層級：三層架構都可以 import 這裡記錄「誰在什麼
時候做了什麼」，供醫院事後回溯查核（IMPROVEMENT_PLAN.md F-06）。

寫入 logger 名稱 "audit"；實際的檔案 handler（RotatingFileHandler）由
main.py 啟動時掛上，這裡只負責格式化與呼叫，不碰檔案 I/O。

🚨 紅線：絕不可記錄病人代碼、密碼、token 明碼。call_audit 對常見的誤用
key 名稱做防呆——開發期就會炸掉，而不是悄悄把病人代碼寫進日誌。
"""

import logging

_FORBIDDEN_KEYS = {"patient", "patient_id", "password", "token"}


def audit(event: str, **fields):
    bad = _FORBIDDEN_KEYS & fields.keys()
    if bad:
        raise AssertionError(
            f"audit() 呼叫誤用：欄位 {sorted(bad)} 疑似含病人代碼或密碼/token 明碼，"
            f"禁止寫入審計日誌（event={event}）")
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    logging.getLogger("audit").info(f"{event} {detail}".rstrip())
