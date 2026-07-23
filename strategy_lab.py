"""Compare several established intraday strategy families on unseen data.

This module is deliberately a research tool. It does not change live settings or
place orders. Signals use completed 15-minute bars and enter at the next bar's
open to avoid look-ahead bias.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import autonomous_trader
import bot_config
import intraday_backtester
import paper_bot
import trading_database

STRATEGIES = (
    "trend_pullback",
    "volume_breakout",
    "mean_reversion",
    "opening_range_breakout",
)

DISPLAY_NAMES = {
    "trend_pullback": "Trend pullback",
    "volume_breakout": "Volume breakout",
    "mean_reversion": "Mean reversion",
    "opening_range_breakout": "Opening-range breakout",
}


def _n(bar, key):
    return float(bar.get(key) or 0)


def _rsi(closes, length=14):
    if len(closes) < length + 1:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(len(closes) - length, len(closes)):
        change = closes[index] - closes[index - 1]
        gains += max(change, 0)
        losses += max(-change, 0)
    average_gain = gains / length
    average_loss = losses / length
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _signal(strategy, symbol, history, day_bars):
    if len(history) < 55:
        return None
    closes = [_n(bar, "c") for bar in history]
    highs = [_n(bar, "h") for bar in history]
    lows = [_n(bar, "l") for bar in history]
    volumes = [_n(bar, "v") for bar in history]
    close = closes[-1]
    sma20 = autonomous_trader.sma(closes, 20)
    sma50 = autonomous_trader.sma(closes, 50)
    atr14 = autonomous_trader.atr(history, 14)
    if not sma20 or not sma50 or not atr14 or close <= 0:
        return None

    score = 0.0
    reason = None

    if strategy == "trend_pullback":
        rsi14 = _rsi(closes)
        pullback_depth = (sma20 - close) / atr14
        if sma20 > sma50 and close > sma50 and -0.15 <= pullback_depth <= 0.85 and rsi14 is not None and 35 <= rsi14 <= 52:
            score = (sma20 / sma50 - 1) * 100 + (52 - rsi14) / 20
            reason = "uptrend pullback toward SMA20"

    elif strategy == "volume_breakout":
        prior_high = max(highs[-21:-1])
        average_volume = sum(volumes[-21:-1]) / 20
        volume_ratio = volumes[-1] / average_volume if average_volume else 0
        if sma20 > sma50 and close > prior_high and volume_ratio >= 1.5:
            score = volume_ratio + (close / prior_high - 1) * 100
            reason = "20-bar breakout with elevated volume"

    elif strategy == "mean_reversion":
        rsi14 = _rsi(closes)
        mean = sum(closes[-20:]) / 20
        variance = sum((value - mean) ** 2 for value in closes[-20:]) / 20
        lower_band = mean - 2 * variance ** 0.5
        if sma20 > sma50 and close > sma50 and rsi14 is not None and rsi14 <= 30 and close <= lower_band:
            score = (30 - rsi14) + (lower_band / close - 1) * 100
            reason = "oversold move inside a broader uptrend"

    elif strategy == "opening_range_breakout":
        # The first two available bars define a 30-minute opening range. Only
        # consider breakouts during the following eight bars (about two hours).
        if 3 <= len(day_bars) <= 10:
            opening_high = max(_n(bar, "h") for bar in day_bars[:2])
            average_volume = sum(volumes[-20:]) / 20
            volume_ratio = volumes[-1] / average_volume if average_volume else 0
            if sma20 > sma50 and close > opening_high and volume_ratio >= 1.1:
                score = volume_ratio + (close / opening_high - 1) * 100
                reason = "breakout above the first 30-minute range"

    if reason is None:
        return None
    return {"symbol": symbol, "score": score, "reason": reason, "atr": atr14}


def _close(position, raw_price, timestamp, reason, slippage_bps):
    price = raw_price * (1 - slippage_bps / 10000)
    pnl = (price - position["entry_price"]) * position["quantity"]
    return {
        "symbol": position["symbol"],
        "entry_time": position["entry_time"].isoformat(),
        "exit_time": timestamp.isoformat(),
        "entry_price": position["entry_price"],
        "exit_price": price,
        "quantity": position["quantity"],
        "pnl": pnl,
        "reason": reason,
    }


def simulate(strategy, intraday, daily_spy, symbols, start_time, end_time):
    config = bot_config.get_config(force_reload=True)
    starting_capital = float(bot_config.virtual_capital())
    position_size = bot_config.as_float(bot_config.get_path(config, "autonomous", "position_size", default=10.0), 10.0)
    max_positions = bot_config.as_int(bot_config.get_path(config, "autonomous", "max_positions", default=2), 2)
    max_exposure = bot_config.as_float(bot_config.get_path(config, "autonomous", "max_total_exposure", default=20.0), 20.0)
    max_trades = bot_config.as_int(bot_config.get_path(config, "autonomous", "max_trades_per_day", default=5), 5)
    slippage_bps = bot_config.as_float(bot_config.get_path(config, "autonomous", "simulated_slippage_bps", default=5.0), 5.0)
    stop_mult = 1.5
    target_mult = 2.5
    trail_mult = 1.0
    max_hold_bars = 26

    bars_by_time = {
        symbol: {intraday_backtester._parse_time(bar.get("t")): bar for bar in bars if bar.get("t")}
        for symbol, bars in intraday.items()
    }
    timeline = sorted(ts for ts in bars_by_time.get("SPY", {}) if start_time <= ts <= end_time)
    regime = intraday_backtester._daily_regime_map(daily_spy)
    histories = {symbol: [] for symbol in set(["SPY"] + list(symbols))}
    day_histories = {symbol: [] for symbol in histories}
    current_day = None
    positions = {}
    pending = None
    trades = []
    realized = 0.0
    trades_by_day = defaultdict(int)
    peak = starting_capital
    max_drawdown = 0.0

    for timestamp in timeline:
        day_key = timestamp.date().isoformat()
        if current_day != day_key:
            current_day = day_key
            day_histories = {symbol: [] for symbol in histories}

        if pending:
            symbol = pending["symbol"]
            bar = bars_by_time.get(symbol, {}).get(timestamp)
            exposure = len(positions) * position_size
            if bar and symbol not in positions and len(positions) < max_positions and exposure + position_size <= max_exposure + 1e-9 and trades_by_day[day_key] < max_trades:
                raw_open = _n(bar, "o")
                if raw_open > 0:
                    entry = raw_open * (1 + slippage_bps / 10000)
                    positions[symbol] = {
                        "symbol": symbol,
                        "entry_time": timestamp,
                        "entry_price": entry,
                        "quantity": position_size / entry,
                        "highest": entry,
                        "hold_bars": 0,
                    }
                    trades_by_day[day_key] += 1
            pending = None

        for symbol in list(positions):
            bar = bars_by_time.get(symbol, {}).get(timestamp)
            if not bar:
                continue
            position = positions[symbol]
            position["hold_bars"] += 1
            history = histories[symbol] + [bar]
            atr14 = autonomous_trader.atr(history, 14)
            close = _n(bar, "c")
            high = _n(bar, "h")
            low = _n(bar, "l")
            exit_price = None
            reason = None
            if atr14:
                stop = position["entry_price"] - stop_mult * atr14
                target = position["entry_price"] + target_mult * atr14
                trail = position["highest"] - trail_mult * atr14
                if low <= stop:
                    exit_price, reason = stop, "ATR stop"
                elif high >= target:
                    exit_price, reason = target, "ATR target"
                elif position["highest"] >= position["entry_price"] + atr14 and low <= trail:
                    exit_price, reason = trail, "ATR trailing stop"
            position["highest"] = max(position["highest"], high)
            if exit_price is None and position["hold_bars"] >= max_hold_bars:
                exit_price, reason = close, "maximum hold"
            if exit_price and exit_price > 0:
                trade = _close(position, exit_price, timestamp, reason, slippage_bps)
                trades.append(trade)
                realized += trade["pnl"]
                del positions[symbol]

        for symbol in histories:
            bar = bars_by_time.get(symbol, {}).get(timestamp)
            if bar:
                histories[symbol].append(bar)
                day_histories[symbol].append(bar)

        healthy = intraday_backtester._prior_daily_regime(timestamp, regime)
        if healthy and len(positions) < max_positions:
            candidates = []
            for symbol in symbols:
                if symbol in positions:
                    continue
                candidate = _signal(strategy, symbol, histories[symbol], day_histories[symbol])
                if candidate:
                    candidates.append(candidate)
            candidates.sort(key=lambda item: item["score"], reverse=True)
            pending = candidates[0] if candidates else None

        equity = starting_capital + realized
        for symbol, position in positions.items():
            bar = bars_by_time.get(symbol, {}).get(timestamp)
            if bar:
                equity += (_n(bar, "c") - position["entry_price"]) * position["quantity"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    if timeline:
        final_time = timeline[-1]
        for symbol in list(positions):
            bar = bars_by_time.get(symbol, {}).get(final_time)
            if bar and _n(bar, "c") > 0:
                trade = _close(positions[symbol], _n(bar, "c"), final_time, "end of period", slippage_bps)
                trades.append(trade)
                realized += trade["pnl"]

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    return {
        "strategy": strategy,
        "strategy_name": DISPLAY_NAMES[strategy],
        "start_date": start_time.date().isoformat(),
        "end_date": end_time.date().isoformat(),
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "net_pnl": realized,
        "return_pct": realized / starting_capital * 100 if starting_capital else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0),
        "maximum_drawdown": max_drawdown,
        "maximum_drawdown_pct": max_drawdown / peak * 100 if peak else 0.0,
    }


def run_lab(days=365):
    paper_bot.reload_config()
    days = max(90, min(int(days), 730))
    symbols, intraday, daily_spy = intraday_backtester.load_data(None, days)
    spy_bars = intraday.get("SPY", [])
    timestamps = [intraday_backtester._parse_time(bar.get("t")) for bar in spy_bars if bar.get("t")]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    available = [ts for ts in timestamps if ts >= cutoff]
    if len(available) < 200:
        raise RuntimeError("Not enough 15-minute history for the requested strategy test.")
    split_index = int(len(available) * 0.70)
    train_start, train_end = available[0], available[split_index - 1]
    validation_start, validation_end = available[split_index], available[-1]
    rows = []
    for strategy in STRATEGIES:
        training = simulate(strategy, intraday, daily_spy, symbols, train_start, train_end)
        validation = simulate(strategy, intraday, daily_spy, symbols, validation_start, validation_end)
        passed = (
            validation["net_pnl"] > 0
            and validation["profit_factor"] >= 1.20
            and validation["maximum_drawdown_pct"] <= 10.0
            and validation["total_trades"] >= 20
        )
        rows.append({"strategy": strategy, "name": DISPLAY_NAMES[strategy], "training": training, "validation": validation, "passed": passed})
    rows.sort(key=lambda row: (row["passed"], row["validation"]["profit_factor"], row["validation"]["net_pnl"]), reverse=True)
    result = {
        "days_requested": days,
        "symbols": symbols,
        "train_start": train_start.date().isoformat(),
        "train_end": train_end.date().isoformat(),
        "validation_start": validation_start.date().isoformat(),
        "validation_end": validation_end.date().isoformat(),
        "strategies": rows,
        "missing_symbols": [item for item in symbols if len(intraday.get(item, [])) < 100],
    }
    result["run_id"] = trading_database.save_strategy_lab_result(result)
    return result


def format_result(result):
    if not result:
        return "No saved strategy-lab result yet. Run !strategies test 365 first."
    lines = [
        "AI Paper Trader Strategy Lab",
        f"Run ID: {result.get('run_id', '?')}",
        f"Training: {result.get('train_start')} through {result.get('train_end')}",
        f"Validation: {result.get('validation_start')} through {result.get('validation_end')}",
        "",
        "Unseen validation ranking:",
    ]
    for index, row in enumerate(result.get("strategies", []), 1):
        train = row.get("training", {})
        valid = row.get("validation", {})
        lines.extend([
            f"{index}. {row.get('name')} - {'PASSED' if row.get('passed') else 'FAILED'}",
            f"   Training: {train.get('total_trades', 0)} trades, P/L {paper_bot.money(train.get('net_pnl'))}, PF {train.get('profit_factor', 0):.2f}",
            f"   Validation: {valid.get('total_trades', 0)} trades, {valid.get('win_rate', 0):.1f}% wins, P/L {paper_bot.money(valid.get('net_pnl'))}, PF {valid.get('profit_factor', 0):.2f}, drawdown {valid.get('maximum_drawdown_pct', 0):.2f}%",
        ])
    lines.append("")
    lines.append("Pass rule: positive validation P/L, PF >= 1.20, drawdown <= 10%, and at least 20 validation trades.")
    lines.append("Research only. Live settings are not changed automatically.")
    if result.get("missing_symbols"):
        lines.append("Insufficient history: " + ", ".join(result["missing_symbols"]))
    return "\n".join(lines)
