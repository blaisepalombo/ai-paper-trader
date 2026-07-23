from collections import defaultdict
from datetime import datetime, timedelta, timezone

import bot_config
import paper_bot
from . import common, config, data, signals, storage


def _bars_map(bars):
    output = {}
    for bar in bars:
        try:
            output[common.bar_date(bar)] = bar
        except (TypeError, ValueError):
            continue
    return output


def _history_through(bars, before_date):
    return [bar for bar in bars if common.bar_date(bar) < before_date]


def _simulate(market, raw_bars, settings, universe, start_date, end_date, position_size, cost_rate, emergency_stop_pct, benchmark_symbol):
    benchmark_bars = raw_bars.get(benchmark_symbol, [])
    timeline = sorted(
        common.bar_date(bar)
        for bar in benchmark_bars
        if start_date <= common.bar_date(bar) <= end_date
    )
    maps = {symbol: _bars_map(bars) for symbol, bars in raw_bars.items()}
    starting_capital = float(bot_config.virtual_capital())
    position = None
    trades = []
    realized = 0.0
    equity_curve = []
    last_week = None

    for date_value in timeline:
        current_dt = datetime(date_value.year, date_value.month, date_value.day, tzinfo=timezone.utc)
        benchmark_bar = maps.get(benchmark_symbol, {}).get(date_value)
        if not benchmark_bar:
            continue

        if position:
            bar = maps.get(position["symbol"], {}).get(date_value)
            if bar:
                stop_price = position["entry_price"] * (1.0 - emergency_stop_pct / 100.0)
                if common.number(bar, "l") <= stop_price:
                    trade = common.close_trade(position, stop_price, current_dt, "emergency stop", cost_rate)
                    trades.append(trade)
                    realized += trade["pnl"]
                    position = None

        this_week = common.week_key(current_dt)
        if this_week != last_week:
            histories = {
                symbol: _history_through(bars, date_value)
                for symbol, bars in raw_bars.items()
            }
            if market == "stock":
                target, _, _ = signals.stock_target(histories, settings)
            else:
                target, _, _ = signals.crypto_target(histories, settings, universe)

            current_symbol = position["symbol"] if position else None
            if current_symbol != target:
                if position:
                    bar = maps.get(position["symbol"], {}).get(date_value)
                    if bar and common.number(bar, "o") > 0:
                        trade = common.close_trade(position, common.number(bar, "o"), current_dt, "weekly rebalance", cost_rate)
                        trades.append(trade)
                        realized += trade["pnl"]
                        position = None
                if target:
                    target_bar = maps.get(target, {}).get(date_value)
                    raw_open = common.number(target_bar or {}, "o")
                    if raw_open > 0:
                        entry_price = raw_open * (1.0 + cost_rate)
                        position = {
                            "symbol": target,
                            "entry_time": current_dt,
                            "entry_price": entry_price,
                            "quantity": position_size / entry_price,
                        }
            last_week = this_week

        equity = starting_capital + realized
        if position:
            bar = maps.get(position["symbol"], {}).get(date_value)
            if bar:
                equity += (common.number(bar, "c") - position["entry_price"]) * position["quantity"]
        equity_curve.append(equity)

    if position and timeline:
        final_date = timeline[-1]
        bar = maps.get(position["symbol"], {}).get(final_date)
        if bar:
            final_dt = datetime(final_date.year, final_date.month, final_date.day, tzinfo=timezone.utc)
            trade = common.close_trade(position, common.number(bar, "c"), final_dt, "end of period", cost_rate)
            trades.append(trade)
            realized += trade["pnl"]
            equity_curve.append(starting_capital + realized)

    result = common.metrics(trades, equity_curve, starting_capital)
    result["trades_detail"] = trades
    result["start_date"] = start_date.isoformat()
    result["end_date"] = end_date.isoformat()
    return result


def _benchmark(raw_bars, symbol, start_date, end_date, position_size, cost_rate):
    bars = [bar for bar in raw_bars.get(symbol, []) if start_date <= common.bar_date(bar) <= end_date]
    if len(bars) < 2:
        return {"net_pnl": 0.0, "return_pct": 0.0, "maximum_drawdown_pct": 0.0, "risk_adjusted_score": 0.0}
    entry = common.number(bars[0], "o") * (1.0 + cost_rate)
    quantity = position_size / entry if entry > 0 else 0
    equity_curve = []
    for bar in bars:
        equity_curve.append(float(bot_config.virtual_capital()) + (common.number(bar, "c") - entry) * quantity)
    exit_price = common.number(bars[-1], "c") * (1.0 - cost_rate)
    realized = (exit_price - entry) * quantity
    result = common.metrics([
        {
            "pnl": realized,
            "hold_days": (common.bar_date(bars[-1]) - common.bar_date(bars[0])).days,
        }
    ], equity_curve, float(bot_config.virtual_capital()))
    result["risk_adjusted_score"] = common.risk_adjusted_score(result)
    return result


def _periods(raw_bars, benchmark_symbol, days):
    dates = sorted(common.bar_date(bar) for bar in raw_bars.get(benchmark_symbol, []))
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=int(days))
    dates = [value for value in dates if value >= cutoff and value < datetime.now(timezone.utc).date()]
    if len(dates) < 300:
        raise RuntimeError("Not enough completed daily history for this edge test.")
    split = int(len(dates) * 0.70)
    return dates[0], dates[split - 1], dates[split], dates[-1]


def run(market="stock", days=1825):
    market = str(market).lower()
    days = max(730, min(int(days), 3000))
    if market == "stock":
        settings = config.stock()
        universe = [str(symbol).upper() for symbol in settings.get("universe", [])]
        raw_bars = {symbol: data.stock_bars(symbol, max(days + 420, int(settings.get("history_days", 2200)))) for symbol in universe}
        benchmark_symbol = "SPY"
        position_size = float(settings.get("position_size", 30.0))
        cost_rate = float(settings.get("slippage_bps_per_side", 5.0)) / 10000.0
    elif market == "crypto":
        settings = config.crypto()
        universe = config.crypto_universe()
        raw_bars = {symbol: data.crypto_bars(symbol, max(days + 420, int(settings.get("history_days", 1800)))) for symbol in universe}
        benchmark_symbol = "BTC/USD"
        position_size = config.crypto_position_size()
        cost_rate = (float(settings.get("fee_bps_per_side", 25.0)) + float(settings.get("slippage_bps_per_side", 8.0))) / 10000.0
    else:
        raise ValueError("Market must be stock or crypto.")

    train_start, train_end, validation_start, validation_end = _periods(raw_bars, benchmark_symbol, days)
    emergency_stop_pct = float(settings.get("emergency_stop_pct", 12.0 if market == "stock" else 18.0))
    training = _simulate(market, raw_bars, settings, universe, train_start, train_end, position_size, cost_rate, emergency_stop_pct, benchmark_symbol)
    validation = _simulate(market, raw_bars, settings, universe, validation_start, validation_end, position_size, cost_rate, emergency_stop_pct, benchmark_symbol)
    full = _simulate(market, raw_bars, settings, universe, train_start, validation_end, position_size, cost_rate, emergency_stop_pct, benchmark_symbol)
    benchmark_training = _benchmark(raw_bars, benchmark_symbol, train_start, train_end, position_size, cost_rate)
    benchmark_validation = _benchmark(raw_bars, benchmark_symbol, validation_start, validation_end, position_size, cost_rate)
    benchmark_full = _benchmark(raw_bars, benchmark_symbol, train_start, validation_end, position_size, cost_rate)

    validation_score = common.risk_adjusted_score(validation)
    benchmark_score = common.risk_adjusted_score(benchmark_validation)
    passed = (
        validation["net_pnl"] > 0
        and validation["total_trades"] >= 1
        and validation_score > benchmark_score
        and validation["maximum_drawdown_pct"] <= benchmark_validation["maximum_drawdown_pct"] + 0.01
    )
    result = {
        "market": market,
        "days_requested": days,
        "universe": universe,
        "position_size": position_size,
        "cost_bps_per_side": cost_rate * 10000.0,
        "training": training,
        "validation": validation,
        "full": full,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_training": benchmark_training,
        "benchmark_validation": benchmark_validation,
        "benchmark_full": benchmark_full,
        "validation_risk_adjusted_score": validation_score,
        "benchmark_validation_risk_adjusted_score": benchmark_score,
        "passed": passed,
    }
    result["run_id"] = storage.save_strategy_result(result)
    return result


def format_result(result):
    if not result:
        return "No saved edge-strategy result yet."
    market = str(result.get("market", "?")).upper()
    training = result.get("training", {})
    validation = result.get("validation", {})
    full = result.get("full", {})
    benchmark = result.get("benchmark_validation", {})
    lines = [
        "AI Paper Trader Evidence Strategy - %s" % market,
        "Run ID: %s" % result.get("run_id", "?"),
        "Universe: %s" % ", ".join(result.get("universe", [])),
        "Position size: %s" % paper_bot.money(result.get("position_size")),
        "Modeled cost: %.1f bps per side" % float(result.get("cost_bps_per_side") or 0),
        "",
        "Training: %s through %s" % (training.get("start_date"), training.get("end_date")),
        "- %d trades, P/L %s, return %.2f%%, PF %.2f, drawdown %.2f%%" % (
            int(training.get("total_trades") or 0), paper_bot.money(training.get("net_pnl")),
            float(training.get("return_pct") or 0), float(training.get("profit_factor") or 0),
            float(training.get("maximum_drawdown_pct") or 0),
        ),
        "Validation: %s through %s" % (validation.get("start_date"), validation.get("end_date")),
        "- %d trades, P/L %s, return %.2f%%, PF %.2f, drawdown %.2f%%" % (
            int(validation.get("total_trades") or 0), paper_bot.money(validation.get("net_pnl")),
            float(validation.get("return_pct") or 0), float(validation.get("profit_factor") or 0),
            float(validation.get("maximum_drawdown_pct") or 0),
        ),
        "- %s benchmark: P/L %s, return %.2f%%, drawdown %.2f%%" % (
            result.get("benchmark_symbol"), paper_bot.money(benchmark.get("net_pnl")),
            float(benchmark.get("return_pct") or 0), float(benchmark.get("maximum_drawdown_pct") or 0),
        ),
        "",
        "Full period: %d trades, P/L %s, return %.2f%%, drawdown %.2f%%" % (
            int(full.get("total_trades") or 0), paper_bot.money(full.get("net_pnl")),
            float(full.get("return_pct") or 0), float(full.get("maximum_drawdown_pct") or 0),
        ),
        "Validation verdict: %s" % ("PASSED" if result.get("passed") else "FAILED"),
        "Pass rule: positive unseen P/L, at least 1 completed trade, lower/equal drawdown than benchmark, and better return-to-drawdown than benchmark.",
        "Fixed research-backed rules; no parameter optimization and no live settings changed.",
    ]
    return "\n".join(lines)
