"""SQLite with atomic forward migrations, backups, and explicit transactions."""
from __future__ import annotations

import sqlite3
import os
import threading
from datetime import datetime
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

from .paths import db_path

# Explicit adapter avoids Python 3.12+'s deprecated implicit datetime binding.
sqlite3.register_adapter(datetime, lambda value: value.isoformat(sep=' '))

# ---------------------------------------------------------------------------
# 保留旧业务表的核心列；新元数据通过版本迁移追加。
# ---------------------------------------------------------------------------
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS fetch_days_v2 (
        date TEXT NOT NULL, source TEXT NOT NULL,
        fetched INTEGER NOT NULL, expected INTEGER, kept INTEGER NOT NULL,
        complete INTEGER NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (date, source))""",
    """CREATE TABLE IF NOT EXISTS kline_sync (
        code TEXT PRIMARY KEY, source TEXT NOT NULL, adjustment TEXT NOT NULL,
        snapshot_key TEXT NOT NULL, start_date TEXT, end_date TEXT,
        target_end TEXT, checked_at TEXT, status TEXT, error TEXT DEFAULT '')""",
    "CREATE TABLE IF NOT EXISTS board_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS stocks (
        code VARCHAR(16) NOT NULL,
        name VARCHAR(32) NOT NULL,
        board VARCHAR(16),
        market_value FLOAT,
        updated_at DATETIME,
        PRIMARY KEY (code)
    )""",
    """CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER NOT NULL,
        ann_id VARCHAR(64) NOT NULL,
        code VARCHAR(16) NOT NULL,
        name VARCHAR(32),
        title VARCHAR(512) NOT NULL,
        date VARCHAR(10) NOT NULL,
        category VARCHAR(32) NOT NULL,
        board VARCHAR(16),
        key_numbers VARCHAR(256),
        url VARCHAR(512),
        summary TEXT,
        created_at DATETIME,
        PRIMARY KEY (id)
    )""",
    """CREATE TABLE IF NOT EXISTS klines (
        id INTEGER NOT NULL,
        code VARCHAR(16) NOT NULL,
        date VARCHAR(10) NOT NULL,
        open FLOAT,
        high FLOAT,
        low FLOAT,
        close FLOAT,
        volume FLOAT,
        change_pct FLOAT,
        PRIMARY KEY (id)
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER NOT NULL,
        mode VARCHAR(16) NOT NULL,
        status VARCHAR(16),
        started_at DATETIME,
        finished_at DATETIME,
        fetched INTEGER,
        new_count INTEGER,
        duration_ms INTEGER,
        peak_rss_mb INTEGER,
        error TEXT,
        PRIMARY KEY (id)
    )""",
    """CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER NOT NULL,
        code VARCHAR(16) NOT NULL,
        name VARCHAR(32),
        reason TEXT,
        category VARCHAR(32),
        created_at DATETIME,
        updated_at DATETIME,
        PRIMARY KEY (id)
    )""",
    # 每天「从源收到多少条原始公告」的记账本。
    # ★ 存在的唯一理由：让"某天是否被抓漏/截断"的判断有**同口径**的两个数可比。
    #   源自报的 total_hits 是过滤前的条数，而 announcements 表里只有过滤后的
    #   （噪声标题被丢掉约 23%）。拿库内条数去比 total_hits，比值恒在 0.7~0.85，
    #   永远低于阈值 —— 于是每一天都被判成"被截断"，每轮都拉满最长窗口。
    #   实测某轮 30 天里 27 天被误判，增量窗口从 2 天退化成 12 天，白抓 6 倍。
    #   这里存下过滤前的原始条数，两个数就同口径了，误判消失。
    """CREATE TABLE IF NOT EXISTS day_fetch (
        date VARCHAR(10) NOT NULL,
        source VARCHAR(16),
        fetched INTEGER NOT NULL,
        kept INTEGER NOT NULL,
        updated_at DATETIME,
        PRIMARY KEY (date)
    )""",
    # 市值按「股票代码 + 日期」记账，与 K 线同一口径。
    # 不用 stocks.market_value 那个单值字段作为历史：它是"首次拿到就缓存"的
    # 参考值，覆盖式写入后无法回答"某天市值是多少"。
    """CREATE TABLE IF NOT EXISTS market_caps (
        code VARCHAR(16) NOT NULL,
        date VARCHAR(10) NOT NULL,
        market_value FLOAT NOT NULL,
        updated_at DATETIME,
        PRIMARY KEY (code, date)
    )""",
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_announcements_ann_id ON announcements (ann_id)",
    "CREATE INDEX IF NOT EXISTS ix_announcements_code ON announcements (code)",
    "CREATE INDEX IF NOT EXISTS ix_announcements_date ON announcements (date)",
    "CREATE INDEX IF NOT EXISTS ix_announcements_category ON announcements (category)",
    "CREATE INDEX IF NOT EXISTS ix_announcements_board ON announcements (board)",
    # 高频组合筛选
    "CREATE INDEX IF NOT EXISTS idx_date_cat ON announcements (date, category)",
    "CREATE INDEX IF NOT EXISTS idx_board_date ON announcements (board, date)",
    "CREATE INDEX IF NOT EXISTS idx_code_date ON announcements (code, date)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_kline_code_date ON klines (code, date)",
    "CREATE INDEX IF NOT EXISTS ix_klines_code ON klines (code)",
    "CREATE INDEX IF NOT EXISTS ix_runs_status ON runs (status)",
    "CREATE INDEX IF NOT EXISTS ix_stocks_name ON stocks (name)",
    "CREATE INDEX IF NOT EXISTS ix_stocks_board ON stocks (board)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_favorites_code ON favorites (code)",
    "CREATE INDEX IF NOT EXISTS ix_favorites_created_at ON favorites (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_market_caps_code ON market_caps (code)",
    "CREATE INDEX IF NOT EXISTS ix_market_caps_date ON market_caps (date)",
]

# 版本化迁移：新增结构只向前加，不动历史数据
MIGRATIONS: dict[int, list[str]] = {
    1: ["ALTER TABLE runs ADD COLUMN peak_rss_mb INTEGER"],
    2: [
        "ALTER TABLE announcements ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE announcements ADD COLUMN is_noise INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE klines ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE klines ADD COLUMN adjustment TEXT NOT NULL DEFAULT 'unknown'",
        "ALTER TABLE klines ADD COLUMN snapshot_key TEXT NOT NULL DEFAULT 'legacy'",
    ],
}

# 8 vCPU / 4 GiB 上的合理值
_PAGE_CACHE_KB = -16384
_MMAP_BYTES = 268435456          # 256 MB

_local = threading.local()
_write_lock = threading.RLock()


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    for pragma in (
        "PRAGMA journal_mode=WAL",        # 读写不互斥
        "PRAGMA synchronous=FULL",
        "PRAGMA busy_timeout=10000",      # 写锁等待，别一撞就报错
        f"PRAGMA cache_size={_PAGE_CACHE_KB}",
        "PRAGMA temp_store=MEMORY",       # 排序/分组别落盘
        f"PRAGMA mmap_size={_MMAP_BYTES}",
    ):
        conn.execute(pragma)


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30.0, isolation_level=None)
    _configure(conn)
    return conn


def conn() -> sqlite3.Connection:
    """当前线程的连接（sqlite3 连接不能跨线程共享）。"""
    c: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    path = os.path.realpath(db_path())
    if c is not None and getattr(_local, "path", None) != path:
        close()
        c = None
    if c is None:
        c = _open()
        _local.conn = c
        _local.path = path
    return c


def close() -> None:
    c: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        c.close()
        _local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """显式事务。写操作串行化，避免 WAL 下并发写报 SQLITE_BUSY。"""
    with _write_lock:
        c = conn()
        nested = c.in_transaction
        c.execute("SAVEPOINT board_write" if nested else "BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK TO board_write" if nested else "ROLLBACK")
            if nested:
                c.execute("RELEASE board_write")
            raise
        else:
            c.execute("RELEASE board_write" if nested else "COMMIT")


def init_db() -> None:
    """Back up existing legacy databases before the first v2 migration."""
    c = conn()
    existing = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='announcements'").fetchone()
    if existing:
        has_version = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        version = c.execute('SELECT COALESCE(MAX(version),0) FROM schema_version').fetchone()[0] if has_version else 0
        if version > max(MIGRATIONS):
            raise RuntimeError('数据库版本高于本程序；禁止旧程序修改新数据库')
        if version < 2:
            from pathlib import Path
            from datetime import datetime, timezone
            from .maintenance import snapshot
            if c.in_transaction: raise RuntimeError('迁移必须在业务事务外执行')
            target=Path(db_path()).parent/'backups'/('pre-v2-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.db')
            snapshot(db_path(),target)
    with transaction() as c:
        for stmt in SCHEMA:
            c.execute(stmt)
        version = c.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0] or 0
        for target in sorted(MIGRATIONS):
            if target <= version:
                continue
            for sql in MIGRATIONS[target]:
                try:
                    c.execute(sql)
                except sqlite3.Error as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            c.execute("DELETE FROM schema_version")
            c.execute("INSERT INTO schema_version(version) VALUES (?)", (target,))
        for stmt in INDEXES:
            c.execute(stmt)
        c.execute("""CREATE VIEW IF NOT EXISTS current_klines AS
            SELECT k.* FROM klines k LEFT JOIN kline_sync s ON s.code=k.code
            WHERE k.snapshot_key=COALESCE(s.snapshot_key, 'legacy')""")


def query(sql: str, params: Any = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def one(sql: str, params: Any = ()) -> Optional[sqlite3.Row]:
    return conn().execute(sql, params).fetchone()


def scalar(sql: str, params: Any = (), default: Any = None) -> Any:
    row = one(sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]


def table_stats() -> dict:
    """各表行数，健康自检用。"""
    return {t: scalar("SELECT COUNT(*) FROM %s" % t, default=0)
            for t in ("stocks", "announcements", "klines", "runs",
                      "favorites", "day_fetch", "market_caps")}
