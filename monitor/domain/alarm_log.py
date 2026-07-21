"""
領域層 — SQLite 警報 episode 歷史
=================================
`alarm` 訊息是目前作用中警報的全量快照。這裡以 ``(device, cp, code)``
配對出一段完整警報：第一次看到建立 episode，從清單消失時寫入解除時間。

資料庫只保存機台編號與警報內容，不保存病歷號、床邊波形或量測值。已結束
episode 僅保留設定天數；作用中的警報不因保留期限而被刪除。
"""

import csv
import io
import logging
import os
import sqlite3
import time
from datetime import datetime


EXPORT_FIELDS = (
    "episode_id", "device_id", "started_at", "ended_at", "duration_seconds",
    "status", "start_reason", "end_reason", "cp", "code", "prio", "text",
)


class AlarmLog:
    """單一 SQLite 連線，由 TelemetryHub 在其 event loop 執行緒內使用。"""

    def __init__(self, db_path: str = "", retention_days: int = 7, now_fn=None):
        self.db_path = db_path
        self.retention_days = max(1, int(retention_days))
        self._now = now_fn or time.time
        self.log = logging.getLogger("alarm_log")
        self._conn = None
        self._active = {}       # device -> {(cp, code): episode_id}
        self._seen_devices = set()
        self._next_cleanup_at = 0.0

        if not db_path:
            return
        try:
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(db_path, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self._create_schema()
            now = float(self._now())
            # 前一次程序若中斷，資料庫裡的 active 無法證明仍持續；標成結束不明。
            self._close_stale_active(now)
            self._cleanup(now, force=True)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.log.warning(f"警報 SQLite 無法啟用，停用歷史紀錄: {exc}")
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self.db_path = ""

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def _create_schema(self):
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS alarm_episode (
                    episode_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id        TEXT NOT NULL,
                    cp               TEXT NOT NULL,
                    code             TEXT NOT NULL,
                    prio             INTEGER,
                    text             TEXT NOT NULL DEFAULT '',
                    started_at       REAL NOT NULL,
                    source_started_at REAL,
                    ended_at         REAL,
                    source_ended_at  REAL,
                    status           TEXT NOT NULL CHECK(status IN ('active', 'cleared', 'unknown')),
                    start_reason     TEXT NOT NULL CHECK(start_reason IN ('appeared', 'observed_active')),
                    end_reason       TEXT,
                    duration_seconds REAL
                )
            """)
            self._conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS alarm_episode_one_active
                ON alarm_episode(device_id, cp, code) WHERE status = 'active'
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS alarm_episode_device_time
                ON alarm_episode(device_id, started_at DESC, episode_id DESC)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS alarm_episode_retention
                ON alarm_episode(status, ended_at)
            """)
            self._conn.execute("PRAGMA user_version = 1")

    @staticmethod
    def _source_ts(value):
        try:
            result = float(value)
            return result if result > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _prio(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _key(alarm: dict):
        return str(alarm.get("cp", "")), str(alarm.get("code", ""))

    @staticmethod
    def _iso(ts):
        if ts is None:
            return None
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")

    def _close_stale_active(self, now: float):
        with self._conn:
            self._conn.execute("""
                UPDATE alarm_episode
                   SET ended_at = ?, status = 'unknown', end_reason = 'server_restart',
                       duration_seconds = MAX(0, ? - started_at)
                 WHERE status = 'active'
            """, (now, now))

    def _cleanup(self, now=None, force=False):
        if not self.enabled:
            return 0
        now = float(self._now() if now is None else now)
        if not force and now < self._next_cleanup_at:
            return 0
        cutoff = now - self.retention_days * 86400
        with self._conn:
            cur = self._conn.execute("""
                DELETE FROM alarm_episode
                 WHERE status != 'active' AND COALESCE(ended_at, started_at) < ?
            """, (cutoff,))
            deleted = cur.rowcount
            if deleted:
                self._conn.execute("PRAGMA incremental_vacuum(1000)")
        self._next_cleanup_at = now + 3600
        if deleted:
            self.log.info(f"已清除 {deleted} 筆超過 {self.retention_days} 天的警報 episode")
        return deleted

    def on_alarm(self, device: str, alarms: list, source_ts=None):
        """接收全量警報快照，建立、更新或結束各警報 episode。"""
        if not self.enabled:
            return
        device = str(device)
        now = float(self._now())
        self._cleanup(now)
        source_ts = self._source_ts(source_ts)
        current = {self._key(alarm): alarm for alarm in alarms}
        previous = self._active.get(device, {})
        first_snapshot = device not in self._seen_devices
        next_active = {}

        try:
            with self._conn:
                for key, alarm in current.items():
                    cp, code = key
                    prio = self._prio(alarm.get("prio"))
                    text = str(alarm.get("text", ""))
                    episode_id = previous.get(key)
                    if episode_id is None:
                        start_reason = "observed_active" if first_snapshot else "appeared"
                        cur = self._conn.execute("""
                            INSERT INTO alarm_episode (
                                device_id, cp, code, prio, text, started_at,
                                source_started_at, status, start_reason
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                        """, (device, cp, code, prio, text, now, source_ts, start_reason))
                        episode_id = cur.lastrowid
                    else:
                        # 作用期間警報文字或 MEDIBUS 優先值變化時保留最新值，不另拆一段。
                        self._conn.execute("""
                            UPDATE alarm_episode SET prio = ?, text = ?
                             WHERE episode_id = ? AND status = 'active'
                        """, (prio, text, episode_id))
                    next_active[key] = episode_id

                for key, episode_id in previous.items():
                    if key in current:
                        continue
                    self._conn.execute("""
                        UPDATE alarm_episode
                           SET ended_at = ?, source_ended_at = ?, status = 'cleared',
                               end_reason = 'cleared', duration_seconds = MAX(0, ? - started_at)
                         WHERE episode_id = ? AND status = 'active'
                    """, (now, source_ts, now, episode_id))
        except sqlite3.Error as exc:
            self.log.warning(f"{device} 警報 SQLite 寫入失敗: {exc}")
            return

        self._active[device] = next_active
        self._seen_devices.add(device)

    def on_disconnect(self, device: str, reason: str = "device_offline"):
        """裝置離線不能當成警報解除；結束時間記為未知來源。"""
        if not self.enabled:
            return
        device = str(device)
        active = self._active.get(device, {})
        if not active:
            self._active.pop(device, None)
            self._seen_devices.discard(device)
            return
        now = float(self._now())
        try:
            with self._conn:
                for episode_id in active.values():
                    self._conn.execute("""
                        UPDATE alarm_episode
                           SET ended_at = ?, status = 'unknown', end_reason = ?,
                               duration_seconds = MAX(0, ? - started_at)
                         WHERE episode_id = ? AND status = 'active'
                    """, (now, reason, now, episode_id))
        except sqlite3.Error as exc:
            self.log.warning(f"{device} 離線狀態寫入警報 SQLite 失敗: {exc}")
            return
        self._active.pop(device, None)
        self._seen_devices.discard(device)

    def forget(self, device: str):
        """管理員移除裝置只清記憶體狀態；SQLite 歷史保留到七天期限。"""
        self.on_disconnect(device, reason="device_removed")
        self._active.pop(str(device), None)
        self._seen_devices.discard(str(device))

    def _row_to_dict(self, row, now=None):
        now = float(self._now() if now is None else now)
        ended = row["ended_at"]
        duration = row["duration_seconds"]
        if row["status"] == "active":
            duration = max(0.0, now - row["started_at"])
        return {
            "episode_id": row["episode_id"],
            "device_id": row["device_id"],
            "cp": row["cp"],
            "code": row["code"],
            "prio": row["prio"],
            "text": row["text"],
            "started_at": self._iso(row["started_at"]),
            "source_started_at": self._iso(row["source_started_at"]),
            "ended_at": self._iso(ended),
            "source_ended_at": self._iso(row["source_ended_at"]),
            "duration_seconds": round(float(duration or 0), 1),
            "status": row["status"],
            "start_reason": row["start_reason"],
            "end_reason": row["end_reason"],
        }

    def recent(self, device: str, limit: int = 50) -> list:
        """回傳該機台最近警報 episode，新到舊；不含病歷號。"""
        if not self.enabled:
            return []
        limit = max(1, min(100, int(limit)))
        now = float(self._now())
        self._cleanup(now)
        try:
            rows = self._conn.execute("""
                SELECT * FROM alarm_episode
                 WHERE device_id = ?
                 ORDER BY started_at DESC, episode_id DESC LIMIT ?
            """, (str(device), limit)).fetchall()
            return [self._row_to_dict(row, now) for row in rows]
        except sqlite3.Error as exc:
            self.log.warning(f"{device} 警報 SQLite 讀取失敗: {exc}")
            return []

    def export_csv(self, device: str):
        """輸出該機台七天內 episode CSV；沒有資料時回傳 None。"""
        if not self.enabled:
            return None
        now = float(self._now())
        self._cleanup(now)
        try:
            db_rows = self._conn.execute("""
                SELECT * FROM alarm_episode
                 WHERE device_id = ?
                 ORDER BY started_at, episode_id
            """, (str(device),)).fetchall()
        except sqlite3.Error as exc:
            self.log.warning(f"{device} 警報 SQLite 匯出失敗: {exc}")
            return None
        if not db_rows:
            return None
        rows = [self._row_to_dict(row, now) for row in db_rows]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()

    def close(self, reason: str = "server_shutdown"):
        if not self.enabled:
            return
        for device in list(self._active):
            self.on_disconnect(device, reason=reason)
        self._conn.close()
        self._conn = None
