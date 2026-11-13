"""SQLite 持久化：上传的原件、逐行候选、用户选择。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import solver

DB_PATH = os.environ.get("APP_DB", os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  raw_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  line_no INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  local_text TEXT NOT NULL,
  water_text TEXT NOT NULL,
  candidates TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS selections (
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  chosen_utc TEXT NOT NULL,
  PRIMARY KEY (file_id, row_id)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    d = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(d, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)


def create_file(name: str, raw_text: str, parsed: list[solver.ParsedRow]) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO files(name, uploaded_at, row_count, raw_text) VALUES (?,?,?,?)",
            (name, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             len(parsed), raw_text))
        fid = cur.lastrowid
        conn.executemany(
            "INSERT INTO rows(file_id, line_no, seq, local_text, water_text, candidates, error)"
            " VALUES (?,?,?,?,?,?,?)",
            [(fid, p.line_no, p.seq, p.local_text, p.water_text,
              json.dumps([solver.iso(c) for c in p.candidates]), p.error)
             for p in parsed])
        return fid


def list_files() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, uploaded_at, row_count FROM files ORDER BY id DESC")]


def get_file(fid: int) -> dict | None:
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, name, uploaded_at, row_count, raw_text FROM files WHERE id=?",
            (fid,)).fetchone()
        return dict(r) if r else None


def get_rows(fid: int) -> list[solver.RowIn]:
    with _connect() as conn:
        out = []
        for r in conn.execute(
                "SELECT id, line_no, seq, local_text, water_text, candidates, error"
                " FROM rows WHERE file_id=? ORDER BY line_no", (fid,)):
            cands = [datetime.fromisoformat(s).astimezone(timezone.utc)
                     for s in json.loads(r["candidates"])]
            out.append(solver.RowIn(
                id=r["id"], line_no=r["line_no"], seq=r["seq"],
                local_text=r["local_text"], water_text=r["water_text"],
                candidates=cands, error=r["error"]))
        return out


def get_selections(fid: int) -> dict[int, datetime]:
    with _connect() as conn:
        return {r["row_id"]: datetime.fromisoformat(r["chosen_utc"]).astimezone(timezone.utc)
                for r in conn.execute(
                    "SELECT row_id, chosen_utc FROM selections WHERE file_id=?", (fid,))}


def set_selection(fid: int, row_id: int, chosen_utc: datetime) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO selections(file_id, row_id, chosen_utc) VALUES (?,?,?)"
            " ON CONFLICT(file_id, row_id) DO UPDATE SET chosen_utc=excluded.chosen_utc",
            (fid, row_id, solver.iso(chosen_utc)))


def clear_selection(fid: int, row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM selections WHERE file_id=? AND row_id=?", (fid, row_id))


def delete_file(fid: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
