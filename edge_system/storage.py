"""SQLite storage used only by the evidence-strategy subsystem."""

import json
import sqlite3
from contextlib import contextmanager

import trading_database


@contextmanager
def connection():
    path = trading_database.database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize():
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS edge_strategy_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                market TEXT NOT NULL,
                days_requested INTEGER NOT NULL,
                validation_passed INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_edge_strategy_runs_market_time
                ON edge_strategy_runs(market, timestamp_utc);
            CREATE TABLE IF NOT EXISTS edge_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                market TEXT NOT NULL,
                target_symbol TEXT NOT NULL,
                reason TEXT,
                details_json TEXT
            );
            """
        )


def _json(value):
    return json.dumps(value, default=str, sort_keys=True)


def save_strategy_result(result):
    initialize()
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO edge_strategy_runs (
                timestamp_utc, market, days_requested, validation_passed, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                trading_database.now_utc(), str(result.get("market") or "unknown").lower(),
                int(result.get("days_requested") or 1825), int(bool(result.get("passed"))),
                _json(result),
            ),
        )
        return cursor.lastrowid


def latest_strategy_result(market):
    initialize()
    with connection() as conn:
        row = conn.execute(
            "SELECT id, details_json FROM edge_strategy_runs WHERE market=? ORDER BY id DESC LIMIT 1",
            (str(market).lower(),),
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["details_json"] or "{}")
    except (TypeError, ValueError):
        return None
    result["run_id"] = row["id"]
    return result


def log_decision(market, target_symbol, reason, details=None):
    initialize()
    with connection() as conn:
        conn.execute(
            "INSERT INTO edge_decisions (timestamp_utc, market, target_symbol, reason, details_json) VALUES (?, ?, ?, ?, ?)",
            (
                trading_database.now_utc(), str(market).lower(),
                str(target_symbol or "CASH").upper(), reason, _json(details or {}),
            ),
        )
