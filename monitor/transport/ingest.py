"""
傳輸層 — Pi 遙測資料接收（TCP ingest）
========================================
只負責「資料怎麼進來」：收線、拆行、解析 JSON、轉交 domain 層的 Hub。
不含任何業務邏輯；異常輸入（壞 JSON、超長行、非 hello 開頭）只能
記 log 後忽略或斷該連線，絕不讓伺服器崩潰。

協議格式見 PROTOCOL.md。
"""

import asyncio
import hmac
import json
import logging

from monitor.audit import audit

MAX_LINE = 64 * 1024       # 單行訊息上限（防禦異常資料；正常 wave/params 批次遠小於此）
READ_LIMIT = 1024 * 1024   # asyncio stream 讀取緩衝上限


class _ConnCounter:
    """單執行緒 asyncio 內共用的連線計數器（不需要 lock）"""
    __slots__ = ("count",)

    def __init__(self):
        self.count = 0


async def handle_ingest(reader, writer, hub, token="", max_conns=64,
                         hello_timeout=10.0, idle_timeout=60.0, counter=None,
                         devices=None):
    """單一 Pi 連線的生命週期：hello（含 token 驗證）→ 持續收訊息 → 斷線通知 Hub。

    連線防護（IMPROVEMENT_PLAN.md F-02）：同時連線數超過 max_conns 直接拒絕；
    等 hello 逾時 hello_timeout 秒、hello 通過後閒置逾時 idle_timeout 秒皆斷線
    ——Pi 端本來就每 2 秒送一次 ping，idle_timeout 預設 60 秒留有充裕餘裕。
    """
    log = logging.getLogger("ingest")
    peer = writer.get_extra_info("peername")
    device = None
    conn_seq = None

    if counter is not None:
        if counter.count >= max_conns:
            log.warning(f"{peer} 連線數已達上限 {max_conns}，拒絕連線")
            audit("device_reject", reason="max_conns", peer=str(peer))
            try:
                writer.close()
            except OSError:
                pass
            return
        counter.count += 1

    try:
        while True:
            timeout = hello_timeout if device is None else idle_timeout
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                reason = "hello_timeout" if device is None else "idle_timeout"
                log.warning(f"{peer} {'等待 hello' if device is None else '閒置'}逾時，斷線")
                audit("device_reject", reason=reason, peer=str(peer), device=device)
                break
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
                    audit("device_reject", reason="not_hello", peer=str(peer))
                    break
                attempted_device = str(msg.get("device") or "")
                # 存取驗證（見 PROTOCOL.md）；注意：token 值不得寫入 log。
                # devices.json 存在 → 每台裝置獨立 token（IMPROVEMENT_PLAN.md F-01）；
                # 否則退回舊版單一 ingest_token（向後相容）。兩種模式失敗都用同一句
                # log，不透露是裝置不存在、被停用、還是 token 錯誤。
                dev_token = str(msg.get("token") or "")
                if devices is not None and devices.exists():
                    if not devices.verify(attempted_device, dev_token):
                        log.warning(f"{peer} 裝置權杖驗證失敗，斷線")
                        audit("device_reject", reason="bad_device_token",
                             peer=str(peer), device=attempted_device)
                        break
                # 用常數時間比對，避免用回應時間差猜出正確 token（timing attack）
                elif token and not hmac.compare_digest(dev_token, token):
                    log.warning(f"{peer} token 驗證失敗，斷線")
                    audit("device_reject", reason="bad_shared_token",
                         peer=str(peer), device=attempted_device)
                    break
                accepted = hub.device_hello(msg)
                if accepted is None:
                    log.warning(f"{peer} 裝置數已達上限，拒絕連線")
                    audit("device_reject", reason="max_devices",
                         peer=str(peer), device=attempted_device)
                    break
                device, conn_seq = accepted
            else:
                if not _valid_message(msg):
                    log.warning(f"{peer} {msg.get('type')} 訊息格式異常（忽略該則）")
                    continue
                hub.device_message(device, conn_seq, msg)
    except (ConnectionResetError, OSError):
        pass
    finally:
        if counter is not None:
            counter.count -= 1
        hub.device_disconnected(device, conn_seq)
        try:
            writer.close()
        except OSError:
            pass


MAX_WAVE_SAMPLES = 2000     # 每批次上限（正常批次遠小於此，實測 ~15~100 樣本）
MAX_KV_ITEMS = 200          # params 的 settings/measured 鍵數上限
MAX_KV_STR_LEN = 200        # 上述 dict 內字串值長度上限


def _valid_array(v, max_len) -> bool:
    return isinstance(v, list) and len(v) <= max_len \
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)


def _valid_kv(d, max_items, max_str_len) -> bool:
    if not isinstance(d, dict) or len(d) > max_items:
        return False
    return all(not isinstance(v, str) or len(v) <= max_str_len for v in d.values())


def _valid_message(msg: dict) -> bool:
    """已知類型做基本結構驗證（防禦異常/惡意裝置灌爆觀看端與伺服器記憶體）；
    未知類型交給 hub 層既有的「Tolerant Reader」規則忽略，這裡一律放行。"""
    t = msg.get("type")
    if t == "wave":
        p, f, v = msg.get("p"), msg.get("f"), msg.get("v")
        n = len(p) if isinstance(p, list) else -1
        if not (0 <= n <= MAX_WAVE_SAMPLES):
            return False
        if not (_valid_array(p, MAX_WAVE_SAMPLES) and _valid_array(f, MAX_WAVE_SAMPLES)
                and _valid_array(v, MAX_WAVE_SAMPLES)
                and len(f) == n and len(v) == n):
            return False
        trig = msg.get("trig", [])
        return isinstance(trig, list) and all(isinstance(i, int) for i in trig)
    if t == "params":
        return _valid_kv(msg.get("settings", {}), MAX_KV_ITEMS, MAX_KV_STR_LEN) \
            and _valid_kv(msg.get("measured", {}), MAX_KV_ITEMS, MAX_KV_STR_LEN)
    return True


async def start_ingest(hub, port: int, token: str = "", ssl_ctx=None,
                        max_conns: int = 64, hello_timeout: float = 10.0,
                        idle_timeout: float = 60.0, devices=None):
    """啟動 TCP 接收伺服器；devices（DeviceRegistry）存在時優先驗證每台裝置
    獨立 token，否則退回 token 單一共用比對；ssl_ctx 非 None 時整條連線走
    TLS（Pi 端需以 tls_ca 信任本伺服器的 CA）"""
    counter = _ConnCounter()
    return await asyncio.start_server(
        lambda r, w: handle_ingest(r, w, hub, token, max_conns, hello_timeout,
                                   idle_timeout, counter, devices),
        "0.0.0.0", port, limit=READ_LIMIT, ssl=ssl_ctx,
    )
