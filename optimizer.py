"""Controlled parameter optimization for the daily AI Paper Trader backtest.

The optimizer downloads historical bars once, tests a modest grid of strategy
settings, ranks configurations on an earlier training period, then reports how
the best training candidates performed on a later unseen validation period.
It never changes the live bot configuration automatically.
"""

from datetime import datetime, timedelta, timezone
from itertools import product

import autonomous_trader
import backtester
import bot_config
import paper_bot
import trading_database


def _bar_date(bar):
    return str(bar.get("t", ""))[:10]


def _number(bar, key):
    return float(bar.get(key) or 0)


def _current_parameters():
    config = bot_config.get_config(force_reload=True)

    def value(name, default):
        return bot_config.get_path(config, "autonomous", name, default=default)

    max_hold_minutes = bot_config.as_int(value("max_hold_minutes", 780), 780, minimum=390)
    return {
        "minimum_entry_score": bot_config.as_int(value("minimum_entry_score", 6), 6, minimum=1, maximum=8),
        "stop_atr_multiple": bot_config.as_float(value("stop_atr_multiple", 1.25), 1.25, minimum=0.1),
        "target_atr_multiple": bot_config.as_float(value("target_atr_multiple", 2.0), 2.0, minimum=0.1),
        "trailing_atr_multiple": bot_config.as_float(value("trailing_atr_multiple", 1.0), 1.0, minimum=0.1),
        "trail_arm_atr_multiple": bot_config.as_float(value("trail_arm_atr_multiple", 1.0), 1.0, minimum=0.1),
        "max_hold_bars": max(1, round(max_hold_minutes / 390)),
    }


def _parameter_grid():
    """A deliberately modest 243-combination grid for the Oracle VM."""
    current = _current_parameters()
    return [
        {
            "minimum_entry_score": score,
            "stop_atr_multiple": stop,
            "target_atr_multiple": target,
            "trailing_atr_multiple": trail,
            "trail_arm_atr_multiple": current["trail_arm_atr_multiple"],
            "max_hold_bars": hold,
        }
        for score, stop, target, trail, hold in product(
            [5, 6, 7],
            [0.75, 1.25, 1.75],
            [1.5, 2.0, 3.0],
            [0.75, 1.0, 1.5],
            [2, 5, 10],
        )
    ]


def _watchlist():
    config = bot_config.get_config(force_reload=True)
    configured = bot_config.get_path(config, "autonomous", "watchlist", default=bot_config.analyze_watchlist())
    return list(dict.fromkeys(
        str(symbol).upper()
        for symbol in configured
        if str(symbol).upper() in paper_bot.ALLOWED_SYMBOLS
    ))


def _close_trade(position, raw_exit, reason, slippage_bps):
    exit_price = raw_exit * (1 - slippage_bps / 10000)
    pnl = (exit_price - position["entry_price"]) * position["quantity"]
    return {
        "symbol": position["symbol"],
        "pnl": pnl,
        "reason": reason,
        "hold_bars": position["hold_bars"],
    }


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
        "symbol": symbol, "score": score, "latest": latest,
        "return_5": return_5, "return_20": return_20,
    }


def _simulate(bars_by_symbol, symbols, start_date, end_date, parameters):
    config = bot_config.get_config(force_reload=True)
    starting_capital = float(bot_config.virtual_capital())
    position_size = bot_config.as_float(
        bot_config.get_path(config, "autonomous", "position_size", default=10.0), 10.0, minimum=0.01
    )
    max_positions = bot_config.as_int(
        bot_config.get_path(config, "autonomous", "max_positions", default=2), 2, minimum=1
    )
    slippage_bps = bot_config.as_float(
        bot_config.get_path(config, "autonomous", "simulated_slippage_bps", default=5.0), 5.0, minimum=0
    )
    min_atr_pct = bot_config.as_float(
        bot_config.get_path(config, "autonomous", "min_atr_pct", default=0.10), 0.10
    )
    max_atr_pct = bot_config.as_float(
        bot_config.get_path(config, "autonomous", "max_atr_pct", default=5.0), 5.0
    )

    requested = list(dict.fromkeys(["SPY"] + symbols))
    bars_by_date = {
        symbol: {_bar_date(bar): bar for bar in bars_by_symbol.get(symbol, []) if _bar_date(bar)}
        for symbol in requested
    }
    dates = [date for date in sorted(bars_by_date.get("SPY", {})) if date <= end_date]
    histories = {symbol: [] for symbol in requested}
    positions = {}
    pending_candidate = None
    trades = []
    realized = 0.0
    peak_equity = starting_capital
    maximum_drawdown = 0.0

    for date in dates:
        if pending_candidate and date >= start_date and len(positions) < max_positions:
            symbol = pending_candidate["symbol"]
            entry_bar = bars_by_date.get(symbol, {}).get(date)
            if entry_bar and symbol not in positions:
                raw_open = _number(entry_bar, "o")
                if raw_open > 0:
                    entry_price = raw_open * (1 + slippage_bps / 10000)
                    positions[symbol] = {
                        "symbol": symbol,
                        "entry_price": entry_price,
                        "quantity": position_size / entry_price,
                        "highest_price": entry_price,
                        "hold_bars": 0,
                    }
            pending_candidate = None

        for symbol in list(positions):
            bar = bars_by_date.get(symbol, {}).get(date)
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
                exit_price, reason = close, "trend reversal"
            if exit_price is None and position["hold_bars"] >= parameters["max_hold_bars"]:
                exit_price, reason = close, "maximum hold"

            if exit_price is not None and exit_price > 0:
                trade = _close_trade(position, exit_price, reason, slippage_bps)
                trades.append(trade)
                realized += trade["pnl"]
                del positions[symbol]

        for symbol in requested:
            bar = bars_by_date.get(symbol, {}).get(date)
            if bar:
                histories[symbol].append(bar)

        if date >= start_date and date < end_date and len(positions) < max_positions and len(histories["SPY"]) >= 55:
            spy_closes = [_number(item, "c") for item in histories["SPY"]]
            spy_sma20 = autonomous_trader.sma(spy_closes, 20)
            spy_sma50 = autonomous_trader.sma(spy_closes, 50)
            healthy = bool(spy_sma20 and spy_sma50 and spy_closes[-1] > spy_sma20 and spy_sma20 > spy_sma50)
            if healthy:
                spy_return20 = spy_closes[-1] / spy_closes[-21] - 1
                candidates = []
                for symbol in symbols:
                    if symbol in positions or len(histories.get(symbol, [])) < 55:
                        continue
                    result = _analyze_bars(
                        symbol, histories[symbol], spy_return20, min_atr_pct, max_atr_pct
                    )
                    if result and result["score"] >= parameters["minimum_entry_score"]:
                        candidates.append(result)
                candidates.sort(key=lambda item: (item["score"], item["return_20"], item["return_5"]), reverse=True)
                pending_candidate = candidates[0] if candidates else None
            else:
                pending_candidate = None

        equity = starting_capital + realized
        for symbol, position in positions.items():
            bar = bars_by_date.get(symbol, {}).get(date)
            if bar:
                equity += (_number(bar, "c") - position["entry_price"]) * position["quantity"]
        peak_equity = max(peak_equity, equity)
        maximum_drawdown = max(maximum_drawdown, peak_equity - equity)

    final_date = dates[-1] if dates else end_date
    for symbol in list(positions):
        bar = bars_by_date.get(symbol, {}).get(final_date)
        if bar and _number(bar, "c") > 0:
            trade = _close_trade(positions[symbol], _number(bar, "c"), "end of period", slippage_bps)
            trades.append(trade)
            realized += trade["pnl"]

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "net_pnl": realized,
        "return_pct": (realized / starting_capital * 100) if starting_capital else 0.0,
        "profit_factor": profit_factor,
        "maximum_drawdown": maximum_drawdown,
        "maximum_drawdown_pct": (maximum_drawdown / peak_equity * 100) if peak_equity else 0.0,
    }


def _rank_value(metrics):
    trades = int(metrics.get("total_trades") or 0)
    if trades < 8:
        return -10000 + trades
    pf = min(float(metrics.get("profit_factor") or 0), 5.0)
    return (
        float(metrics.get("return_pct") or 0)
        + (pf - 1.0) * 8.0
        - float(metrics.get("maximum_drawdown_pct") or 0) * 0.45
    )


def run_optimization(days=730):
    paper_bot.reload_config()
    days = max(365, min(int(days), 1500))
    symbols = _watchlist()
    requested = list(dict.fromkeys(["SPY"] + symbols))
    bars_by_symbol = {symbol: backtester.get_daily_bars(symbol, days=days) for symbol in requested}
    spy_dates = sorted(_bar_date(bar) for bar in bars_by_symbol.get("SPY", []) if _bar_date(bar))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    test_dates = [date for date in spy_dates if date >= cutoff]
    if len(test_dates) < 160:
        raise RuntimeError(f"Not enough history for train/validation optimization. Received {len(test_dates)} SPY bars.")

    split_index = max(80, min(len(test_dates) - 40, int(len(test_dates) * 0.70)))
    train_start = test_dates[0]
    train_end = test_dates[split_index - 1]
    validation_start = test_dates[split_index]
    validation_end = test_dates[-1]

    grid = _parameter_grid()
    ranked = []
    for parameters in grid:
        training = _simulate(bars_by_symbol, symbols, train_start, train_end, parameters)
        ranked.append({
            "parameters": parameters,
            "training": training,
            "rank_score": _rank_value(training),
        })
    ranked.sort(key=lambda row: row["rank_score"], reverse=True)

    finalists = []
    for row in ranked[:10]:
        validation = _simulate(bars_by_symbol, symbols, validation_start, validation_end, row["parameters"])
        item = dict(row)
        item["validation"] = validation
        item["validation_passed"] = bool(
            validation["total_trades"] >= 5
            and validation["net_pnl"] > 0
            and validation["profit_factor"] > 1.0
            and validation["maximum_drawdown_pct"] < 20.0
        )
        finalists.append(item)

    current_parameters = _current_parameters()
    baseline_training = _simulate(bars_by_symbol, symbols, train_start, train_end, current_parameters)
    baseline_validation = _simulate(bars_by_symbol, symbols, validation_start, validation_end, current_parameters)

    best = finalists[0]
    result = {
        "days_requested": days,
        "symbols": symbols,
        "combinations_tested": len(grid),
        "train_start": train_start,
        "train_end": train_end,
        "validation_start": validation_start,
        "validation_end": validation_end,
        "best": best,
        "finalists": finalists,
        "baseline": {
            "parameters": current_parameters,
            "training": baseline_training,
            "validation": baseline_validation,
        },
        "missing_symbols": [symbol for symbol in symbols if len(bars_by_symbol.get(symbol, [])) < 55],
    }
    result["run_id"] = trading_database.save_optimizer_result(result)
    return result


def _parameter_text(parameters):
    return (
        f"score>={parameters['minimum_entry_score']}, stop {parameters['stop_atr_multiple']:.2f} ATR, "
        f"target {parameters['target_atr_multiple']:.2f} ATR, trail {parameters['trailing_atr_multiple']:.2f} ATR, "
        f"max hold {parameters['max_hold_bars']} daily bars"
    )


def _metric_text(label, metrics):
    return (
        f"{label}: {metrics['total_trades']} trades, {metrics['win_rate']:.1f}% wins, "
        f"P/L {paper_bot.money(metrics['net_pnl'])}, PF {metrics['profit_factor']:.2f}, "
        f"drawdown {metrics['maximum_drawdown_pct']:.2f}%"
    )


def format_result(result):
    if not result:
        return "No saved optimization results yet. Run !optimize first."
    best = result["best"]
    baseline = result["baseline"]
    lines = [
        "AI Paper Trader Parameter Optimization",
        f"Run ID: {result.get('run_id', '?')}",
        f"Combinations tested: {result.get('combinations_tested', 0)}",
        f"Training: {result.get('train_start')} through {result.get('train_end')}",
        f"Validation: {result.get('validation_start')} through {result.get('validation_end')}",
        "",
        "Best training configuration:",
        _parameter_text(best["parameters"]),
        _metric_text("Training", best["training"]),
        _metric_text("Unseen validation", best["validation"]),
        f"Validation check: {'PASSED' if best.get('validation_passed') else 'FAILED'}",
        "",
        "Current live settings for comparison:",
        _parameter_text(baseline["parameters"]),
        _metric_text("Training", baseline["training"]),
        _metric_text("Unseen validation", baseline["validation"]),
        "",
        "The optimizer does not change live settings automatically.",
    ]
    if result.get("missing_symbols"):
        lines.append("Insufficient history: " + ", ".join(result["missing_symbols"]))
    return "\n".join(lines)
