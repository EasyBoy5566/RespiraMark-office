"""
RespiraMark Office — 中央監視儀表板 進入點
============================================
- TCP :8765  接收各 Pi 的遙測資料（JSON Lines，見 PROTOCOL.md）
- HTTP :8080 提供儀表板網頁 + WebSocket /ws 即時廣播

啟動：python main.py（或雙擊 start_server.bat）
設定：config.json（不存在則用預設值，範本見 config.json.example）

本檔是 composition root——唯一同時 import 三層並組裝的地方：
    transport(ingest) ──▶ domain(hub) ◀── web(routes)
"""

import argparse
import asyncio
import logging
import socket

from monitor.config import DEFAULT_CONFIG_PATH, load_config
from monitor.domain.hub import TelemetryHub
from monitor.transport.ingest import start_ingest
from monitor.web.routes import start_web


def lan_ip() -> str:
    """找出本機對外的區網 IP（不會真的發封包）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def print_banner(cfg: dict):
    ip = lan_ip()
    print()
    print("=" * 62)
    print("  RespiraMark Office 彙整伺服器已啟動")
    print(f"  儀表板（本機）:   http://localhost:{cfg['web_port']}")
    print(f"  儀表板（區網）:   http://{ip}:{cfg['web_port']}")
    print(f"  Pi 端 telemetry.json 的 server_host 請填: {ip}")
    print(f"  Pi 資料接收 port: {cfg['ingest_port']}")
    print("  （若其他裝置連不上，請確認 Windows 防火牆已放行以上兩個 port）")
    print("=" * 62)
    print()


async def main():
    ap = argparse.ArgumentParser(description="RespiraMark Office 彙整伺服器")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help="設定檔路徑（預設: 專案目錄的 config.json）")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)

    # 組裝三層
    hub = TelemetryHub(offline_timeout=float(cfg["offline_timeout"]),
                       max_devices=int(cfg["max_devices"]))
    ingest_server = await start_ingest(hub, int(cfg["ingest_port"]),
                                       token=str(cfg["ingest_token"] or ""))
    web_runner = await start_web(hub, int(cfg["web_port"]))
    watchdog = asyncio.ensure_future(hub.watchdog())

    print_banner(cfg)
    try:
        await asyncio.Event().wait()           # 永久運行，Ctrl+C 結束
    finally:
        watchdog.cancel()
        ingest_server.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n伺服器已停止")
