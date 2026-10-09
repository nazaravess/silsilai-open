"""Одна память: люди, разговоры, идеи, расход, оценки моделей. SQLite, без внешних служб."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  tg_id INTEGER PRIMARY KEY,
  name TEXT,
  lang TEXT DEFAULT 'ru',
  role TEXT DEFAULT 'companion',      -- founder | cofounder | companion | guest
  invited_by INTEGER,
  joined_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER, ts REAL, role TEXT, text TEXT, task_class TEXT, model_id TEXT
);
CREATE TABLE IF NOT EXISTS ideas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER, ts REAL, kind TEXT,           -- idea | question | objection | offer
  topic TEXT, text TEXT, status TEXT DEFAULT 'new'   -- new | accepted | rejected | merged
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, tg_id INTEGER, task_class TEXT, model_id TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, ok INTEGER, latency_ms INTEGER
);
CREATE TABLE IF NOT EXISTS model_scores (
  model_id TEXT PRIMARY KEY, quality REAL, exam_ts REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS quota (
  model_id TEXT, day TEXT, used INTEGER, PRIMARY KEY (model_id, day)
);
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY, owner INTEGER, uses INTEGER DEFAULT 0
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- люди ----
    def upsert_user(self, tg_id: int, name: str, lang: str = "ru", invited_by: int | None = None):
        with self.conn() as c:
            row = c.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO users(tg_id,name,lang,invited_by,joined_at) VALUES(?,?,?,?,?)",
                    (tg_id, name, lang, invited_by, time.time()),
                )
            else:
                c.execute("UPDATE users SET name=?, lang=? WHERE tg_id=?", (name, lang, tg_id))

    def get_user(self, tg_id: int):
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()

    def set_role(self, tg_id: int, role: str):
        with self.conn() as c:
            c.execute("UPDATE users SET role=? WHERE tg_id=?", (role, tg_id))

    # ---- разговор ----
    def add_message(self, tg_id: int, role: str, text: str, task_class: str = "", model_id: str = ""):
        with self.conn() as c:
            c.execute(
                "INSERT INTO messages(tg_id,ts,role,text,task_class,model_id) VALUES(?,?,?,?,?,?)",
                (tg_id, time.time(), role, text, task_class, model_id),
            )

    def history(self, tg_id: int, limit: int = 10) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT role,text FROM messages WHERE tg_id=? ORDER BY id DESC LIMIT ?", (tg_id, limit)
            ).fetchall()
        return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]

    # ---- идеи ----
    def add_idea(self, tg_id: int, kind: str, topic: str, text: str):
        with self.conn() as c:
            c.execute(
                "INSERT INTO ideas(tg_id,ts,kind,topic,text) VALUES(?,?,?,?,?)",
                (tg_id, time.time(), kind, topic, text),
            )

    def ideas_since(self, ts: float):
        with self.conn() as c:
            return c.execute(
                "SELECT i.*, u.name FROM ideas i LEFT JOIN users u ON u.tg_id=i.tg_id "
                "WHERE i.ts>=? ORDER BY i.topic, i.ts",
                (ts,),
            ).fetchall()

    # ---- расход ----
    def log_usage(self, tg_id, task_class, model_id, tokens_in, tokens_out, cost_usd, ok, latency_ms):
        with self.conn() as c:
            c.execute(
                "INSERT INTO usage(ts,tg_id,task_class,model_id,tokens_in,tokens_out,cost_usd,ok,latency_ms)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), tg_id, task_class, model_id, tokens_in, tokens_out, cost_usd, int(ok), latency_ms),
            )

    def spent_since(self, ts: float) -> float:
        with self.conn() as c:
            r = c.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE ts>=?", (ts,)).fetchone()
        return float(r["s"])

    def usage_report(self, ts: float):
        with self.conn() as c:
            return c.execute(
                "SELECT model_id, task_class, COUNT(*) n, SUM(tokens_in) tin, SUM(tokens_out) tout, "
                "SUM(cost_usd) cost, AVG(latency_ms) lat FROM usage WHERE ts>=? "
                "GROUP BY model_id, task_class ORDER BY cost DESC, n DESC",
                (ts,),
            ).fetchall()

    # ---- квоты ----
    def quota_used(self, model_id: str, day: str) -> int:
        with self.conn() as c:
            r = c.execute("SELECT used FROM quota WHERE model_id=? AND day=?", (model_id, day)).fetchone()
        return int(r["used"]) if r else 0

    def quota_inc(self, model_id: str, day: str):
        with self.conn() as c:
            c.execute(
                "INSERT INTO quota(model_id,day,used) VALUES(?,?,1) "
                "ON CONFLICT(model_id,day) DO UPDATE SET used=used+1",
                (model_id, day),
            )

    # ---- оценки ----
    def set_score(self, model_id: str, quality: float, notes: str = ""):
        with self.conn() as c:
            c.execute(
                "INSERT INTO model_scores(model_id,quality,exam_ts,notes) VALUES(?,?,?,?) "
                "ON CONFLICT(model_id) DO UPDATE SET quality=excluded.quality, exam_ts=excluded.exam_ts, notes=excluded.notes",
                (model_id, quality, time.time(), notes),
            )

    def scores(self) -> dict[str, float]:
        with self.conn() as c:
            return {r["model_id"]: float(r["quality"]) for r in c.execute("SELECT * FROM model_scores")}

    # ---- приглашения ----
    def invite_code(self, tg_id: int) -> str:
        code = f"a{tg_id:x}"
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO invites(code,owner) VALUES(?,?)", (code, tg_id))
        return code

    def invite_owner(self, code: str) -> int | None:
        with self.conn() as c:
            r = c.execute("SELECT owner FROM invites WHERE code=?", (code,)).fetchone()
            if r:
                c.execute("UPDATE invites SET uses=uses+1 WHERE code=?", (code,))
        return int(r["owner"]) if r else None
