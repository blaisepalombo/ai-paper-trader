"""Walk-forward parameter optimization for the 15-minute strategy."""

from datetime import datetime, timedelta, timezone
from itertools import product

import bot_config
import intraday_backtester
import paper_bot
import trading_database


def _current_parameters():
    return intraday_backtester._current_parameters()


def _parameter_grid():
    current = _current_parameters()
    return [
        {
            "minimum_entry_score": score,
            "stop_atr_multiple": stop,
            "target_atr_multiple": target,
            "trailing_atr_multiple": trail,
            "trail_arm_atr_multiple": current["trail_arm_atr_multiple"],
            "max_hold_minutes": hold,
            "cooldown_seconds": current["cooldown_seconds"],
            "max_trades_per_day": current["max_trades_per_day"],
        }
        for score, stop, target, trail, hold in product(
            [6, 7],
            [1.0, 1.5, 2.0],
            [1.5, 2.0, 3.0],
            [0.75, 1.0, 1.5],
            [390, 780, 1560],
        )
    ]


def _rank(metrics):
    trades = int(metrics.get("total_trades") or 0)
    if trades < 10:
        return -10000 + trades
    pf = min(float(metrics.get("profit_factor") or 0), 4.0)
    return float(metrics.get("return_pct") or 0) + (pf - 1.0) * 10.0 - float(metrics.get("maximum_drawdown_pct") or 0) * 0.6


def run_optimization(days=180):
    paper_bot.reload_config()
    days = max(90, min(int(days), 730))
    symbols, intraday, daily_spy = intraday_backtester.load_data(None, days)
    spy_bars = intraday.get("SPY", [])
    timestamps = [intraday_backtester._parse_time(bar.get("t")) for bar in spy_bars if bar.get("t")]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    test_times = [value for value in timestamps if value >= cutoff]
    if len(test_times) < 500:
        raise RuntimeError(f"Not enough 15-minute history for optimization. Received {len(test_times)} SPY bars.")

    split_index = max(300, min(len(test_times) - 150, int(len(test_times) * 0.70)))
    train_start, train_end = test_times[0], test_times[split_index - 1]
    validation_start, validation_end = test_times[split_index], test_times[-1]

    grid = _parameter_grid()
    ranked = []
    for parameters in grid:
        training = intraday_backtester.simulate(intraday, daily_spy, symbols, train_start, train_end, parameters)
        ranked.append({"parameters": parameters, "training": training, "rank_score": _rank(training)})
    ranked.sort(key=lambda row: row["rank_score"], reverse=True)

    finalists = []
    for row in ranked[:8]:
        validation = intraday_backtester.simulate(intraday, daily_spy, symbols, validation_start, validation_end, row["parameters"])
        item = dict(row)
        item["validation"] = validation
        item["validation_passed"] = bool(
            validation["total_trades"] >= 10
            and validation["net_pnl"] > 0
            and validation["profit_factor"] > 1.10
            and validation["maximum_drawdown_pct"] < 15.0
        )
        finalists.append(item)

    baseline_parameters = _current_parameters()
    baseline_training = intraday_backtester.simulate(intraday, daily_spy, symbols, train_start, train_end, baseline_parameters)
    baseline_validation = intraday_backtester.simulate(intraday, daily_spy, symbols, validation_start, validation_end, baseline_parameters)

    # Prefer a validation-passing finalist; otherwise report the training winner honestly.
    passing = [item for item in finalists if item["validation_passed"]]
    best = max(passing, key=lambda item: _rank(item["validation"])) if passing else finalists[0]
    result = {
        "days_requested": days,
        "symbols": symbols,
        "combinations_tested": len(grid),
        "train_start": train_start.date().isoformat(),
        "train_end": train_end.date().isoformat(),
        "validation_start": validation_start.date().isoformat(),
        "validation_end": validation_end.date().isoformat(),
        "best": best,
        "finalists": finalists,
        "baseline": {
            "parameters": baseline_parameters,
            "training": baseline_training,
            "validation": baseline_validation,
        },
        "missing_symbols": [symbol for symbol in symbols if len(intraday.get(symbol, [])) < 100],
    }
    result["run_id"] = trading_database.save_intraday_optimizer_result(result)
    return result


def _parameter_text(parameters):
    return (
        f"score>={parameters['minimum_entry_score']}, stop {parameters['stop_atr_multiple']:.2f} ATR, "
        f"target {parameters['target_atr_multiple']:.2f} ATR, trail {parameters['trailing_atr_multiple']:.2f} ATR, "
        f"max hold {parameters['max_hold_minutes']} minutes"
    )


def _metric_text(label, metrics):
    return (
        f"{label}: {metrics['total_trades']} trades, {metrics['win_rate']:.1f}% wins, "
        f"P/L {paper_bot.money(metrics['net_pnl'])}, PF {metrics['profit_factor']:.2f}, "
        f"drawdown {metrics['maximum_drawdown_pct']:.2f}%"
    )


def format_result(result):
    if not result:
        return "No saved intraday optimization results yet. Run !optimize intraday first."
    best = result["best"]
    baseline = result["baseline"]
    lines = [
        "AI Paper Trader Intraday Optimization",
        f"Run ID: {result.get('run_id', '?')}",
        f"Combinations tested: {result.get('combinations_tested', 0)}",
        f"Training: {result.get('train_start')} through {result.get('train_end')}",
        f"Validation: {result.get('validation_start')} through {result.get('validation_end')}",
        "",
        "Best candidate:",
        _parameter_text(best["parameters"]),
        _metric_text("Training", best["training"]),
        _metric_text("Unseen validation", best["validation"]),
        f"Validation check: {'PASSED' if best.get('validation_passed') else 'FAILED'}",
        "",
        "Current live settings:",
        _parameter_text(baseline["parameters"]),
        _metric_text("Training", baseline["training"]),
        _metric_text("Unseen validation", baseline["validation"]),
        "",
        "No live settings were changed automatically.",
    ]
    if result.get("missing_symbols"):
        lines.append("Insufficient history: " + ", ".join(result["missing_symbols"]))
    return "\n".join(lines)
