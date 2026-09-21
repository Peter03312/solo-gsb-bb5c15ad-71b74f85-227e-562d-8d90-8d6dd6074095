import os
import sqlite3

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed')),
    request     TEXT NOT NULL,
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_tasks (
    task_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed')),
    request     TEXT NOT NULL,
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create the database file and schema (called on application startup)."""
    path = settings.db_path
    if path != ":memory:":
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn
