import os
from datetime import datetime, timezone

import paper_bot
import trade_exit
import trading_database

from . import common, config, data, signals

STATE_NAME = "edge_stock_state.json"


def defaults():
    return {
        "enabled": False,
        "last_signal_week": None,
        "pending_target": None,
        "last_decision": "Evidence stock strategy is stopped.",
        "last_scan_at": None,
        "errors": 0,
    }


def load():
    return common.load_state(STATE_NAME, defaults())


def save(state):
    common.save_state(STATE_NAME, state)


def start():
    state = load()
    state["enabled"] = True
    state["last_decision"] = "Evidence stock strategy enabled; waiting for weekly review."
    save(state)
    return state


def stop():
    state = load()
    state["enabled"] = False
    state["pending_target"] = None
    state["last_decision"] = "Evidence stock entries stopped. Emergency protection remains active."
    save(state)
    return state


def _universe():
    return [str(symbol).upper() for symbol in config.stock().get("universe", [])]


def _positions():
    universe = set(_universe())
    return [position for position in paper_bot.get_positions() if str(position.get("symbol", "")).upper() in universe]


def _orders():
    universe = set(_universe())
    return [order for order in paper_bot.get_orders("open") if str(order.get("symbol", "")).upper() in universe]


def _sell(position, reason):
    symbol = str(position.get("symbol", "")).upper()
    quantity = float(position.get("qty") or 0)
    result = trade_exit.submit_sell_order(symbol, quantity)
    trading_database.log_trade(
        symbol, "sell", status=result.get("status"), quantity=quantity,
        entry_price=float(position.get("avg_entry_price") or 0),
        exit_price=float(position.get("current_price") or 0),
        pnl=float(position.get("unrealized_pl") or 0), reason=reason,
        order_id=result.get("id"), source="edge_stock", details=position,
    )
    return "EDGE STOCK SELL %s: %g shares because %s. Status %s" % (symbol, quantity, reason, result.get("status", "submitted"))


def _buy(symbol, reason):
    size = float(config.stock().get("position_size", 30.0))
    result = paper_bot.submit_order(symbol, size)
    trading_database.log_trade(
        symbol, "buy", status=result.get("status"), dollars=size,
        reason=reason, order_id=result.get("id"), source="edge_stock",
        details={"strategy": "weekly ETF dual momentum"},
    )
    return "EDGE STOCK BUY %s: %s. Status %s" % (symbol, paper_bot.money(size), result.get("status", "submitted"))


def _signal():
    settings = config.stock()
    raw = {symbol: common.completed_bars(data.stock_bars(symbol, int(settings.get("history_days", 2200)))) for symbol in _universe()}
    return signals.stock_target(raw, settings)


def run_cycle():
    state = load()
    state["last_scan_at"] = common.now_utc().isoformat()
    events = []
    try:
        if "paper-api.alpaca.markets" not in os.environ.get("APCA_API_BASE_URL", ""):
            raise RuntimeError("Paper-only safety check failed.")
        clock = paper_bot.get_clock()
        positions = _positions()
        orders = _orders()

        for position in positions:
            entry = float(position.get("avg_entry_price") or 0)
            current = float(position.get("current_price") or 0)
            stop_pct = float(config.stock().get("emergency_stop_pct", 12.0))
            if entry > 0 and current <= entry * (1.0 - stop_pct / 100.0) and not orders:
                events.append(_sell(position, "%.1f%% emergency stop" % stop_pct))
                state["last_decision"] = "Emergency stock exit submitted."
                save(state)
                return events

        if not state.get("enabled"):
            state["last_decision"] = "Evidence stock strategy is stopped."
        elif not clock.get("is_open"):
            state["last_decision"] = "Stock market is closed."
        elif orders:
            state["last_decision"] = "Waiting for an evidence-stock order to fill."
        elif state.get("pending_target") and not positions:
            target = state.get("pending_target")
            events.append(_buy(target, "pending weekly rotation target"))
            state["pending_target"] = None
            state["last_decision"] = "Bought pending weekly target %s." % target
        elif state.get("last_signal_week") == common.week_key():
            state["last_decision"] = "Weekly stock review already completed."
        else:
            target, candidates, reason = _signal()
            current = str(positions[0].get("symbol", "")).upper() if positions else None
            trading_database.log_edge_decision("stock", target or "CASH", reason, {"candidates": candidates, "current": current})
            if current == target:
                state["last_signal_week"] = common.week_key()
                state["last_decision"] = "Hold %s. %s" % (current, reason)
            elif current:
                events.append(_sell(positions[0], "weekly rotation to %s" % (target or "cash")))
                state["pending_target"] = target
                state["last_signal_week"] = common.week_key()
                state["last_decision"] = "Sold %s; next target is %s." % (current, target or "cash")
            elif target:
                events.append(_buy(target, reason))
                state["last_signal_week"] = common.week_key()
                state["last_decision"] = "Bought %s. %s" % (target, reason)
            else:
                state["last_signal_week"] = common.week_key()
                state["last_decision"] = reason
        state["errors"] = 0
    except Exception as error:
        state["errors"] = int(state.get("errors") or 0) + 1
        state["last_decision"] = "Evidence stock error: %s" % error
        if state["errors"] >= 6:
            state["enabled"] = False
            events.append("EDGE STOCK STOPPED after repeated errors: %s" % error)
    save(state)
    return events


def panic():
    state = stop()
    if not paper_bot.get_clock().get("is_open"):
        return ["Evidence stock strategy stopped. Market is closed, so positions were not sold."]
    events = [_sell(position, "manual evidence-stock panic") for position in _positions()]
    save(state)
    return events or ["Evidence stock strategy stopped. No edge ETF position was open."]


def status():
    state = load()
    positions = _positions()
    lines = [
        "AI Paper Trader Evidence Stock",
        "Automation: %s" % ("RUNNING" if state.get("enabled") else "STOPPED"),
        "Strategy: weekly ETF dual momentum + 200-day absolute trend",
        "Universe: %s" % ", ".join(_universe()),
        "Position size: %s" % paper_bot.money(config.stock().get("position_size")),
        "Last weekly review: %s" % (state.get("last_signal_week") or "not yet"),
        "Last decision: %s" % state.get("last_decision"),
    ]
    for position in positions:
        lines.append("- %s: value %s, P/L %s" % (position.get("symbol"), paper_bot.money(position.get("market_value")), paper_bot.money(position.get("unrealized_pl"))))
    return "\n".join(lines)
