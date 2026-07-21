"""15-minute historical backtesting that mirrors the live autonomous strategy.

Signals are calculated from completed 15-minute bars. Entries occur at the next
15-minute bar open, avoiding look-ahead bias. The SPY regime uses only the most
recent completed daily bar available before the intraday timestamp.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import autonomous_trader
import bot_config
import paper_bot
import trading_database


def _rfc3339(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value):
    text = str(value or "").replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _number(bar, key):
    return float(bar.get(key) or 0)


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
    return list(dict.fromkeys(
        str(item).upper() for item in configured
        if str(item).upper() in paper_bot.ALLOWED_SYMBOLS
    ))


def _fetch_bars_chunk(symbol, timeframe, start, end, limit=10000):
    query = urlencode({
        "timeframe": timeframe,
        "start": _rfc3339(start),
        "end": _rfc3339(end),
        "limit": limit,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    })
    data = paper_bot.alpaca_data_request("GET", f"/stocks/{symbol}/bars?{query}")
    bars = data.get("bars", [])
    return bars if isinstance(bars, list) else []


def get_intraday_bars(symbol, days=180):
    """Fetch 15-minute bars in 28-day chunks to avoid page limits."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(30, int(days)) + 10)
    bars = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=28), end)
        bars.extend(_fetch_bars_chunk(symbol, "15Min", cursor, chunk_end))
        cursor = chunk_end
    unique = {str(bar.get("t")): bar for bar in bars if bar.get("t")}
    return [unique[key] for key in sorted(unique)]


def get_daily_bars(symbol="SPY", days=180):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(90, int(days)) + 120)
    return _fetch_bars_chunk(symbol, "1Day", start, end)




def _analyze_bars(symbol, bars, spy_return_20, min_atr_pct, max_atr_pct):
    closes = [_number(bar, "c") for bar in bars]
    volumes = [_number(bar, "v") for bar in bars]
    if len(closes) < 55 or any(value <= 0 for value in closes[-55:]):
        return None
    latest = closes[-1]
    sma20 = autonomous_trader.sma(closes, 20)
    sma50 = autonomous_trader.sma(closes, 50)
    return_5 = latest / closes[-6] - 1
    return_20 = latest / closes[-21] - 1
    average_volume20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    recent_high = max(closes[-20:])
    atr14 = autonomous_trader.atr(bars, 14)
    atr_pct = (atr14 / latest) * 100 if atr14 and latest else 0
    score = 0
    if latest > sma20:
        score += 1
    if sma20 > sma50:
        score += 1
    if return_5 > 0:
        score += 1
    if return_20 > 0:
        score += 1
    if symbol == "SPY" or return_20 > spy_return_20:
        score += 1
    if average_volume20 > 0 and volumes[-1] >= average_volume20 * 0.8:
        score += 1
    if latest >= recent_high * 0.985:
        score += 1
    if min_atr_pct <= atr_pct <= max_atr_pct:
        score += 1
    return {
        "symbol": symbol,
        "score": score,
        "latest": latest,
        "return_5": return_5,
        "return_20": return_20,
        "atr": atr14,
        "atr_pct": atr_pct,
    }


def _current_parameters():
    return {
        "minimum_entry_score": _setting_int("minimum_entry_score", 6),
        "stop_atr_multiple": _setting_float("stop_atr_multiple", 1.25),
        "target_atr_multiple": _setting_float("target_atr_multiple", 2.0),
        "trailing_atr_multiple": _setting_float("trailing_atr_multiple", 1.0),
        "trail_arm_atr_multiple": _setting_float("trail_arm_atr_multiple", 1.0),
        "max_hold_minutes": _setting_int("max_hold_minutes", 780),
        "cooldown_seconds": _setting_int("cooldown_seconds", 1800),
        "max_trades_per_day": _setting_int("max_trades_per_day", 5),
    }


def load_data(symbol=None, days=180):
    symbols = _watchlist(symbol)
    requested = list(dict.fromkeys(["SPY"] + symbols))
    intraday = {item: get_intraday_bars(item, days) for item in requested}
    daily_spy = get_daily_bars("SPY", days)
    return symbols, intraday, daily_spy


def _daily_regime_map(daily_bars):
    closes = []
    result = {}
    for bar in daily_bars:
        date = str(bar.get("t", ""))[:10]
        close = _number(bar, "c")
        if not date or close <= 0:
            continue
        closes.append(close)
        healthy = False
        if len(closes) >= 50:
            sma20 = sum(closes[-20:]) / 20
            sma50 = sum(closes[-50:]) / 50
            healthy = close > sma20 and sma20 > sma50
        result[date] = healthy
    return result


def _prior_daily_regime(timestamp, regime_map):
    current_date = timestamp.date()
    for offset in range(1, 8):
        key = (current_date - timedelta(days=offset)).isoformat()
        if key in regime_map:
            return regime_map[key]
    return False


def _close_trade(position, raw_exit, timestamp, reason, slippage_bps):
    exit_price = raw_exit * (1 - slippage_bps / 10000)
    pnl = (exit_price - position["entry_price"]) * position["quantity"]
    return {
        "symbol": position["symbol"],
        "entry_time": position["entry_time"].isoformat(),
        "exit_time": timestamp.isoformat(),
        "entry_price": position["entry_price"],
        "exit_price": exit_price,
        "quantity": position["quantity"],
        "pnl": pnl,
        "return_pct": ((exit_price / position["entry_price"]) - 1) * 100,
        "hold_bars": position["hold_bars"],
        "reason": reason,
        "entry_score": position["entry_score"],
    }


def simulate(intraday, daily_spy, symbols, start_time, end_time, parameters):
    config = bot_config.get_config(force_reload=True)
    starting_capital = float(bot_config.virtual_capital())
    position_size = bot_config.as_float(bot_config.get_path(config, "autonomous", "position_size", default=10.0), 10.0, minimum=0.01)
    max_positions = bot_config.as_int(bot_config.get_path(config, "autonomous", "max_positions", default=2), 2, minimum=1)
    max_exposure = bot_config.as_float(bot_config.get_path(config, "autonomous", "max_total_exposure", default=20.0), 20.0, minimum=1)
    daily_loss_limit = bot_config.as_float(bot_config.get_path(config, "autonomous", "daily_loss_limit", default=5.0), 5.0, minimum=0.01)
    slippage_bps = bot_config.as_float(bot_config.get_path(config, "autonomous", "simulated_slippage_bps", default=5.0), 5.0, minimum=0)
    min_atr_pct = bot_config.as_float(bot_config.get_path(config, "autonomous", "min_atr_pct", default=0.10), 0.10)
    max_atr_pct = bot_config.as_float(bot_config.get_path(config, "autonomous", "max_atr_pct", default=5.0), 5.0)

    bars_by_time = {
        symbol: {_parse_time(bar.get("t")): bar for bar in bars if bar.get("t")}
        for symbol, bars in intraday.items()
    }
    timeline = sorted(timestamp for timestamp in bars_by_time.get("SPY", {}) if timestamp <= end_time)
    regime_map = _daily_regime_map(daily_spy)
    histories = {symbol: [] for symbol in set(["SPY"] + list(symbols))}
    positions = {}
    pending = None
    last_trade_time = None
    trades = []
    realized = 0.0
    realized_by_day = defaultdict(float)
    trades_by_day = defaultdict(int)
    peak_equity = starting_capital
    maximum_drawdown = 0.0

    cooldown_seconds = int(parameters["cooldown_seconds"])
    max_hold_bars = max(1, int(round(parameters["max_hold_minutes"] / 15.0)))

    for timestamp in timeline:
        day_key = timestamp.date().isoformat()

        # Enter at this bar's open from the prior completed-bar signal.
        if pending and timestamp >= start_time:
            symbol = pending["symbol"]
            bar = bars_by_time.get(symbol, {}).get(timestamp)
            exposure = sum(position_size for _ in positions)
            cooldown_ok = last_trade_time is None or (timestamp - last_trade_time).total_seconds() >= cooldown_seconds
            risk_ok = realized_by_day[day_key] > -daily_loss_limit
            limits_ok = (
                len(positions) < max_positions
                and exposure + position_size <= max_exposure + 1e-9
                and trades_by_day[day_key] < parameters["max_trades_per_day"]
            )
            if bar and symbol not in positions and cooldown_ok and risk_ok and limits_ok:
                raw_open = _number(bar, "o")
                if raw_open > 0:
                    entry_price = raw_open * (1 + slippage_bps / 10000)
                    positions[symbol] = {
                        "symbol": symbol,
                        "entry_time": timestamp,
                        "entry_price": entry_price,
                        "quantity": position_size / entry_price,
                        "highest_price": entry_price,
                        "hold_bars": 0,
                        "entry_score": pending["score"],
                    }
                    trades_by_day[day_key] += 1
                    last_trade_time = timestamp
            pending = None

        # Exit management uses the current completed bar.
        for symbol in list(positions):
            bar = bars_by_time.get(symbol, {}).get(timestamp)
            if not bar:
                continue
            position = positions[symbol]
            position["hold_bars"] += 1
            high = _number(bar, "h")
            low = _number(bar, "l")
            close = _number(bar, "c")
            history = histories[symbol] + [bar]
            atr_value = autonomous_trader.atr(history, 14)
            sma20 = autonomous_trader.sma([_number(item, "c") for item in history], 20)
            exit_price = None
            reason = None

            if atr_value and close > 0:
                stop_price = position["entry_price"] - parameters["stop_atr_multiple"] * atr_value
                target_price = position["entry_price"] + parameters["target_atr_multiple"] * atr_value
                prior_high = position["highest_price"]
                trail_armed = prior_high >= position["entry_price"] + parameters["trail_arm_atr_multiple"] * atr_value
                trailing_price = prior_high - parameters["trailing_atr_multiple"] * atr_value
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
                exit_price, reason = close, "maximum hold"

            if exit_price is not None and exit_price > 0:
                trade = _close_trade(position, exit_price, timestamp, reason, slippage_bps)
                trades.append(trade)
                realized += trade["pnl"]
                realized_by_day[day_key] += trade["pnl"]
                del positions[symbol]
                last_trade_time = timestamp

        # Add this completed bar to each history.
        for symbol in histories:
            bar = bars_by_time.get(symbol, {}).get(timestamp)
            if bar:
                histories[symbol].append(bar)

        # Generate a next-bar entry signal.
        healthy = _prior_daily_regime(timestamp, regime_map)
        if timestamp >= start_time and healthy and len(histories.get("SPY", [])) >= 55 and len(positions) < max_positions:
            spy_closes = [_number(item, "c") for item in histories["SPY"]]
            spy_return20 = spy_closes[-1] / spy_closes[-21] - 1
            candidates = []
            for symbol in symbols:
                if symbol in positions or len(histories.get(symbol, [])) < 55:
                    continue
                result = _analyze_bars(symbol, histories[symbol], spy_return20, min_atr_pct, max_atr_pct)
                if result and result["score"] >= parameters["minimum_entry_score"]:
                    candidates.append(result)
            candidates.sort(key=lambda item: (item["score"], item["return_20"], item["return_5"]), reverse=True)
            pending = candidates[0] if candidates else None
        else:
            pending = None

        if timestamp >= start_time:
            equity = starting_capital + realized
            for symbol, position in positions.items():
                bar = bars_by_time.get(symbol, {}).get(timestamp)
                if bar:
                    equity += (_number(bar, "c") - position["entry_price"]) * position["quantity"]
            peak_equity = max(peak_equity, equity)
            maximum_drawdown = max(maximum_drawdown, peak_equity - equity)

    if timeline:
        final_time = timeline[-1]
        for symbol in list(positions):
            bar = bars_by_time.get(symbol, {}).get(final_time)
            if bar and _number(bar, "c") > 0:
                trade = _close_trade(positions[symbol], _number(bar, "c"), final_time, "end of period", slippage_bps)
                trades.append(trade)
                realized += trade["pnl"]

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0)
    average_win = gross_profit / len(wins) if wins else 0.0
    average_loss = -gross_loss / len(losses) if losses else 0.0
    ending_equity = starting_capital + realized
    return {
        "start_date": start_time.date().isoformat(),
        "end_date": end_time.date().isoformat(),
        "starting_capital": starting_capital,
        "ending_equity": ending_equity,
        "net_pnl": realized,
        "return_pct": realized / starting_capital * 100 if starting_capital else 0.0,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "average_win": average_win,
        "average_loss": average_loss,
        "profit_factor": profit_factor,
        "maximum_drawdown": maximum_drawdown,
        "maximum_drawdown_pct": maximum_drawdown / peak_equity * 100 if peak_equity else 0.0,
        "trades": trades,
    }


def run_backtest(symbol=None, days=180):
    paper_bot.reload_config()
    days = max(30, min(int(days), 730))
    symbols, intraday, daily_spy = load_data(symbol, days)
    spy_bars = intraday.get("SPY", [])
    if len(spy_bars) < 100:
        raise RuntimeError(f"Not enough SPY 15-minute bars. Received {len(spy_bars)}.")
    timestamps = [_parse_time(bar.get("t")) for bar in spy_bars if bar.get("t")]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    test_times = [value for value in timestamps if value >= cutoff]
    if len(test_times) < 100:
        raise RuntimeError("Not enough intraday bars in the requested period.")
    result = simulate(intraday, daily_spy, symbols, test_times[0], test_times[-1], _current_parameters())
    result.update({
        "requested_symbol": symbol,
        "symbols": symbols,
        "days_requested": days,
        "timeframe": "15Min",
        "slippage_bps": _setting_float("simulated_slippage_bps", 5.0),
        "missing_symbols": [item for item in symbols if len(intraday.get(item, [])) < 100],
    })
    result["run_id"] = trading_database.save_intraday_backtest_result(result)
    return result


def format_result(result):
    if not result:
        return "No saved intraday backtest results yet. Run !backtest intraday first."
    lines = [
        "AI Paper Trader Intraday Backtest",
        f"Run ID: {result.get('run_id', '?')}",
        f"Scope: {result.get('requested_symbol') or 'configured watchlist'}",
        f"Period: {result.get('start_date')} through {result.get('end_date')}",
        "Timeframe: 15Min",
        f"Trades: {result.get('total_trades', 0)}",
        f"Wins / losses: {result.get('wins', 0)} / {result.get('losses', 0)}",
        f"Win rate: {result.get('win_rate', 0):.1f}%",
        f"Net P/L: {paper_bot.money(result.get('net_pnl'))}",
        f"Return on {paper_bot.money(result.get('starting_capital'))}: {result.get('return_pct', 0):.2f}%",
        f"Average win: {paper_bot.money(result.get('average_win'))}",
        f"Average loss: {paper_bot.money(result.get('average_loss'))}",
        f"Profit factor: {result.get('profit_factor', 0):.2f}",
        f"Maximum drawdown: {paper_bot.money(result.get('maximum_drawdown'))} ({result.get('maximum_drawdown_pct', 0):.2f}%)",
        f"Simulated slippage: {result.get('slippage_bps', 0):.1f} bps per side",
        "Signals use completed 15-minute bars; entries occur at the next bar open.",
    ]
    if result.get("missing_symbols"):
        lines.append("Insufficient history: " + ", ".join(result["missing_symbols"]))
    return "\n".join(lines)
