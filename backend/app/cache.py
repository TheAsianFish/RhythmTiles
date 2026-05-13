"""SQLite-backed chart cache.

Keyed by (content_hash, difficulty). Stores the Chart JSON blob and a timestamp.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from app.config import settings
from app.models import Chart

_DDL = """
CREATE TABLE IF NOT EXISTS chart_cache (
    content_hash TEXT NOT NULL,
    difficulty   TEXT NOT NULL,
    chart_json   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (content_hash, difficulty)
);
"""


class ChartCache:
    """Thread-safe cache. One connection per process; serialize writes with a lock."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (settings.cache_dir / "charts.sqlite")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(_DDL)
        self._conn.commit()

    def get(self, *, content_hash: str, difficulty: str) -> Chart | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT chart_json FROM chart_cache WHERE content_hash = ? AND difficulty = ?",
                (content_hash, difficulty),
            ).fetchone()
        if row is None:
            return None
        try:
            return Chart.model_validate_json(row[0])
        except Exception:
            # Corrupt or stale schema; treat as miss and let the caller regenerate.
            return None

    def put(self, *, content_hash: str, difficulty: str, chart: Chart) -> None:
        payload = chart.model_dump_json()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO chart_cache(content_hash, difficulty, chart_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (content_hash, difficulty, payload, time.time()),
            )
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM chart_cache")
            self._conn.commit()

    def prune_older_than(self, *, max_age_days: float) -> int:
        """Drop chart-cache rows whose created_at is older than max_age_days.

        Returns the number of rows deleted. Safe to call periodically; the
        cost is a single indexed DELETE. Charts that get pruned are
        regenerated on next request, so this is a disk-space tradeoff
        against one wait per song. Default policy in production: 30 days.
        """
        if max_age_days <= 0:
            return 0
        cutoff = time.time() - max_age_days * 86400.0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM chart_cache WHERE created_at < ?",
                (cutoff,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)
