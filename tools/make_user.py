"""
make_user — 建立/管理儀表板登入帳號（寫入 accounts.json）
==========================================================
用法（在專案根目錄執行）：
    python tools/make_user.py --user admin --role admin        # 互動式輸入密碼
    python tools/make_user.py --user nurse01                   # 預設角色 viewer
    python tools/make_user.py --list                           # 列出帳號
    python tools/make_user.py --delete --user nurse01          # 刪除帳號

- 密碼只存 PBKDF2 雜湊（見 monitor/crypto.py），檔案裡沒有明碼。
- accounts.json 不進 git（每台伺服器各自維護）；改動後不用重啟伺服器，
  下一次登入就會讀到新內容。
- 角色：viewer = 只看儀表板；admin = 之後的管理頁也能進。
"""

import argparse
import getpass
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from monitor.crypto import hash_password  # noqa: E402


def load_accounts(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("users"), list):
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"警告：{path} 讀取失敗（將重建）: {e}")
    return {"users": []}


def save_accounts(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="建立/管理儀表板登入帳號")
    ap.add_argument("--file", default=os.path.join(PROJECT_ROOT, "accounts.json"),
                    help="帳號檔路徑（預設: 專案根目錄 accounts.json）")
    ap.add_argument("--user", help="帳號名稱")
    ap.add_argument("--role", choices=("viewer", "admin"), default="viewer",
                    help="角色（預設 viewer）")
    ap.add_argument("--password",
                    help="密碼（不建議；會留在指令歷史。省略則安全地互動輸入）")
    ap.add_argument("--list", action="store_true", help="列出所有帳號")
    ap.add_argument("--delete", action="store_true", help="刪除 --user 指定的帳號")
    args = ap.parse_args()

    data = load_accounts(args.file)
    users = data["users"]

    if args.list:
        if not users:
            print("（尚無帳號）")
        for u in users:
            print(f"  {u.get('username')}  ({u.get('role', 'viewer')})")
        return

    if not args.user:
        ap.error("請指定 --user（或用 --list 列出帳號）")

    if args.delete:
        before = len(users)
        data["users"] = [u for u in users if u.get("username") != args.user]
        if len(data["users"]) == before:
            print(f"找不到帳號 {args.user}")
            sys.exit(1)
        save_accounts(args.file, data)
        print(f"已刪除帳號 {args.user}")
        return

    password = args.password
    if not password:
        password = getpass.getpass(f"設定 {args.user} 的密碼: ")
        again = getpass.getpass("再輸入一次確認: ")
        if password != again:
            print("兩次輸入不一致，未變更")
            sys.exit(1)
    if len(password) < 8:
        print("密碼至少 8 個字元")
        sys.exit(1)

    entry = {"username": args.user, "role": args.role,
             "password": hash_password(password)}
    for i, u in enumerate(users):
        if u.get("username") == args.user:
            users[i] = entry
            save_accounts(args.file, data)
            print(f"已更新帳號 {args.user}（{args.role}）")
            return
    users.append(entry)
    save_accounts(args.file, data)
    print(f"已建立帳號 {args.user}（{args.role}）→ {args.file}")


if __name__ == "__main__":
    main()
