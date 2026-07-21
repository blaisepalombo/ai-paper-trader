"""Historical daily-bar backtesting for the AI Paper Trader.

The simulator uses only information available before each entry. A signal is
calculated after one daily bar closes and, when qualified, the simulated entry
occurs at the next trading day's open. Results are saved to SQLite.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import autonomous_trader
import bot_config
import paper_bot
import trading_database


def _rfc3339(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bar_date(bar):
    return str(bar.get("t", ""))[:10]


def _number(bar, key):
    return float(bar.get(key) or 0)


def get_daily_bars(symbol, days=365):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(int(days), 90) + 120)
    query = urlencode({
        "timeframe": "1Day",
        "start": _rfc3339(start),
        "end": _rfc3339(end),
        "limit": 10000,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    })
    data = paper_bot.alpaca_data_request("GET", f"/stocks/{symbol}/bars?{query}")
    bars = data.get("bars", [])
    return bars if isinstance(bars, list) else []


def _setting(name, default):
    config = bot_config.get_config(force_reload=True)
    return bot_config.get_path(config, "autonomous", name, default=default)


def _setting_float(name, default):
    return bot_config.as_float(_setting(name, default), default)


def _setting_int(name, default):
    return bot_config.as_int(_setting(name, default), default)


def _watchlist(symbol=None):
    if symbol:
        requested = str(symbol).upper()
        if requested not in paper_bot.ALLOWED_SYMBOLS:
            raise ValueError(f"{requested} is not on the bot allowlist.")
        return [requested]
    configured = _setting("watchlist", bot_config.analyze_watchlist())
    return [
        str(item).upper()
        for item in configured
        if str(item).upper() in paper_bot.ALLOWED_SYMBOLS
    ]


def _max_hold_bars():
    minutes = max(_setting_int("max_hold_minutes", 780), 390)
    return max(1, round(minutes / 390))


def _close_trade(position, exit_price, exit_date, reason, slippage_bps):
    adjusted_exit = exit_price * (1 - slippage_bps / 10000)
    pnl = (adjusted_exit - position["entry_price"]) * position["quantity"]
    return {
        "symbol": position["symbol"],
        "entry_date": position["entry_date"],
        "exit_date": exit_date,
        "entry_price": position["entry_price"],
        "exit_price": adjusted_exit,
        "quantity": position["quantity"],
        "pnl": pnl,
        "return_pct": ((adjusted_exit / position["entry_price"]) - 1) * 100,
        "hold_bars": position["hold_bars"],
        "reason": reason,
        "entry_score": position["entry_score"],
    }


def run_backtest(symbol=None, days=365):
    paper_bot.reload_config()
    symbols = _watchlist(symbol)
    requested_symbols = list(dict.fromkeys(["SPY"] + symbols))
    bars_by_symbol = {item: get_daily_bars(item, days=days) for item in requested_symbols}

    spy_bars = bars_by_symbol.get("SPY", [])
    if len(spy_bars) < 60:
        raise RuntimeError(f"Not enough SPY daily bars. Received {len(spy_bars)}, need at least 60.")

    bars_by_date = {
        item: {_bar_date(bar): bar for bar in bars if _bar_date(bar)}
        for item, bars in bars_by_symbol.items()
    }
    histories = {item: [] for item in requested_symbols}
    dates = sorted(bars_by_date["SPY"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(int(days), 90))).date().isoformat()

    starting_capital = float(bot_config.virtual_capital())
    position_size = _setting_float("position_size", 10.0)
    max_positions = _setting_int("max_positions", 2)
    minimum_score = _setting_int("minimum_entry_score", 6)
    slippage_bps = _setting_float("simulated_slippage_bps", 5.0)
    stop_multiple = _setting_float("stop_atr_multiple", 1.25)
    target_multiple = _setting_float("target_atr_multiple", 2.0)
    trail_multiple = _setting_float("trailing_atr_multiple", 1.0)
    trail_arm_multiple = _setting_float("trail_arm_atr_multiple", 1.0)
    max_hold_bars = _max_hold_bars()

    positions = {}
    pending_candidate = None
    trades = []
    realized = 0.0
    peak_equity = starting_capital
    maximum_drawdown = 0.0
    equity_curve = []
    first_test_date = None
    last_test_date = None

    for date in dates:
        current_spy = bars_by_date["SPY"].get(date)
        if not current_spy:
            continue

        # Enter at today's open using a signal computed after the prior bar closed.
        if pending_candidate and date >= cutoff and len(positions) < max_positions:
            candidate_symbol = pending_candidate["symbol"]
            entry_bar = bars_by_date.get(candidate_symbol, {}).get(date)
            if entry_bar and candidate_symbol not in positions:
                raw_open = _number(entry_bar, "o")
                if raw_open > 0:
                    entry_price = raw_open * (1 + slippage_bps / 10000)
                    quantity = position_size / entry_price
                    positions[candidate_symbol] = {
                        "symbol": candidate_symbol,
                        "entry_date": date,
                        "entry_price": entry_price,
                        "quantity": quantity,
                        "highest_price": entry_price,
                        "hold_bars": 0,
                        "entry_score": pending_candidate["score"],
                    }
                    first_test_date = first_test_date or date
            pending_candidate = None

        # Manage exits using today's OHLC and indicators available through today.
        for held_symbol in list(positions):
            bar = bars_by_date.get(held_symbol, {}).get(date)
            if not bar:
                continue
            position = positions[held_symbol]
            position["hold_bars"] += 1
            high = _number(bar, "h")
            low = _number(bar, "l")
            close = _number(bar, "c")
            history_with_today = histories[held_symbol] + [bar]
            atr_value = autonomous_trader.atr(history_with_today, 14)
            sma20 = autonomous_trader.sma([_number(item, "c") for item in history_with_today], 20)
            exit_price = None
            reason = None

            if atr_value and close > 0:
                stop_price = position["entry_price"] - stop_multiple * atr_value
                target_price = position["entry_price"] + target_multiple * atr_value
                prior_highest = position["highest_price"]
                trail_armed = prior_highest >= position["entry_price"] + trail_arm_multiple * atr_value
                trailing_price = prior_highest - trail_multiple * atr_value

                # Conservative same-day assumption: when both are touched, stop wins.
                if low <= stop_price:
                    exit_price, reason = stop_price, "ATR stop"
                elif high >= target_price:
                    exit_price, reason = target_price, "ATR target"
                elif trail_armed and low <= trailing_price:
                    exit_price, reason = trailing_price, "ATR trailing stop"

            position["highest_price"] = max(position["highest_price"], high)
            if exit_price is None and sma20 and close < sma20:
                exit_price, reason = close, "trend reversal below SMA20"
            if exit_price is None and position["hold_bars"] >= max_hold_bars:
                exit_price, reason = close, f"maximum hold of {max_hold_bars} daily bars"

            if exit_price is not None and exit_price > 0:
                closed = _close_trade(position, exit_price, date, reason, slippage_bps)
                trades.append(closed)
                realized += closed["pnl"]
                del positions[held_symbol]
                last_test_date = date

        # Add today's bars to history only after today's entry/exit simulation.
        for item in requested_symbols:
            bar = bars_by_date.get(item, {}).get(date)
            if bar:
                histories[item].append(bar)

        # Build the next-day signal using history through today's close.
        if date >= cutoff and len(positions) < max_positions and len(histories["SPY"]) >= 55:
            spy_closes = [_number(item, "c") for item in histories["SPY"]]
            spy_sma20 = autonomous_trader.sma(spy_closes, 20)
            spy_sma50 = autonomous_trader.sma(spy_closes, 50)
            market_healthy = bool(
                spy_sma20 and spy_sma50
                and spy_closes[-1] > spy_sma20
                and spy_sma20 > spy_sma50
            )
            if market_healthy:
                spy_return20 = spy_closes[-1] / spy_closes[-21] - 1
                candidates = []
                for item in symbols:
                    if item in positions or len(histories.get(item, [])) < 55:
                        continue
                    result = autonomous_trader.analyze_bars(item, histories[item], spy_return20)
                    if result and result["score"] >= minimum_score:
                        candidates.append(result)
                candidates.sort(
                    key=lambda result: (result["score"], result["return_20"], result["return_5"]),
                    reverse=True,
                )
                pending_candidate = candidates[0] if candidates else None

        marked_equity = starting_capital + realized
        for held_symbol, position in positions.items():
            bar = bars_by_date.get(held_symbol, {}).get(date)
            if bar:
                marked_equity += (_number(bar, "c") - position["entry_price"]) * position["quantity"]
        peak_equity = max(peak_equity, marked_equity)
        drawdown = peak_equity - marked_equity
        maximum_drawdown = max(maximum_drawdown, drawdown)
        if date >= cutoff:
            equity_curve.append({"date": date, "equity": marked_equity})

    # Close any remaining positions at the last available close.
    for held_symbol in list(positions):
        available = [date for date in dates if date in bars_by_date.get(held_symbol, {})]
        if not available:
            continue
        exit_date = available[-1]
        exit_price = _number(bars_by_date[held_symbol][exit_date], "c")
        closed = _close_trade(positions[held_symbol], exit_price, exit_date, "end of backtest", slippage_bps)
        trades.append(closed)
        realized += closed["pnl"]
        last_test_date = exit_date

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (gross_profit if gross_profit else 0.0)

    test_spy = [bar for bar in spy_bars if _bar_date(bar) >= cutoff]
    buy_hold_pnl = 0.0
    if len(test_spy) >= 2 and _number(test_spy[0], "o") > 0:
        buy_hold_return = _number(test_spy[-1], "c") / _number(test_spy[0], "o") - 1
        buy_hold_pnl = starting_capital * buy_hold_return

    result = {
        "requested_symbol": str(symbol).upper() if symbol else None,
        "symbols": symbols,
        "days_requested": int(days),
        "start_date": first_test_date or cutoff,
        "end_date": last_test_date or (dates[-1] if dates else None),
        "starting_capital": starting_capital,
        "ending_equity": starting_capital + realized,
        "net_pnl": realized,
        "return_pct": (realized / starting_capital * 100) if starting_capital else 0.0,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "average_win": (gross_profit / len(wins)) if wins else 0.0,
        "average_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": profit_factor,
        "maximum_drawdown": maximum_drawdown,
        "maximum_drawdown_pct": (maximum_drawdown / peak_equity * 100) if peak_equity else 0.0,
        "spy_buy_hold_pnl": buy_hold_pnl,
        "slippage_bps": slippage_bps,
        "timeframe": "1Day",
        "trade_details": trades,
        "equity_curve": equity_curve,
        "missing_symbols": [item for item in symbols if len(bars_by_symbol.get(item, [])) < 55],
    }
    result["run_id"] = trading_database.save_backtest_result(result)
    return result


def format_result(result):
    if not result:
        return "No saved backtest results yet. Run !backtest first."
    scope = result.get("requested_symbol") or "configured watchlist"
    profit_factor = float(result.get("profit_factor") or 0)
    lines = [
        "AI Paper Trader Daily Backtest",
        f"Run ID: {result.get('run_id', '?')}",
        f"Scope: {scope}",
        f"Period: {result.get('start_date')} through {result.get('end_date')}",
        f"Timeframe: {result.get('timeframe', '1Day')}",
        f"Trades: {int(result.get('total_trades') or 0)}",
        f"Wins / losses: {int(result.get('wins') or 0)} / {int(result.get('losses') or 0)}",
        f"Win rate: {float(result.get('win_rate') or 0):.1f}%",
        f"Net P/L: {paper_bot.money(result.get('net_pnl'))}",
        f"Return on {paper_bot.money(result.get('starting_capital'))}: {float(result.get('return_pct') or 0):.2f}%",
        f"Average win: {paper_bot.money(result.get('average_win'))}",
        f"Average loss: {paper_bot.money(result.get('average_loss'))}",
        f"Profit factor: {profit_factor:.2f}",
        f"Maximum drawdown: {paper_bot.money(result.get('maximum_drawdown'))} ({float(result.get('maximum_drawdown_pct') or 0):.2f}%)",
        f"SPY buy-and-hold P/L on same starting capital: {paper_bot.money(result.get('spy_buy_hold_pnl'))}",
        f"Simulated slippage: {float(result.get('slippage_bps') or 0):.1f} bps per side",
    ]
    missing = result.get("missing_symbols") or []
    if missing:
        lines.append("Skipped for insufficient data: " + ", ".join(missing))
    lines.append("Signals use completed bars; entries occur at the next day's open.")
    return "\n".join(lines)
