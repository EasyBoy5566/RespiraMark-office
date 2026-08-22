"""
傳輸層 — 院方設備系統（maya）位置查詢客戶端
================================================
向 maya 的 `getDeviceList` 取回全院設備清單，解析成「財編 → 床號、記錄時間」。
規格見 PROTOCOL.md「床號自動帶入」。

本模組只回答「maya 上現在這些機器在哪一床」，**不含任何業務規則**——誰要查、
何時該停、查到要寫去哪，全在 domain/bed_sync.py（CLAUDE.md §3.1：傳輸層只做
收線、解析、轉交）。

🚨 **資安紅線（CLAUDE.md §2.2）**：maya 的 `ON_POSITION` 字串同時帶有病人姓名
與病歷號。本模組**只取床號與記錄時間**，姓名與病歷號在解析當下就丟棄——
`Position` 裡根本沒有這兩個欄位，下游想留也留不到。

⚠️ 這是本專案唯一會主動對外（院內網）發請求的模組，對象是院方**正式系統**。
因此：預設停用（`maya_enabled=false`）、有輪詢時限、沒有待查裝置時完全不發請求。
開發驗證一律打 `tools/fake_maya.py` 本地假伺服器，絕不對 maya-ap 探測。
"""

import asyncio
import logging
import re
from collections import namedtuple
from datetime import datetime

import aiohttp

log = logging.getLogger("maya")

# maya 上一台設備的目前位置。**刻意只有這兩個欄位**（見檔頭紅線）：
#   bed         床號字串，例 "CCU18"、"3520-1"
#   recorded_at 該位置紀錄的寫入時刻（Unix epoch 秒，以本機時區解讀）
Position = namedtuple("Position", ("bed", "recorded_at"))

# 院方字串同時用得到全形與半形冒號（實際回傳裡「床號：」是全形、「記錄時間:」是半形），
# 兩種都收。床號取到空白為止，才能容納 "3520-1"、"HP13-1" 這類帶床位序號的格式。
_BED_RE = re.compile(r"床號[：:]\s*(\S+)")
_TIME_RE = re.compile(r"記錄時間[：:]\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def _parse_time(text: str):
    """院方時間字串 → Unix epoch 秒；認不得回 None。

    字串不帶時區，視為與本伺服器同一時區（兩台都在院內、都對院內 NTP）。
    時鐘偏移的容忍在 bed_sync 那層處理，不在這裡猜。
    """
    text = (text or "").strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def parse_entry(entry: dict):
    """單筆設備 → `(財編, Position)`；資料不全一律回 `(None, None)` 跳過該筆。

    跳過的情況都是正常現象，不是錯誤：機器閒置中（沒有 ON_POSITION）、
    欄位是 null（院方回傳大量 null 欄位）、時間格式認不得。
    """
    asset = str(entry.get("DEVICE_NO") or "").strip()
    if not asset:
        return None, None
    on_position = str(entry.get("ON_POSITION") or "")
    bed_match = _BED_RE.search(on_position)
    if not bed_match:
        return None, None                 # 沒有位置資訊（機器未在使用中）
    time_match = _TIME_RE.search(on_position)
    recorded_at = _parse_time(time_match.group(1)) if time_match else None
    if recorded_at is None:               # 字串裡沒有記錄時間 → 退回同筆的 RECORD_DATE
        recorded_at = _parse_time(str(entry.get("RECORD_DATE") or ""))
    if recorded_at is None:
        return None, None                 # 沒有時間就無從判斷新舊，寧可跳過
    # 只回床號與時間：姓名與病歷號到此為止，不進入回傳值（檔頭紅線）
    return asset, Position(bed_match.group(1), recorded_at)


def _entries(payload):
    """從回傳中取出設備陣列；認不得的形狀回 None。

    實際回傳是一個 JSON 陣列。這裡多認幾種常見的包裝鍵，是因為院方系統改版
    把陣列包進物件裡的成本很低、而我們每次改都要重新排上院內測試時段。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "Data", "result", "Result", "items", "Items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def parse_device_list(payload) -> dict:
    """整份回傳 → `{財編: Position}`；同一財編有多筆時取記錄時間最新的一筆。"""
    entries = _entries(payload)
    if entries is None:
        log.warning("maya 回傳的資料形狀不是設備清單（整份忽略）")
        return {}
    positions = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        asset, pos = parse_entry(entry)
        if pos is None:
            continue
        current = positions.get(asset)
        if current is None or pos.recorded_at > current.recorded_at:
            positions[asset] = pos
    return positions


async def fetch_positions(url: str, timeout: float = 10.0) -> dict:
    """查詢 maya 全院設備位置 → `{財編: Position}`；**任何失敗回傳 None**。

    回傳 None 與回傳空 dict 是兩件事：None = 這輪沒問到（下輪再試），
    空 dict = 問到了但沒有任何一台有位置資訊。呼叫端據此決定要不要重試。

    這裡吞掉所有預期內的網路/解析例外——院方系統維護或網路抖動絕不該讓
    伺服器崩潰或讓儀表板卡住（CLAUDE.md §2.1）。
    """
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning(f"maya 回應 HTTP {resp.status}（本輪略過）")
                    return None
                # content_type=None：院方回應的 Content-Type 不保證是 application/json
                payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError,
            UnicodeDecodeError) as e:
        log.warning(f"maya 查詢失敗（本輪略過）: {type(e).__name__}: {e}")
        return None
    return parse_device_list(payload)
