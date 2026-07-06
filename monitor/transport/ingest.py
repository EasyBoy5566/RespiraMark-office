"""
傳輸層 — Pi 遙測資料接收（TCP ingest）
========================================
只負責「資料怎麼進來」：收線、拆行、解析 JSON、轉交 domain 層的 Hub。
不含任何業務邏輯；異常輸入（壞 JSON、超長行、非 hello 開頭）只能
記 log 後忽略或斷該連線，絕不讓伺服器崩潰。

協議格式見 PROTOCOL.md。
"""

import asyncio
import json
import logging

MAX_LINE = 512 * 1024      # 單行訊息上限（防禦異常資料）
READ_LIMIT = 1024 * 1024   # asyncio stream 讀取緩衝上限


async def handle_ingest(reader, writer, hub, token=""):
    """單一 Pi 連線的生命週期：hello（含 token 驗證）→ 持續收訊息 → 斷線通知 Hub"""
    log = logging.getLogger("ingest")
    peer = writer.get_extra_info("peername")
    device = None
    conn_seq = None
    try:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.warning(f"{peer} 訊息過長，斷線")
                break
            if not line:
                break                          # 對方關閉連線
            line = line.strip()
            if not line or len(line) > MAX_LINE:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning(f"{peer} JSON 解析失敗（忽略該行）")
                continue
            if device is None:
                if msg.get("type") != "hello":
                    log.warning(f"{peer} 第一則訊息不是 hello，斷線")
                    break
                # token 驗證（見 PROTOCOL.md）；注意：token 值不得寫入 log
                if token and msg.get("token") != token:
                    log.warning(f"{peer} token 驗證失敗，斷線")
                    break
                accepted = hub.device_hello(msg)
                if accepted is None:
                    log.warning(f"{peer} 裝置數已達上限，拒絕連線")
                    break
                device, conn_seq = accepted
            else:
                hub.device_message(device, conn_seq, msg)
    except (ConnectionResetError, OSError):
        pass
    finally:
        hub.device_disconnected(device, conn_seq)
        try:
            writer.close()
        except OSError:
            pass


async def start_ingest(hub, port: int, token: str = "", ssl_ctx=None):
    """啟動 TCP 接收伺服器；token 非空時對所有連線做 hello 驗證；
    ssl_ctx 非 None 時整條連線走 TLS（Pi 端需以 tls_ca 信任本伺服器的 CA）"""
    return await asyncio.start_server(
        lambda r, w: handle_ingest(r, w, hub, token),
        "0.0.0.0", port, limit=READ_LIMIT, ssl=ssl_ctx,
    )
