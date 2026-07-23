import json
import math
from datetime import datetime, timezone
from pathlib import Path


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    text = str(value or "").replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def bar_date(bar):
    return parse_time(bar.get("t")).date()


def number(bar, key):
    try:
        return float(bar.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def sma(values, length):
    if len(values) < int(length):
        return None
    return sum(values[-int(length):]) / int(length)


def momentum_return(values, lookback, skip=0):
    lookback = int(lookback)
    skip = int(skip)
    end_index = len(values) - 1 - skip
    start_index = end_index - lookback
    if start_index < 0 or end_index < 0:
        return None
    start = float(values[start_index])
    end = float(values[end_index])
    if start <= 0:
        return None
    return end / start - 1.0


def weighted_momentum(values, lookbacks, weights, skip=0):
    returns = []
    for lookback in lookbacks:
        value = momentum_return(values, lookback, skip)
        if value is None:
            return None, []
        returns.append(value)
    if len(weights) != len(returns):
        weights = [1.0 / len(returns)] * len(returns)
    score = sum(float(weight) * value for weight, value in zip(weights, returns))
    return score, returns


def completed_bars(bars):
    today = now_utc().date()
    output = []
    for bar in bars:
        try:
            if bar_date(bar) < today:
                output.append(bar)
        except (TypeError, ValueError):
            continue
    return output


def week_key(value=None):
    value = value or now_utc()
    iso = value.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def state_path(name):
    return Path.home() / "ai-paper-trader-data" / name


def load_state(name, defaults):
    path = state_path(name)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    result = dict(defaults)
    if isinstance(loaded, dict):
        result.update(loaded)
    return result


def save_state(name, state):
    path = state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def close_trade(position, exit_price, exit_time, reason, cost_rate):
    entry_price = float(position["entry_price"])
    quantity = float(position["quantity"])
    net_exit = float(exit_price) * (1.0 - cost_rate)
    pnl = (net_exit - entry_price) * quantity
    return {
        "symbol": position["symbol"],
        "entry_time": position["entry_time"].isoformat(),
        "exit_time": exit_time.isoformat(),
        "entry_price": entry_price,
        "exit_price": net_exit,
        "quantity": quantity,
        "pnl": pnl,
        "reason": reason,
        "hold_days": max(0, (exit_time.date() - position["entry_time"].date()).days),
    }


def metrics(trades, equity_curve, starting_capital):
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    net_pnl = sum(trade["pnl"] for trade in trades)
    peak = float(starting_capital)
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, float(value))
        max_drawdown = max(max_drawdown, peak - float(value))
    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "net_pnl": net_pnl,
        "return_pct": net_pnl / starting_capital * 100 if starting_capital else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0),
        "average_win": gross_profit / len(wins) if wins else 0.0,
        "average_loss": -gross_loss / len(losses) if losses else 0.0,
        "maximum_drawdown": max_drawdown,
        "maximum_drawdown_pct": max_drawdown / peak * 100 if peak else 0.0,
        "average_hold_days": sum(trade["hold_days"] for trade in trades) / len(trades) if trades else 0.0,
    }


def risk_adjusted_score(result):
    drawdown = max(float(result.get("maximum_drawdown_pct") or 0), 0.25)
    return float(result.get("return_pct") or 0) / drawdown


def annualized_volatility(values, window=30, periods=252):
    if len(values) < window + 1:
        return None
    returns = []
    for index in range(len(values) - window, len(values)):
        previous = values[index - 1]
        current = values[index]
        if previous > 0:
            returns.append(current / previous - 1.0)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(max(variance, 0.0)) * math.sqrt(periods)
