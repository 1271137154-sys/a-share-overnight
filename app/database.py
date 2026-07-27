import json
import sqlite3
from datetime import datetime
from pathlib import Path
import pandas as pd

class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.initialize()

    def connect(self):
        return sqlite3.connect(self.path)

    def initialize(self):
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS daily_snapshots (
                trade_date TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, source TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS selections (
                trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
                strategy_ids TEXT NOT NULL, metrics TEXT NOT NULL, failures TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS next_day_performance (
                selection_date TEXT NOT NULL, code TEXT NOT NULL, measured_date TEXT NOT NULL,
                open_return REAL, high_return REAL, low_return REAL, close_return REAL,
                nine_thirty_five_return REAL, PRIMARY KEY (selection_date, code)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS task_runs (
                task_name TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, records_count INTEGER, message TEXT,
                PRIMARY KEY (task_name, trade_date)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS rejections (
                trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
                metrics TEXT NOT NULL, failures TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            )""")

    def save_snapshot(self, trade_date: str, frame: pd.DataFrame, source: str):
        payload = frame.to_json(orient="records", force_ascii=False)
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO daily_snapshots VALUES (?, ?, ?, ?)",
                         (trade_date, datetime.now().isoformat(timespec="seconds"), source, payload))

    def load_snapshot(self, trade_date: str):
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM daily_snapshots WHERE trade_date=?", (trade_date,)).fetchone()
        return pd.DataFrame(json.loads(row[0])) if row else None

    def save_selections(self, trade_date: str, records: list[dict]):
        with self.connect() as conn:
            conn.execute("DELETE FROM selections WHERE trade_date=?", (trade_date,))
            conn.executemany("INSERT INTO selections VALUES (?, ?, ?, ?, ?, ?)", [
                (trade_date, r["code"], r["name"], json.dumps(r["strategy_ids"], ensure_ascii=False),
                 json.dumps(r["metrics"], ensure_ascii=False), json.dumps(r["failures"], ensure_ascii=False))
                for r in records
            ])

    def load_selections(self, trade_date: str):
        with self.connect() as conn:
            rows = conn.execute("SELECT code,name,strategy_ids,metrics,failures FROM selections WHERE trade_date=? ORDER BY name", (trade_date,)).fetchall()
        return [{"code": r[0], "name": r[1], "strategy_ids": json.loads(r[2]), "metrics": json.loads(r[3]), "failures": json.loads(r[4])} for r in rows]

    def save_rejections(self, trade_date: str, records: list[dict]):
        with self.connect() as conn:
            conn.execute("DELETE FROM rejections WHERE trade_date=?", (trade_date,))
            conn.executemany("INSERT INTO rejections VALUES (?, ?, ?, ?, ?, ?)", [
                (trade_date, item["code"], item["name"], json.dumps(item["metrics"], ensure_ascii=False), json.dumps(item["failures"], ensure_ascii=False), item["reason"])
                for item in records
            ])

    def load_rejections(self, trade_date: str, limit: int = 100):
        with self.connect() as conn:
            rows = conn.execute("SELECT code,name,metrics,failures,reason FROM rejections WHERE trade_date=? ORDER BY name LIMIT ?", (trade_date, limit)).fetchall()
        return [{"code": row[0], "name": row[1], "metrics": json.loads(row[2]), "failures": json.loads(row[3]), "reason": row[4]} for row in rows]

    def dates(self):
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT trade_date FROM daily_snapshots ORDER BY trade_date DESC")]

    def save_performance(self, selection_date: str, code: str, measured_date: str, values: dict):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO next_day_performance VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (selection_date, code, measured_date, values.get("open_return"), values.get("high_return"),
                          values.get("low_return"), values.get("close_return"), values.get("nine_thirty_five_return")))

    def performance_for_date(self, selection_date: str) -> dict:
        with self.connect() as conn:
            rows = conn.execute("SELECT code,measured_date,open_return,high_return,low_return,close_return,nine_thirty_five_return FROM next_day_performance WHERE selection_date=?", (selection_date,)).fetchall()
        return {r[0]: {"measured_date": r[1], "open_return": r[2], "high_return": r[3], "low_return": r[4], "close_return": r[5], "nine_thirty_five_return": r[6]} for r in rows}

    def begin_task(self, task_name: str, trade_date: str) -> bool:
        """Return False when a successful task already exists for the date."""
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            existing = conn.execute("SELECT status FROM task_runs WHERE task_name=? AND trade_date=?", (task_name, trade_date)).fetchone()
            if existing and existing[0] == "success":
                return False
            conn.execute("INSERT OR REPLACE INTO task_runs (task_name,trade_date,status,started_at,finished_at,records_count,message) VALUES (?, ?, 'running', ?, NULL, NULL, NULL)", (task_name, trade_date, now))
        return True

    def finish_task(self, task_name: str, trade_date: str, status: str, message: str, records_count: int | None = None):
        with self.connect() as conn:
            conn.execute("UPDATE task_runs SET status=?, finished_at=?, records_count=?, message=? WHERE task_name=? AND trade_date=?", (status, datetime.now().isoformat(timespec="seconds"), records_count, message, task_name, trade_date))

    def latest_task(self, task_name: str):
        with self.connect() as conn:
            row = conn.execute("SELECT trade_date,status,started_at,finished_at,records_count,message FROM task_runs WHERE task_name=? ORDER BY started_at DESC LIMIT 1", (task_name,)).fetchone()
        return dict(zip(["trade_date", "status", "started_at", "finished_at", "records_count", "message"], row)) if row else None
