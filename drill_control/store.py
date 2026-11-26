"""SQLite 持久化：轮次、指令、回执、中断、接管与替身收件箱。

所有演练状态都落库，进程重启后仍可查询轮次、被中断的范围、
各班处置结果和接管凭据。
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  team TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  status TEXT NOT NULL,
  started_tick INTEGER NOT NULL,
  from_run TEXT,
  same_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
  call_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ticket TEXT NOT NULL,
  scope TEXT NOT NULL,
  action TEXT NOT NULL,
  operation TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  namespace TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  ticket TEXT NOT NULL,
  operation TEXT NOT NULL,
  namespace TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  state TEXT NOT NULL,
  tick INTEGER NOT NULL,
  matched_call_id INTEGER,
  flag TEXT
);
CREATE TABLE IF NOT EXISTS interruptions (
  interruption_id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident TEXT NOT NULL,
  supervisor TEXT NOT NULL,
  scopes TEXT NOT NULL,
  tick INTEGER NOT NULL,
  lifted_tick INTEGER
);
CREATE TABLE IF NOT EXISTS handoffs (
  handoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident TEXT NOT NULL,
  ticket TEXT NOT NULL,
  scope TEXT NOT NULL,
  operation TEXT NOT NULL,
  status TEXT NOT NULL,
  credential_receipt_id INTEGER,
  reason TEXT,
  tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS standin_ops (
  op_id INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint TEXT NOT NULL,
  namespace TEXT NOT NULL,
  operation TEXT NOT NULL,
  ticket TEXT NOT NULL,
  action TEXT NOT NULL,
  run_id TEXT,
  misdelivered INTEGER NOT NULL,
  status TEXT NOT NULL,
  tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_packages (
  package_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ticket TEXT NOT NULL,
  operation TEXT NOT NULL,
  review_only INTEGER NOT NULL,
  tick INTEGER NOT NULL
);
"""


class Store:
    """对 SQLite 的薄封装：建表、插入、查询与元信息读写。"""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def execute(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor

    def insert(self, table, **fields):
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(fields.values())
        )
        return cursor.lastrowid

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def get_meta(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
