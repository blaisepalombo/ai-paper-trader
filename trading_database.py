"""Durable SQLite logging for the AI Paper Trader.

The database is deliberately stored outside the Git repository by default so
code deployments cannot erase trading history. Set TRADING_DB_PATH to override
its location.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def database_path():
    configured = os.environ.get("TRADING_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "ai-paper-trader-data" / "trading_bot.db"


@contextmanager
def connection():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize():
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'autonomous',
                symbol TEXT NOT NULL,
                price REAL,
                score INTEGER,
                maximum_score INTEGER NOT NULL DEFAULT 8,
                sma20 REAL,
                sma50 REAL,
                return_5 REAL,
                return_20 REAL,
                atr REAL,
                atr_pct REAL,
                market_healthy INTEGER,
                action TEXT NOT NULL,
                reason TEXT,
                details_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_time
                ON decisions(timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_decisions_symbol
                ON decisions(symbol);
            CREATE INDEX IF NOT EXISTS idx_decisions_action
                ON decisions(action);

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                dollars REAL,
                quantity REAL,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                status TEXT,
                reason TEXT,
                order_id TEXT,
                source TEXT NOT NULL DEFAULT 'bot',
                details_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trades_time
                ON trades(timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol
                ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_side
                ON trades(side);

            CREATE TABLE IF NOT EXISTS daily_stats (
                trade_date TEXT PRIMARY KEY,
                timestamp_utc TEXT NOT NULL,
                starting_equity REAL,
                ending_equity REAL,
                daily_pnl REAL,
                realized_pnl REAL,
                unrealized_pnl REAL,
                trades_opened INTEGER NOT NULL DEFAULT 0,
                trades_closed INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                win_rate REAL NOT NULL DEFAULT 0,
                open_positions INTEGER NOT NULL DEFAULT 0,
                maximum_drawdown REAL,
                details_json TEXT
            );
            """
        )
    return database_path()


def _json(value):
    return json.dumps(value, default=str, sort_keys=True) if value is not None else None


def log_decision(result, action, reason, market_healthy=None, source="autonomous"):
    initialize()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO decisions (
                timestamp_utc, source, symbol, price, score, maximum_score,
                sma20, sma50, return_5, return_20, atr, atr_pct,
                market_healthy, action, reason, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_utc(), source, str(result.get("symbol", "")).upper(),
                result.get("latest"), result.get("score"), 8,
                result.get("sma20"), result.get("sma50"),
                result.get("return_5"), result.get("return_20"),
                result.get("atr"), result.get("atr_pct"),
                None if market_healthy is None else int(bool(market_healthy)),
                action, reason, _json(result),
            ),
        )


def log_trade(symbol, side, status=None, dollars=None, quantity=None,
              entry_price=None, exit_price=None, pnl=None, reason=None,
              order_id=None, source="bot", details=None):
    initialize()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO trades (
                timestamp_utc, symbol, side, dollars, quantity, entry_price,
                exit_price, pnl, status, reason, order_id, source, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_utc(), str(symbol).upper(), side, dollars, quantity,
                entry_price, exit_price, pnl, status, reason, order_id,
                source, _json(details),
            ),
        )


def upsert_daily_stats(account, positions, state):
    initialize()
    today = datetime.now(timezone.utc).date().isoformat()
    equity = float(account.get("equity") or 0)
    last_equity = float(account.get("last_equity") or equity)
    daily_pnl = equity - last_equity
    unrealized = sum(float(position.get("unrealized_pl") or 0) for position in positions)
    closed = [
        trade for trade in state.get("closed_trades", [])
        if str(trade.get("time", "")).startswith(today)
    ]
    wins = sum(1 for trade in closed if float(trade.get("estimated_pl") or 0) > 0)
    losses = sum(1 for trade in closed if float(trade.get("estimated_pl") or 0) < 0)
    win_rate = wins / len(closed) * 100 if closed else 0.0

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO daily_stats (
                trade_date, timestamp_utc, starting_equity, ending_equity,
                daily_pnl, realized_pnl, unrealized_pnl, trades_opened,
                trades_closed, wins, losses, win_rate, open_positions,
                maximum_drawdown, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                timestamp_utc=excluded.timestamp_utc,
                ending_equity=excluded.ending_equity,
                daily_pnl=excluded.daily_pnl,
                realized_pnl=excluded.realized_pnl,
                unrealized_pnl=excluded.unrealized_pnl,
                trades_opened=excluded.trades_opened,
                trades_closed=excluded.trades_closed,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                open_positions=excluded.open_positions,
                details_json=excluded.details_json
            """,
            (
                today, now_utc(), last_equity, equity, daily_pnl,
                float(state.get("realized_pl_today") or 0), unrealized,
                int(state.get("trades_today") or 0), len(closed), wins, losses,
                win_rate, len(positions), None,
                _json({"account": account, "state_date": state.get("date")}),
            ),
        )


def stats_summary(days=30):
    initialize()
    with connection() as conn:
        trade_row = conn.execute(
            """
            SELECT
                COUNT(*) AS trade_events,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
                COALESCE(SUM(pnl), 0) AS net_pnl,
                COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) AS avg_win,
                COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl END), 0) AS avg_loss
            FROM trades
            WHERE julianday(timestamp_utc) >= julianday('now', ?)
            """,
            (f"-{int(days)} days",),
        ).fetchone()
        decision_row = conn.execute(
            """
            SELECT COUNT(*) AS decisions,
                   SUM(CASE WHEN action='BUY' THEN 1 ELSE 0 END) AS buy_decisions,
                   SUM(CASE WHEN action='PASS' THEN 1 ELSE 0 END) AS passes
            FROM decisions
            WHERE julianday(timestamp_utc) >= julianday('now', ?)
            """,
            (f"-{int(days)} days",),
        ).fetchone()
    return dict(trade_row), dict(decision_row)
