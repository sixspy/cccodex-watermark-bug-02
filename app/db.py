import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None, *, isolation_level: str | None = "") -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=isolation_level,
        timeout=15.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_transaction(db_path: str | None = None):
    """Open one exclusive write transaction.

    ``BEGIN IMMEDIATE`` takes the SQLite write lock up front, so two值班
    clicking "续写" at the same time queue behind one another instead of
    double-writing residue rows.  The caller must keep every write inside the
    block: the context performs exactly one COMMIT.
    """

    conn = connect(db_path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  stamp_state TEXT NOT NULL DEFAULT '未盖',
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stamp_rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES run_batches(id) ON DELETE CASCADE,
  row_no INTEGER NOT NULL,
  partition_code TEXT NOT NULL,
  row_status TEXT NOT NULL DEFAULT '待盖' CHECK (row_status IN ('待盖','已盖','失败')),
  watermark_value TEXT,
  stamped_at TEXT,
  UNIQUE(batch_id, row_no)
);

CREATE TABLE IF NOT EXISTS residue_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES run_batches(id),
  run_code TEXT NOT NULL,
  watermark_value TEXT NOT NULL,
  boundary TEXT NOT NULL CHECK (boundary IN ('回滚','留存')),
  committed_rows INTEGER NOT NULL DEFAULT 0,
  pending_rows INTEGER NOT NULL DEFAULT 0,
  rolled_back_rows INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('待续写','已收口')),
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_residue_open
  ON residue_segments(batch_id) WHERE state='待续写';

CREATE TABLE IF NOT EXISTS makeup_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  residue_id INTEGER NOT NULL UNIQUE REFERENCES residue_segments(id),
  batch_id INTEGER NOT NULL REFERENCES run_batches(id),
  run_code TEXT NOT NULL,
  task_code TEXT NOT NULL UNIQUE,
  remaining_codes TEXT NOT NULL DEFAULT '[]',
  remaining_rows INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('待补盖','已补盖','已作废')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS stamp_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER,
  residue_id INTEGER,
  event TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  expected_rows INTEGER NOT NULL DEFAULT 0,
  stamped_rows INTEGER NOT NULL DEFAULT 0,
  gap_rows INTEGER NOT NULL DEFAULT 0,
  residue_count INTEGER NOT NULL DEFAULT 0,
  retained_rows INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an already-seeded database up to the residue schema."""

    if "stamp_state" not in _columns(conn, "run_batches"):
        conn.execute("ALTER TABLE run_batches ADD COLUMN stamp_state TEXT NOT NULL DEFAULT '未盖'")
    gap_columns = {
        "expected_rows": "INTEGER NOT NULL DEFAULT 0",
        "stamped_rows": "INTEGER NOT NULL DEFAULT 0",
        "gap_rows": "INTEGER NOT NULL DEFAULT 0",
        "residue_count": "INTEGER NOT NULL DEFAULT 0",
        "retained_rows": "INTEGER NOT NULL DEFAULT 0",
    }
    existing = _columns(conn, "gap_reports")
    for name, ddl in gap_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE gap_reports ADD COLUMN {name} {ddl}")
    conn.execute("UPDATE run_batches SET stamp_state='已盖' WHERE status='已盖印' AND stamp_state<>'已盖'")
    conn.execute("UPDATE run_batches SET stamp_state='未盖' WHERE status<>'已盖印' AND stamp_state<>'未盖'")


def _ensure_stamp_rows(conn: sqlite3.Connection) -> None:
    batches = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
    for batch in batches:
        has_rows = conn.execute(
            "SELECT 1 FROM stamp_rows WHERE batch_id=? LIMIT 1", (batch["id"],)
        ).fetchone()
        if has_rows:
            continue
        done = batch["status"] == "已盖印"
        for row_no in range(1, 5):
            conn.execute(
                "INSERT INTO stamp_rows(batch_id, row_no, partition_code, row_status, watermark_value, stamped_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    batch["id"],
                    row_no,
                    f"{batch['run_code']}-p{row_no}",
                    "已盖" if done else "待盖",
                    batch["watermark_value"] if done else None,
                    batch["stamped_at"] if done else None,
                ),
            )


SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not row:
            for table, col, ver, status in SEED_TEMPLATES:
                conn.execute(
                    "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                    "VALUES (?,?,?,?)",
                    (table, col, ver, status),
                )
            for table, code, biz, status, wm in SEED_BATCHES:
                conn.execute(
                    "INSERT INTO run_batches(table_name, run_code, biz_date, status, stamp_state, watermark_value, stamped_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        table,
                        code,
                        biz,
                        status,
                        "已盖" if status == "已盖印" else "未盖",
                        wm,
                        "2026-11-13 18:00:00" if status == "已盖印" else None,
                    ),
                )
            for up, down in SEED_EDGES:
                conn.execute(
                    "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                    (up, down),
                )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        _ensure_stamp_rows(conn)
