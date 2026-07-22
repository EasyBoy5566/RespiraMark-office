"""
領域層 — SQLite 系統狀態歷史
==============================
每台 Pi 的系統健康資料統一寫入單一 SQLite；CSV 僅在管理員下載時即時產生，
伺服器端不保留匯出檔。資料只含機台編號與系統指標，不含病人代碼。
"""

import csv
import io
import logging
import os
import sqlite3
import time


EXPORT_FIELDS = (
    "time", "cpu", "mem", "temp", "disk_pct", "disk_free", "throttled", "uptime",
)
SYS_FIELDS = EXPORT_FIELDS[1:]


class SysLog:
    """單一 SQLite 連線，由 TelemetryHub 在其 event loop 執行緒內使用。"""

    def __init__(self, db_path: str = "", retention_days: int = 7, now_fn=None):
        self.db_path = db_path
        self.retention_days = max(1, int(retention_days))
        self._now = now_fn or time.time
        self.log = logging.getLogger("sys_log")
        self._conn = None
        self._next_cleanup_at = 0.0
        self._write_failed = False

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
            self._cleanup(force=True)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.log.warning(f"系統狀態 SQLite 無法啟用，停用歷史紀錄: {exc}")
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
                CREATE TABLE IF NOT EXISTS system_sample (
                    sample_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id   TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    source_at   REAL,
                    cpu         REAL,
                    mem         REAL,
                    temp        REAL,
                    disk_pct    REAL,
                    disk_free   REAL,
                    throttled   TEXT,
                    uptime      REAL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS system_sample_device_time
                ON system_sample(device_id, received_at DESC, sample_id DESC)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS system_sample_retention
                ON system_sample(received_at)
            """)
            self._conn.execute("PRAGMA user_version = 1")

    @staticmethod
    def _source_ts(value):
        try:
            result = float(value)
            return result if result > 0 else None
        except (TypeError, ValueError):
            return None

    def _cleanup(self, now=None, force=False):
        if not self.enabled:
            return 0
        now = float(self._now() if now is None else now)
        if not force and now < self._next_cleanup_at:
            return 0
        cutoff = now - self.retention_days * 86400
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM system_sample WHERE received_at < ?", (cutoff,))
            deleted = cur.rowcount
            if deleted:
                self._conn.execute("PRAGMA incremental_vacuum(1000)")
        self._next_cleanup_at = now + 3600
        if deleted:
            self.log.info(f"已清除 {deleted} 筆超過 {self.retention_days} 天的系統狀態")
        return deleted

    def record(self, device: str, msg: dict, received_at=None) -> bool:
        """寫入一筆系統狀態；received_at 一律採伺服器時間供七天清理。"""
        if not self.enabled:
            return False
        now = float(self._now() if received_at is None else received_at)
        self._cleanup(now)
        values = [msg.get(field) if msg.get(field) is not None else None
                  for field in SYS_FIELDS]
        try:
            with self._conn:
                self._conn.execute("""
                    INSERT INTO system_sample (
                        device_id, received_at, source_at, cpu, mem, temp,
                        disk_pct, disk_free, throttled, uptime
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (str(device), now, self._source_ts(msg.get("ts")), *values))
            if self._write_failed:
                self.log.info("系統狀態 SQLite 已恢復寫入")
            self._write_failed = False
            return True
        except sqlite3.Error as exc:
            if not self._write_failed:
                self.log.warning(f"系統狀態 SQLite 寫入失敗: {exc}")
            self._write_failed = True
            return False

    def export_csv(self, device: str):
        """即時產生該機台最近七天 CSV；沒有資料時回傳 None。"""
        if not self.enabled:
            return None
        now = float(self._now())
        self._cleanup(now)
        try:
            rows = self._conn.execute("""
                SELECT source_at, received_at, cpu, mem, temp, disk_pct,
                       disk_free, throttled, uptime
                  FROM system_sample
                 WHERE device_id = ?
                 ORDER BY received_at, sample_id
            """, (str(device),)).fetchall()
        except sqlite3.Error as exc:
            self.log.warning(f"{device} 系統狀態 SQLite 匯出失敗: {exc}")
            return None
        if not rows:
            return None

        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(EXPORT_FIELDS)
        for row in rows:
            timestamp = row["source_at"] or row["received_at"]
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)),
                *[row[field] if row[field] is not None else "" for field in SYS_FIELDS],
            ])
        return output.getvalue()

    def close(self):
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None
