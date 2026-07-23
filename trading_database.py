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

            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                requested_symbol TEXT,
                symbols_json TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                days_requested INTEGER NOT NULL,
                start_date TEXT,
                end_date TEXT,
                starting_capital REAL,
                ending_equity REAL,
                net_pnl REAL,
                return_pct REAL,
                total_trades INTEGER,
                wins INTEGER,
                losses INTEGER,
                win_rate REAL,
                average_win REAL,
                average_loss REAL,
                profit_factor REAL,
                maximum_drawdown REAL,
                maximum_drawdown_pct REAL,
                spy_buy_hold_pnl REAL,
                slippage_bps REAL,
                details_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_backtest_runs_time
                ON backtest_runs(timestamp_utc);

            CREATE TABLE IF NOT EXISTS optimizer_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                days_requested INTEGER NOT NULL,
                combinations_tested INTEGER NOT NULL,
                train_start TEXT,
                train_end TEXT,
                validation_start TEXT,
                validation_end TEXT,
                best_parameters_json TEXT,
                training_net_pnl REAL,
                training_profit_factor REAL,
                validation_net_pnl REAL,
                validation_profit_factor REAL,
                validation_passed INTEGER NOT NULL DEFAULT 0,
                details_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_optimizer_runs_time
                ON optimizer_runs(timestamp_utc);

            CREATE TABLE IF NOT EXISTS intraday_backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                days_requested INTEGER NOT NULL,
                requested_symbol TEXT,
                net_pnl REAL,
                profit_factor REAL,
                maximum_drawdown_pct REAL,
                details_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS intraday_optimizer_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                days_requested INTEGER NOT NULL,
                combinations_tested INTEGER NOT NULL,
                validation_passed INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_lab_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                days_requested INTEGER NOT NULL,
                details_json TEXT NOT NULL
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


def save_backtest_result(result):
    initialize()
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO backtest_runs (
                timestamp_utc, requested_symbol, symbols_json, timeframe,
                days_requested, start_date, end_date, starting_capital,
                ending_equity, net_pnl, return_pct, total_trades, wins, losses,
                win_rate, average_win, average_loss, profit_factor,
                maximum_drawdown, maximum_drawdown_pct, spy_buy_hold_pnl,
                slippage_bps, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_utc(), result.get("requested_symbol"),
                _json(result.get("symbols") or []), result.get("timeframe", "1Day"),
                int(result.get("days_requested") or 365), result.get("start_date"),
                result.get("end_date"), result.get("starting_capital"),
                result.get("ending_equity"), result.get("net_pnl"),
                result.get("return_pct"), result.get("total_trades"),
                result.get("wins"), result.get("losses"), result.get("win_rate"),
                result.get("average_win"), result.get("average_loss"),
                result.get("profit_factor"), result.get("maximum_drawdown"),
                result.get("maximum_drawdown_pct"), result.get("spy_buy_hold_pnl"),
                result.get("slippage_bps"), _json(result),
            ),
        )
        return cursor.lastrowid


def latest_backtest_result():
    initialize()
    with connection() as conn:
        row = conn.execute(
            "SELECT id, details_json FROM backtest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        return None
    result["run_id"] = row["id"]
    return result


def save_optimizer_result(result):
    initialize()
    best = result.get("best") or {}
    training = best.get("training") or {}
    validation = best.get("validation") or {}
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO optimizer_runs (
                timestamp_utc, days_requested, combinations_tested, train_start,
                train_end, validation_start, validation_end, best_parameters_json,
                training_net_pnl, training_profit_factor, validation_net_pnl,
                validation_profit_factor, validation_passed, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_utc(), int(result.get("days_requested") or 730),
                int(result.get("combinations_tested") or 0), result.get("train_start"),
                result.get("train_end"), result.get("validation_start"),
                result.get("validation_end"), _json(best.get("parameters") or {}),
                training.get("net_pnl"), training.get("profit_factor"),
                validation.get("net_pnl"), validation.get("profit_factor"),
                int(bool(best.get("validation_passed"))), _json(result),
            ),
        )
        return cursor.lastrowid


def latest_optimizer_result():
    initialize()
    with connection() as conn:
        row = conn.execute(
            "SELECT id, details_json FROM optimizer_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        return None
    result["run_id"] = row["id"]
    return result


def save_intraday_backtest_result(result):
    initialize()
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO intraday_backtest_runs (
                timestamp_utc, days_requested, requested_symbol, net_pnl,
                profit_factor, maximum_drawdown_pct, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_utc(), int(result.get("days_requested") or 180),
                result.get("requested_symbol"), result.get("net_pnl"),
                result.get("profit_factor"), result.get("maximum_drawdown_pct"),
                _json(result),
            ),
        )
        return cursor.lastrowid


def latest_intraday_backtest_result():
    initialize()
    with connection() as conn:
        row = conn.execute(
            "SELECT id, details_json FROM intraday_backtest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        return None
    result["run_id"] = row["id"]
    return result


def save_intraday_optimizer_result(result):
    initialize()
    best = result.get("best") or {}
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO intraday_optimizer_runs (
                timestamp_utc, days_requested, combinations_tested,
                validation_passed, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                now_utc(), int(result.get("days_requested") or 180),
                int(result.get("combinations_tested") or 0),
                int(bool(best.get("validation_passed"))), _json(result),
            ),
        )
        return cursor.lastrowid


def latest_intraday_optimizer_result():
    initialize()
    with connection() as conn:
        row = conn.execute(
            "SELECT id, details_json FROM intraday_optimizer_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        return None
    result["run_id"] = row["id"]
    return result


def save_strategy_lab_result(result):
    initialize()
    with connection() as conn:
        cursor = conn.execute(
            "INSERT INTO strategy_lab_runs (timestamp_utc, days_requested, details_json) VALUES (?, ?, ?)",
            (now_utc(), int(result.get("days_requested") or 365), _json(result)),
        )
        return cursor.lastrowid


def latest_strategy_lab_result():
    initialize()
    with connection() as conn:
        row = conn.execute(
            "SELECT id, details_json FROM strategy_lab_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        return None
    result["run_id"] = row["id"]
    return result
