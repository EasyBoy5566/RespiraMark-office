"""
make_device — 建立/管理 Pi 裝置存取權杖（寫入 devices.json）
==============================================================
用法（在專案根目錄執行）：
    python tools/make_device.py --device pi-icu-01 --note "ICU 3床"   # 產生新 token
    python tools/make_device.py --list                                # 列出裝置（不顯示 token）
    python tools/make_device.py --disable --device pi-icu-01          # 暫時停用
    python tools/make_device.py --enable --device pi-icu-01           # 重新啟用
    python tools/make_device.py --delete --device pi-icu-01           # 刪除

- token 只存 PBKDF2 雜湊（見 monitor/crypto.py，與瀏覽器密碼共用同一套函式），
  產生時**只顯示這一次**，請立刻複製到該台 Pi 的 telemetry.json。
- devices.json 不進 git（每台伺服器各自維護）；改動後不用重啟伺服器，
  下一次該裝置連線就會讀到新內容。
- 每台 Pi 各自獨立的 token：外洩或懷疑外洩時只需停用/換發該台，不影響其他裝置
  （對應 IMPROVEMENT_PLAN.md F-01）。此檔不存在時，伺服器退回舊版單一
  ingest_token 模式。
"""

import argparse
import json
import os
import secrets
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from monitor.crypto import hash_password  # noqa: E402

TOKEN_BYTES = 24     # secrets.token_urlsafe 的輸入位元組數（產出約 32 字元）


def load_devices(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("devices"), list):
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"警告：{path} 讀取失敗（將重建）: {e}")
    return {"devices": []}


def save_devices(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="建立/管理 Pi 裝置存取權杖")
    ap.add_argument("--file", default=os.path.join(PROJECT_ROOT, "devices.json"),
                    help="裝置檔路徑（預設: 專案根目錄 devices.json）")
    ap.add_argument("--device", help="裝置編號（需與 Pi 端 telemetry.json 的 device_id 一致）")
    ap.add_argument("--note", default="", help="備註（例如床號），僅供辨識用")
    ap.add_argument("--list", action="store_true", help="列出所有裝置（不顯示 token）")
    ap.add_argument("--enable", action="store_true", help="啟用 --device 指定的裝置")
    ap.add_argument("--disable", action="store_true", help="停用 --device 指定的裝置")
    ap.add_argument("--delete", action="store_true", help="刪除 --device 指定的裝置")
    args = ap.parse_args()

    data = load_devices(args.file)
    devices = data["devices"]

    if args.list:
        if not devices:
            print("（尚無裝置）")
        for d in devices:
            state = "啟用" if d.get("enabled", True) else "停用"
            note = f"  {d['note']}" if d.get("note") else ""
            print(f"  {d.get('device_id')}  ({state}){note}")
        return

    if not args.device:
        ap.error("請指定 --device（或用 --list 列出裝置）")

    if args.delete:
        before = len(devices)
        data["devices"] = [d for d in devices if d.get("device_id") != args.device]
        if len(data["devices"]) == before:
            print(f"找不到裝置 {args.device}")
            sys.exit(1)
        save_devices(args.file, data)
        print(f"已刪除裝置 {args.device}")
        return

    if args.enable or args.disable:
        for d in devices:
            if d.get("device_id") == args.device:
                d["enabled"] = args.enable
                save_devices(args.file, data)
                print(f"已{'啟用' if args.enable else '停用'}裝置 {args.device}")
                return
        print(f"找不到裝置 {args.device}")
        sys.exit(1)

    # 產生新 token（或幫既有裝置換發）
    token = secrets.token_urlsafe(TOKEN_BYTES)
    entry = {"device_id": args.device, "note": args.note,
             "enabled": True, "token_hash": hash_password(token)}
    for i, d in enumerate(devices):
        if d.get("device_id") == args.device:
            devices[i] = entry
            break
    else:
        devices.append(entry)
    save_devices(args.file, data)

    print(f"已建立/換發裝置 {args.device} → {args.file}")
    print()
    print(f"  token（只顯示這一次，請立刻複製到該台 Pi 的 telemetry.json）：")
    print(f"    {token}")


if __name__ == "__main__":
    main()
