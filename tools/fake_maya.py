#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
開發工具 — 院方設備系統（maya）假伺服器
==========================================
模擬 `getDeviceList` 的回傳，供床號自動帶入功能在本機做端對端演練。

🚨 **絕不對院方正式系統（maya-ap.csh.org.tw）發送請求**——連唯讀探測都不行。
本專案所有床號查詢的驗證一律打這支假伺服器；真實 API 的連通測試由使用者
自己在院內執行。

回傳的姓名與病歷號都是明顯的假資料，而且伺服器端本來就會在解析當下丟棄
（見 monitor/transport/maya_client.py 檔頭紅線）——放進來只是為了讓字串
形狀跟實際回傳一致，確認解析器面對真實格式時會正確忽略它們。

用法（專案根目錄）：

    # 一開始就是「新資料」：伺服器一啟動，下一輪查詢就會帶入床號
    python tools/fake_maya.py --asset 27943 --bed CCU18

    # 一開始是「舊資料」（記錄時間 = 一天前），60 秒後才更新成「現在」：
    # 可以看到看板先顯示機台編號、輪詢幾次後才換成床號
    python tools/fake_maya.py --asset 27943 --bed CCU18 --stale-for 60

    # 多台
    python tools/fake_maya.py --asset 27943 --bed CCU18 --asset 27944 --bed 3520-1

搭配設定（config.json）：

    "maya_enabled": true,
    "maya_url": "http://127.0.0.1:8899/RCS_CSH/api/System/getDeviceList?pShowDel=false"

再跑一台財編相符的模擬 Pi（財編要先在 /admin 登記到該台）：

    python tools/fake_pi.py --device pi-01 --patient TEST001
"""

import argparse
import datetime
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 一天前，用來模擬「上一位病人留下的舊紀錄」
STALE_AGE_S = 86400.0


def _fmt(ts: float) -> str:
    """院方時間字串格式（無時區，本地時間）"""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def build_payload(devices, started_at: float, stale_for: float) -> list:
    """組出與實際回傳同形狀的清單。

    stale_for 秒內回「舊紀錄」（記錄時間 = 一天前），之後回「現在」——
    用來演練「等 maya 更新」的等待過程。
    """
    fresh = (time.time() - started_at) >= stale_for
    recorded = time.time() if fresh else time.time() - STALE_AGE_S
    stamp = _fmt(recorded)
    payload = []
    for index, (asset, bed) in enumerate(devices, start=1):
        # 姓名與病歷號刻意是假的：伺服器解析時會丟棄，這裡只是形狀要像
        on_position = (f"床號：{bed} 姓名：測試病人{index} "
                       f"病歷號：TEST{index:04d} 記錄時間:{stamp}")
        payload.append({
            "CREATE_NAME": "假 maya 測試伺服器",
            "ON_POSITION": on_position,
            "field_value": f"{asset}|EvitaV500",
            "DEVICE_SEQ": f"20210316083629{index}",
            "DEVICE_NO": asset,
            "ROOM": f"TEST{index:04d}",
            "DEVICE_MODEL": "EvitaV500",
            "USE_STATUS": "Y",
            "PURCHASE_DATE": None,
            "RECORD_DATE": stamp,
            "MODIFY_DATE": "2021-03-16 14:00:00",
            "V_STATUS": None, "V_LOCATION": None, "RETURN_DATE": None,
            "SERIAL_NUMBER": None, "HOSP_DEVICE_NO": None, "COMPANY": None,
            "MACHINE_MODEL": None, "REMARKS": None, "IS_VENTILATOR": None,
            "V_RECORD_DATE": None, "COST_CODE": None, "BED_NO": None,
            "DATATYPE": None, "V_COST": None, "V_COST_OTHER": None,
        })
    # 混一台沒有位置資訊的機器：確認解析器會安靜跳過而不是炸掉
    payload.append({"DEVICE_NO": "00000", "ON_POSITION": None,
                    "DEVICE_MODEL": "EvitaV500", "RECORD_DATE": None})
    return payload


def make_handler(devices, started_at, stale_for):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(build_payload(devices, started_at, stale_for),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            print(f"[fake_maya] 收到查詢 {self.path}")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="maya getDeviceList 假伺服器（本機測試用）")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--asset", action="append", default=[],
                    help="財編（DEVICE_NO），可重複；需與 /admin 登記的財編一致")
    ap.add_argument("--bed", action="append", default=[],
                    help="對應的床號，順序與 --asset 相同")
    ap.add_argument("--stale-for", type=float, default=0.0, metavar="秒",
                    help="啟動後這麼多秒內回「一天前的舊紀錄」，之後才回「現在」")
    args = ap.parse_args()

    assets = args.asset or ["27943"]
    beds = args.bed or ["CCU18"]
    if len(assets) != len(beds):
        ap.error("--asset 與 --bed 的數量必須相同")
    devices = list(zip(assets, beds))

    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(devices, time.time(), args.stale_for))
    print(f"假 maya 伺服器啟動: http://127.0.0.1:{args.port}/"
          f"RCS_CSH/api/System/getDeviceList?pShowDel=false")
    for asset, bed in devices:
        print(f"  財編 {asset} → 床號 {bed}")
    if args.stale_for > 0:
        print(f"  前 {args.stale_for:.0f} 秒回舊紀錄（記錄時間 = 一天前），之後才更新")
    print("  Ctrl+C 結束")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n假 maya 伺服器已停止")


if __name__ == "__main__":
    main()
