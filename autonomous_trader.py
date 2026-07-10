import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import bot_config
import paper_bot
import trade_exit


STATE_FILE = Path(__file__).with_name("autonomous_state.json")


def now_utc():
    return datetime.now(timezone.utc)


def today_utc():
    return now_utc().date().isoformat()


def default_state():
    return {
        "enabled": False,
        "date": today_utc(),
        "trades_today": 0,
        "realized_pl_today": 0.0,
        "last_trade_at": None,
        "consecutive_errors": 0,
        "positions": {},
        "closed_trades": [],
        "last_decision": "Automation is stopped.",
        "last_scan_at": None,
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = default_state()
    base = default_state()
    base.update(state if isinstance(state, dict) else {})
    if base.get("date") != today_utc():
        base["date"] = today_utc()
        base["trades_today"] = 0
        base["realized_pl_today"] = 0.0
        base["consecutive_errors"] = 0
    return base


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cfg(name, default):
    config = bot_config.get_config(force_reload=True)
    return bot_config.get_path(config, "autonomous", name, default=default)


def cfg_float(name, default, minimum=None, maximum=None):
    return bot_config.as_float(cfg(name, default), default, minimum, maximum)


def cfg_int(name, default, minimum=None, maximum=None):
    return bot_config.as_int(cfg(name, default), default, minimum, maximum)


def cfg_bool(name, default=False):
    return bot_config.as_bool(cfg(name, default), default)


def allowed_watchlist():
    configured = cfg("watchlist", bot_config.analyze_watchlist())
    allowed = paper_bot.ALLOWED_SYMBOLS
    if not isinstance(configured, list):
        configured = bot_config.analyze_watchlist()
    return [str(s).upper() for s in configured if str(s).upper() in allowed]


def get_bars(symbol, timeframe="15Min", limit=200):
    query = urlencode({
        "timeframe": timeframe,
        "limit": limit,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    })
    data = paper_bot.alpaca_data_request("GET", f"/stocks/{symbol}/bars?{query}")
    return data.get("bars", [])


def sma(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def atr(bars, length=14):
    if len(bars) < length + 1:
        return None
    ranges = []
    for i in range(1, len(bars)):
        high = float(bars[i].get("h") or 0)
        low = float(bars[i].get("l") or 0)
        prev_close = float(bars[i - 1].get("c") or 0)
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(ranges) < length:
        return None
    return sum(ranges[-length:]) / length


def analyze_bars(symbol, bars, spy_return_20=0.0):
    closes = [float(bar.get("c") or 0) for bar in bars]
    volumes = [float(bar.get("v") or 0) for bar in bars]
    if len(closes) < 55 or any(value <= 0 for value in closes[-55:]):
        return None

    latest = closes[-1]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    return_5 = latest / closes[-6] - 1
    return_20 = latest / closes[-21] - 1
    avg_volume20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    recent_high = max(closes[-20:])
    atr14 = atr(bars, 14)
    atr_pct = (atr14 / latest) * 100 if atr14 and latest else 0

    score = 0
    reasons = []
    if latest > sma20:
        score += 1
        reasons.append("price above SMA20")
    if sma20 > sma50:
        score += 1
        reasons.append("SMA20 above SMA50")
    if return_5 > 0:
        score += 1
        reasons.append("positive short momentum")
    if return_20 > 0:
        score += 1
        reasons.append("positive medium momentum")
    if symbol == "SPY" or return_20 > spy_return_20:
        score += 1
        reasons.append("relative strength")
    if avg_volume20 > 0 and volumes[-1] >= avg_volume20 * 0.8:
        score += 1
        reasons.append("volume confirmation")
    if latest >= recent_high * 0.985:
        score += 1
        reasons.append("near recent high")
    if cfg_float("min_atr_pct", 0.10) <= atr_pct <= cfg_float("max_atr_pct", 5.0):
        score += 1
        reasons.append("usable volatility")

    return {
        "symbol": symbol,
        "score": score,
        "latest": latest,
        "sma20": sma20,
        "sma50": sma50,
        "return_5": return_5,
        "return_20": return_20,
        "atr": atr14,
        "atr_pct": atr_pct,
        "reasons": reasons,
    }


def scan_candidates():
    watchlist = allowed_watchlist()
    if "SPY" not in watchlist:
        watchlist.insert(0, "SPY")

    bars_by_symbol = {symbol: get_bars(symbol) for symbol in watchlist}
    spy_bars = bars_by_symbol.get("SPY", [])
    spy_closes = [float(bar.get("c") or 0) for bar in spy_bars]
    if len(spy_closes) < 55:
        return False, [], "Not enough SPY data for the market filter."

    spy_sma20 = sma(spy_closes, 20)
    spy_sma50 = sma(spy_closes, 50)
    spy_return20 = spy_closes[-1] / spy_closes[-21] - 1
    market_healthy = spy_closes[-1] > spy_sma20 and spy_sma20 > spy_sma50

    results = []
    for symbol, bars in bars_by_symbol.items():
        result = analyze_bars(symbol, bars, spy_return20)
        if result:
            results.append(result)
    results.sort(key=lambda item: (item["score"], item["return_20"], item["return_5"]), reverse=True)

    regime = (
        f"SPY market filter {'healthy' if market_healthy else 'weak'}: "
        f"price {paper_bot.money(spy_closes[-1])}, SMA20 {paper_bot.money(spy_sma20)}, "
        f"SMA50 {paper_bot.money(spy_sma50)}"
    )
    return market_healthy, results, regime


def start():
    state = load_state()
    state["enabled"] = True
    state["last_decision"] = "Autonomous paper trading enabled. Waiting for the next scan."
    save_state(state)
    return state


def stop():
    state = load_state()
    state["enabled"] = False
    state["last_decision"] = "New autonomous entries are stopped. Existing positions remain protected."
    save_state(state)
    return state


def is_enabled():
    return bool(load_state().get("enabled"))


def cooldown_active(state):
    raw = state.get("last_trade_at")
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    elapsed = (now_utc() - last).total_seconds()
    return elapsed < cfg_int("cooldown_seconds", 1800, minimum=0)


def exposure(positions):
    return sum(abs(float(position.get("market_value") or 0)) for position in positions)


def open_order_symbols(open_orders):
    return {str(order.get("symbol", "")).upper() for order in open_orders}


def update_position_tracking(state, positions):
    tracked = state.setdefault("positions", {})
    current_symbols = set()
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        if not symbol:
            continue
        current_symbols.add(symbol)
        current_price = float(position.get("current_price") or 0)
        avg = float(position.get("avg_entry_price") or 0)
        item = tracked.setdefault(symbol, {
            "entry_time": now_utc().isoformat(),
            "entry_price": avg,
            "highest_price": current_price,
        })
        item["highest_price"] = max(float(item.get("highest_price") or 0), current_price)
        item["last_price"] = current_price
        item["qty"] = float(position.get("qty") or 0)

    for symbol in list(tracked):
        if symbol not in current_symbols:
            tracked.pop(symbol, None)


def exit_reason(position, tracked):
    symbol = str(position.get("symbol", "")).upper()
    avg = float(position.get("avg_entry_price") or 0)
    current = float(position.get("current_price") or 0)
    if avg <= 0 or current <= 0:
        return None

    bars = get_bars(symbol)
    closes = [float(bar.get("c") or 0) for bar in bars]
    atr14 = atr(bars, 14)
    if len(closes) < 25 or not atr14:
        return None

    stop = avg - cfg_float("stop_atr_multiple", 1.25, minimum=0.1) * atr14
    target = avg + cfg_float("target_atr_multiple", 2.0, minimum=0.1) * atr14
    highest = max(float(tracked.get("highest_price") or current), current)
    trailing = highest - cfg_float("trailing_atr_multiple", 1.0, minimum=0.1) * atr14
    trail_armed = highest >= avg + cfg_float("trail_arm_atr_multiple", 1.0, minimum=0.1) * atr14
    sma20 = sma(closes, 20)

    if current <= stop:
        return f"ATR stop at {paper_bot.money(stop)}"
    if current >= target:
        return f"ATR target at {paper_bot.money(target)}"
    if trail_armed and current <= trailing:
        return f"ATR trailing stop at {paper_bot.money(trailing)}"
    if sma20 and current < sma20:
        return f"trend reversal below SMA20 {paper_bot.money(sma20)}"

    try:
        entered = datetime.fromisoformat(tracked.get("entry_time"))
        held_minutes = (now_utc() - entered).total_seconds() / 60
        if held_minutes >= cfg_int("max_hold_minutes", 780, minimum=30):
            return f"maximum hold time of {held_minutes:.0f} minutes"
    except (TypeError, ValueError):
        pass
    return None


def record_closed_trade(state, position, reason):
    avg = float(position.get("avg_entry_price") or 0)
    current = float(position.get("current_price") or 0)
    qty = float(position.get("qty") or 0)
    realized = (current - avg) * qty
    state["realized_pl_today"] = float(state.get("realized_pl_today") or 0) + realized
    trades = state.setdefault("closed_trades", [])
    trades.append({
        "time": now_utc().isoformat(),
        "symbol": position.get("symbol"),
        "entry": avg,
        "exit_estimate": current,
        "qty": qty,
        "estimated_pl": realized,
        "reason": reason,
    })
    state["closed_trades"] = trades[-100:]


def run_exits(state, positions, open_orders):
    events = []
    sell_symbols = {
        str(order.get("symbol", "")).upper()
        for order in open_orders
        if str(order.get("side", "")).lower() == "sell"
    }
    tracked = state.setdefault("positions", {})

    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        if not symbol or symbol in sell_symbols:
            continue
        reason = exit_reason(position, tracked.get(symbol, {}))
        if not reason:
            continue
        qty = float(position.get("qty") or 0)
        result = trade_exit.submit_sell_order(symbol, qty)
        order_id = result.get("id", "unknown")
        status = result.get("status", "submitted")
        record_closed_trade(state, position, reason)
        paper_bot.log_trade(symbol, 0, status, f"autonomous exit: {reason}; qty={qty:g}; order_id={order_id}")
        events.append(f"AUTO SELL {symbol}: {qty:g} shares because {reason}. Status {status}, order {order_id}")
    return events


def entry_allowed(state, positions, open_orders, account, clock):
    if not state.get("enabled"):
        return False, "Automation is stopped."
    if not clock.get("is_open"):
        return False, "Market is closed."
    if open_orders:
        return False, "Waiting for an open order to finish."
    if len(positions) >= cfg_int("max_positions", 2, minimum=1):
        return False, "Maximum autonomous positions reached."
    if int(state.get("trades_today") or 0) >= cfg_int("max_trades_per_day", 3, minimum=1):
        return False, "Daily trade limit reached."
    if float(state.get("realized_pl_today") or 0) <= -cfg_float("daily_loss_limit", 5.0, minimum=0.01):
        return False, "Daily loss limit reached."
    if cooldown_active(state):
        return False, "Cooldown is active."
    if int(state.get("consecutive_errors") or 0) >= cfg_int("max_consecutive_errors", 3, minimum=1):
        return False, "Automation stopped by repeated-error protection."
    if exposure(positions) >= cfg_float("max_total_exposure", 20.0, minimum=1):
        return False, "Maximum total exposure reached."
    if "paper-api.alpaca.markets" not in os_base_url():
        return False, "Paper-only safety check failed."
    return True, "Entry checks passed."


def os_base_url():
    import os
    return os.environ.get("APCA_API_BASE_URL", "")


def run_entry(state, positions, open_orders, account, clock):
    allowed, reason = entry_allowed(state, positions, open_orders, account, clock)
    if not allowed:
        state["last_decision"] = reason
        return []

    market_healthy, candidates, regime = scan_candidates()
    if not market_healthy:
        state["last_decision"] = regime + ". No entry."
        return []

    held = {str(position.get("symbol", "")).upper() for position in positions}
    pending = open_order_symbols(open_orders)
    candidates = [item for item in candidates if item["symbol"] not in held and item["symbol"] not in pending]
    minimum_score = cfg_int("minimum_entry_score", 6, minimum=1, maximum=8)
    qualified = [item for item in candidates if item["score"] >= minimum_score]
    if not qualified:
        best = candidates[0] if candidates else None
        state["last_decision"] = (
            f"No entry. Best score was {best['symbol']} {best['score']}/8."
            if best else "No eligible candidates."
        )
        return []

    best = qualified[0]
    remaining_exposure = cfg_float("max_total_exposure", 20.0, minimum=1) - exposure(positions)
    dollars = min(cfg_float("position_size", 10.0, minimum=0.01), remaining_exposure)
    dollars = round(max(0, dollars), 2)
    if dollars < 1:
        state["last_decision"] = "Remaining exposure is too small for another position."
        return []

    result = paper_bot.submit_order(best["symbol"], dollars)
    status = result.get("status", "submitted")
    order_id = result.get("id", "unknown")
    state["trades_today"] = int(state.get("trades_today") or 0) + 1
    state["last_trade_at"] = now_utc().isoformat()
    state["last_decision"] = (
        f"Bought {best['symbol']} for ${dollars:.2f}; score {best['score']}/8. "
        + ", ".join(best["reasons"])
    )
    paper_bot.log_trade(best["symbol"], dollars, status, f"autonomous entry score={best['score']}; order_id={order_id}; {best}")
    return [f"AUTO BUY {best['symbol']}: ${dollars:.2f}, score {best['score']}/8. Status {status}, order {order_id}"]


def run_cycle():
    state = load_state()
    events = []
    state["last_scan_at"] = now_utc().isoformat()
    try:
        paper_bot.reload_config()
        account = paper_bot.get_account()
        clock = paper_bot.get_clock()
        positions = paper_bot.get_positions()
        open_orders = paper_bot.get_orders("open")
        update_position_tracking(state, positions)
        events.extend(run_exits(state, positions, open_orders))
        if not events:
            events.extend(run_entry(state, positions, open_orders, account, clock))
        state["consecutive_errors"] = 0
    except Exception as error:
        state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1
        state["last_decision"] = f"Automation error: {error}"
        if state["consecutive_errors"] >= cfg_int("max_consecutive_errors", 3, minimum=1):
            state["enabled"] = False
            events.append(f"AUTOMATION STOPPED after repeated errors: {error}")
    save_state(state)
    return events


def status_report():
    state = load_state()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    lines = [
        "AI Paper Trader Status",
        f"Autonomous trading: {'RUNNING' if state.get('enabled') else 'STOPPED'}",
        f"Market open: {paper_bot.get_clock().get('is_open')}",
        f"Open positions: {len(positions)}",
        f"Open orders: {len(open_orders)}",
        f"Trades today: {state.get('trades_today', 0)}/{cfg_int('max_trades_per_day', 3)}",
        f"Estimated realized P/L today: {paper_bot.money(state.get('realized_pl_today', 0))}",
        f"Last scan: {state.get('last_scan_at') or 'not yet'}",
        f"Last decision: {state.get('last_decision') or 'none'}",
    ]
    for position in positions:
        symbol = position.get("symbol", "?")
        value = paper_bot.money(position.get("market_value"))
        pl = paper_bot.money(position.get("unrealized_pl"))
        plpc = paper_bot.percent(float(position.get("unrealized_plpc") or 0) * 100)
        lines.append(f"- {symbol}: value {value}, P/L {pl} ({plpc})")
    return "\n".join(lines)


def summary_report():
    state = load_state()
    closed = state.get("closed_trades", [])
    wins = sum(1 for trade in closed if float(trade.get("estimated_pl") or 0) > 0)
    losses = sum(1 for trade in closed if float(trade.get("estimated_pl") or 0) < 0)
    total = sum(float(trade.get("estimated_pl") or 0) for trade in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0
    positions = paper_bot.get_positions()
    unrealized = sum(float(position.get("unrealized_pl") or 0) for position in positions)
    estimated_value = bot_config.virtual_capital() + total + unrealized
    simulated_slippage_bps = cfg_float("simulated_slippage_bps", 5.0, minimum=0)
    approximate_slippage = len(closed) * 2 * cfg_float("position_size", 10.0) * simulated_slippage_bps / 10000

    lines = [
        "AI Paper Trader Summary",
        f"Starting experiment balance: {paper_bot.money(bot_config.virtual_capital())}",
        f"Estimated experiment value: {paper_bot.money(estimated_value)}",
        f"Estimated realized P/L: {paper_bot.money(total)}",
        f"Open unrealized P/L: {paper_bot.money(unrealized)}",
        f"Closed trades: {len(closed)}",
        f"Wins / losses: {wins} / {losses}",
        f"Win rate: {win_rate:.1f}%",
        f"Estimated slippage adjustment: -{paper_bot.money(approximate_slippage)}",
        f"After simulated slippage: {paper_bot.money(total + unrealized - approximate_slippage)} P/L",
    ]
    if closed:
        lines.append("")
        lines.append("Recent closed trades:")
        for trade in closed[-5:]:
            lines.append(
                f"- {trade.get('symbol')}: {paper_bot.money(trade.get('estimated_pl'))}, {trade.get('reason')}"
            )
    return "\n".join(lines)


def panic_close():
    state = stop()
    clock = paper_bot.get_clock()
    if not clock.get("is_open"):
        state["last_decision"] = "Panic requested, but market is closed. New entries remain stopped."
        save_state(state)
        return ["Automation stopped. Market is closed, so positions were not sold."]

    positions = paper_bot.get_positions()
    events = []
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        qty = float(position.get("qty") or 0)
        if not symbol or qty <= 0:
            continue
        result = trade_exit.submit_sell_order(symbol, qty)
        events.append(f"PANIC SELL {symbol}: {qty:g} shares, status {result.get('status', 'submitted')}")
    if not events:
        events.append("Automation stopped. There were no open positions.")
    return events
