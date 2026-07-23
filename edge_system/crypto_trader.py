import os

import paper_bot
import trading_database
from crypto import broker

from . import common, config, data, signals, storage

STATE_NAME = "edge_crypto_state.json"


def defaults():
    return {
        "enabled": False,
        "last_signal_week": None,
        "pending_target": None,
        "last_decision": "Evidence crypto strategy is stopped.",
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
    state["last_decision"] = "Evidence crypto strategy enabled; waiting for weekly review."
    save(state)
    return state


def stop():
    state = load()
    state["enabled"] = False
    state["pending_target"] = None
    state["last_decision"] = "Evidence crypto entries stopped. Emergency protection remains active."
    save(state)
    return state


def _universe():
    return config.crypto_universe()


def _positions():
    universe = set(_universe())
    return [position for position in broker.crypto_positions() if broker.normalize(position.get("symbol")) in universe]


def _orders():
    universe = set(_universe())
    return [order for order in broker.crypto_orders("open") if broker.normalize(order.get("symbol")) in universe]


def _sell(position, reason):
    symbol = broker.normalize(position.get("symbol"))
    quantity = float(position.get("qty") or 0)
    result = broker.submit_sell(symbol, quantity)
    trading_database.log_trade(
        symbol, "sell", status=result.get("status"), quantity=quantity,
        entry_price=float(position.get("avg_entry_price") or 0),
        exit_price=float(position.get("current_price") or 0),
        pnl=float(position.get("unrealized_pl") or 0), reason=reason,
        order_id=result.get("id"), source="edge_crypto", details=position,
    )
    return "EDGE CRYPTO SELL %s: %g because %s. Status %s" % (symbol, quantity, reason, result.get("status", "submitted"))


def _buy(symbol, reason):
    size = config.crypto_position_size()
    result = broker.submit_buy(symbol, size)
    trading_database.log_trade(
        symbol, "buy", status=result.get("status"), dollars=size,
        reason=reason, order_id=result.get("id"), source="edge_crypto",
        details={"strategy": "weekly crypto trend rotation"},
    )
    return "EDGE CRYPTO BUY %s: %s. Status %s" % (symbol, paper_bot.money(size), result.get("status", "submitted"))


def _signal():
    settings = config.crypto()
    assets = broker.tradable_assets()
    universe = [symbol for symbol in _universe() if symbol in assets]
    raw = {symbol: common.completed_bars(data.crypto_bars(symbol, int(settings.get("history_days", 1800)))) for symbol in universe}
    return signals.crypto_target(raw, settings, universe)


def run_cycle():
    state = load()
    state["last_scan_at"] = common.now_utc().isoformat()
    events = []
    try:
        if "paper-api.alpaca.markets" not in os.environ.get("APCA_API_BASE_URL", ""):
            raise RuntimeError("Paper-only safety check failed.")
        positions = _positions()
        orders = _orders()

        for position in positions:
            entry = float(position.get("avg_entry_price") or 0)
            current = float(position.get("current_price") or 0)
            stop_pct = float(config.crypto().get("emergency_stop_pct", 18.0))
            if entry > 0 and current <= entry * (1.0 - stop_pct / 100.0) and not orders:
                events.append(_sell(position, "%.1f%% emergency stop" % stop_pct))
                state["last_decision"] = "Emergency crypto exit submitted."
                save(state)
                return events

        if not state.get("enabled"):
            state["last_decision"] = "Evidence crypto strategy is stopped."
        elif orders:
            state["last_decision"] = "Waiting for an evidence-crypto order to fill."
        elif state.get("pending_target") and not positions:
            target = state.get("pending_target")
            events.append(_buy(target, "pending weekly rotation target"))
            state["pending_target"] = None
            state["last_decision"] = "Bought pending weekly target %s." % target
        elif state.get("last_signal_week") == common.week_key():
            state["last_decision"] = "Weekly crypto review already completed."
        else:
            target, candidates, reason = _signal()
            current = broker.normalize(positions[0].get("symbol")) if positions else None
            storage.log_decision("crypto", target or "CASH", reason, {"candidates": candidates, "current": current})
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
        state["last_decision"] = "Evidence crypto error: %s" % error
        if state["errors"] >= 6:
            state["enabled"] = False
            events.append("EDGE CRYPTO STOPPED after repeated errors: %s" % error)
    save(state)
    return events


def panic():
    state = stop()
    events = [_sell(position, "manual evidence-crypto panic") for position in _positions()]
    save(state)
    return events or ["Evidence crypto strategy stopped. No edge crypto position was open."]


def status():
    state = load()
    positions = _positions()
    lines = [
        "AI Paper Trader Evidence Crypto",
        "Automation: %s" % ("RUNNING" if state.get("enabled") else "STOPPED"),
        "Mode: %s" % config.crypto_mode().upper(),
        "Strategy: weekly BTC-regime momentum rotation",
        "Universe: %s" % ", ".join(_universe()),
        "Position size: %s" % paper_bot.money(config.crypto_position_size()),
        "Last weekly review: %s" % (state.get("last_signal_week") or "not yet"),
        "Last decision: %s" % state.get("last_decision"),
    ]
    for position in positions:
        lines.append("- %s: value %s, P/L %s" % (broker.normalize(position.get("symbol")), paper_bot.money(position.get("market_value")), paper_bot.money(position.get("unrealized_pl"))))
    return "\n".join(lines)
