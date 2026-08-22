#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
開發/維運工具 — SQLite 安全備份（IMPROVEMENT_PLAN.md W-205）
================================================================
🚨 **SQLite 資料庫絕不能用複製檔案的方式備份。** 本專案的 DB 都跑 WAL 模式，
最近的異動還在 `-wal` 檔裡沒併回主檔；伺服器運行中直接 `Copy-Item` 會拿到
一份「撕裂」的資料——平常看起來好好的，真的要還原時才發現打不開或少資料。

本工具走 SQLite 官方的 backup API：由 SQLite 自己在來源資料庫上取一致快照，
**來源可以正在被伺服器寫入**，不需要停機，輸出的單一檔案已含 WAL 內容。

用法：
    python tools/backup_db.py 來源.sqlite3 輸出.sqlite3
    python tools/backup_db.py 來源.sqlite3 輸出.sqlite3 --quiet   # 只在失敗時輸出

備份完會自動做一次完整性檢查（`PRAGMA quick_check`）——備份最怕的是「以為有
備份，還原時才發現是壞的」，檢查成本只有幾毫秒，一律做。

離開碼：0 成功；1 失敗（來源不存在也算失敗，呼叫端才不會靜默少備一個檔）。
`tools/backup.ps1` 會呼叫本工具處理每一個 SQLite；平常不需要手動執行。
"""

import argparse
import os
import sqlite3
import sys


def backup_db(src: str, dst: str) -> bool:
    """把 src 安全備份到 dst（覆蓋）。成功回 True。"""
    if not os.path.exists(src):
        print(f"[錯誤] 來源不存在: {src}", file=sys.stderr)
        return False

    parent = os.path.dirname(os.path.abspath(dst))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 先清掉舊的輸出：backup API 是寫進「既有」資料庫，殘留的舊檔會被覆寫成
    # 新內容沒錯，但殘留的 -wal/-shm 會讓結果難以判讀，不如整組砍乾淨。
    for suffix in ("", "-wal", "-shm"):
        stale = dst + suffix
        if os.path.exists(stale):
            os.remove(stale)

    source = target = None
    try:
        # timeout：伺服器正在寫入時稍等一下，不要一碰到鎖就放棄
        source = sqlite3.connect(src, timeout=30.0)
        target = sqlite3.connect(dst)
        source.backup(target)
        # 快照本身就是一致的，這裡檢查的是「寫出去的檔案有沒有問題」
        result = target.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            print(f"[錯誤] 備份完整性檢查未通過: {src} -> {dst}（{result}）",
                  file=sys.stderr)
            return False
    except sqlite3.Error as e:
        print(f"[錯誤] 備份失敗 {src} -> {dst}: {e}", file=sys.stderr)
        return False
    finally:
        for conn in (target, source):
            if conn is not None:
                conn.close()
    return True


def main():
    ap = argparse.ArgumentParser(description="SQLite 安全備份（backup API，免停機）")
    ap.add_argument("src", help="來源 .sqlite3")
    ap.add_argument("dst", help="輸出 .sqlite3")
    ap.add_argument("--quiet", action="store_true", help="成功時不輸出訊息")
    args = ap.parse_args()

    if not backup_db(args.src, args.dst):
        sys.exit(1)
    if not args.quiet:
        size_mb = os.path.getsize(args.dst) / 1024 / 1024
        print(f"已備份 {args.src} -> {args.dst}（{size_mb:.1f} MB，完整性檢查通過）")


if __name__ == "__main__":
    main()
