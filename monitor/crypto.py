"""
密碼/權杖雜湊工具 — 中性共用模組（不屬於三層任何一層）
========================================================
與 monitor/config.py 同層級：三層架構（transport/domain/web）都可以
import 這裡，但架構規則禁止 transport ↔ web 互相 import；凡是兩層以上
都要用到的工具（而非任一層專屬的業務邏輯），放這裡。

目前用途：
- monitor/web/auth.py 用來雜湊/驗證瀏覽器登入密碼（accounts.json）
- monitor/transport/device_auth.py 用來雜湊/驗證 Pi 裝置 token（devices.json）
"""

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """PBKDF2-SHA256 雜湊，格式: pbkdf2_sha256$迭代數$salt(hex)$hash(hex)"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False
