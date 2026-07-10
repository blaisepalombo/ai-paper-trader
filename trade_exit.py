import secrets
from urllib.parse import quote

import bot_config
import paper_bot


pending_exit = None


def automatic_exits_enabled():
    config = bot_config.get_config(force_reload=True)
    value = bot_config.get_path(config, "risk", "automatic_exits_enabled", default=False)
    return bot_config.as_bool(value, False)


def find_position(symbol, positions=None):
    symbol = symbol.upper().strip()
    positions = positions if positions is not None else paper_bot.get_positions()
    for position in positions:
        if str(position.get("symbol", "")).upper() == symbol:
            return position
    return None


def submit_sell_order(symbol, qty):
    quantity = float(qty)
    if quantity <= 0:
        raise RuntimeError("Sell quantity must be above zero.")

    order = {
        "symbol": symbol.upper(),
        "qty": format(quantity, ".9f").rstrip("0").rstrip("."),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }
    return paper_bot.alpaca_request("POST", "/orders", order)


def close_position_now(symbol):
    path = f"/positions/{quote(symbol.upper())}"
    return paper_bot.alpaca_request("DELETE", path)


def position_report(positions=None):
    positions = positions if positions is not None else paper_bot.get_positions()
    lines = ["AI Paper Trader Positions"]

    if not positions:
        lines.append("No open positions.")
        return "\n".join(lines)

    total_value = 0.0
    total_pl = 0.0
    for position in positions:
        symbol = position.get("symbol", "?")
        qty = float(position.get("qty") or 0)
        avg = float(position.get("avg_entry_price") or 0)
        current = float(position.get("current_price") or 0)
        value = float(position.get("market_value") or 0)
        pl = float(position.get("unrealized_pl") or 0)
        plpc = float(position.get("unrealized_plpc") or 0) * 100
        total_value += value
        total_pl += pl
        lines.append(
            f"- {symbol}: qty {qty:g}, avg {paper_bot.money(avg)}, current {paper_bot.money(current)}, "
            f"value {paper_bot.money(value)}, P/L {paper_bot.money(pl)} ({paper_bot.percent(plpc)})"
        )

    lines.append(f"Total market value: {paper_bot.money(total_value)}")
    lines.append(f"Total unrealized P/L: {paper_bot.money(total_pl)}")
    return "\n".join(lines)


def parse_sell_amount(position, amount_text):
    held_qty = float(position.get("qty") or 0)
    if held_qty <= 0:
        raise ValueError("Position quantity is unavailable.")

    text = (amount_text or "all").strip().lower()
    if text == "all":
        return held_qty, "all"

    if text.endswith("%"):
        try:
            pct = float(text[:-1])
        except ValueError as error:
            raise ValueError("Percentage must look like `50%`.") from error
        if pct <= 0 or pct > 100:
            raise ValueError("Percentage must be above 0 and no more than 100.")
        qty = held_qty * (pct / 100)
        return qty, f"{pct:g}%"

    try:
        qty = float(text)
    except ValueError as error:
        raise ValueError("Use `all`, a percentage like `50%`, or an exact share quantity.") from error

    if qty <= 0:
        raise ValueError("Share quantity must be above zero.")
    if qty > held_qty + 1e-9:
        raise ValueError(f"You only hold {held_qty:g} shares.")
    return qty, f"{qty:g} shares"


async def handle_sell(message, args):
    global pending_exit

    if not args:
        await message.channel.send("Use `!sell SPY`, `!sell SPY 50%`, or `!sell SPY 0.004`.")
        return

    clock = paper_bot.get_clock()
    if not clock.get("is_open"):
        await message.channel.send("Safety stop: market is closed. The bot will not submit a market sell right now.")
        return

    symbol = args[0].upper()
    positions = paper_bot.get_positions()
    position = find_position(symbol, positions)
    if position is None:
        await message.channel.send(f"No open `{symbol}` position was found. Use `!positions`.")
        return

    amount_text = args[1] if len(args) > 1 else "all"
    try:
        qty, label = parse_sell_amount(position, amount_text)
    except ValueError as error:
        await message.channel.send(str(error))
        return

    held_qty = float(position.get("qty") or 0)
    code = f"SELL-{symbol}-{secrets.token_hex(2).upper()}"
    pending_exit = {
        "type": "sell",
        "user_id": str(message.author.id),
        "symbol": symbol,
        "qty": qty,
        "held_qty": held_qty,
        "label": label,
        "code": code,
    }

    await message.channel.send(
        f"Paper sell staged: sell `{label}` of `{symbol}` ({qty:g} shares).\n"
        f"You currently hold `{held_qty:g}` shares.\n"
        f"Approve with: `!approve {code}`\n"
        "This affects Alpaca paper trading only."
    )


async def handle_closeall(message):
    global pending_exit

    clock = paper_bot.get_clock()
    if not clock.get("is_open"):
        await message.channel.send("Safety stop: market is closed. The bot will not close positions right now.")
        return

    positions = paper_bot.get_positions()
    if not positions:
        await message.channel.send("There are no open positions to close.")
        return

    code = f"CLOSEALL-{secrets.token_hex(2).upper()}"
    snapshot = [
        {
            "symbol": str(position.get("symbol", "")).upper(),
            "qty": float(position.get("qty") or 0),
        }
        for position in positions
        if position.get("symbol") and float(position.get("qty") or 0) > 0
    ]
    pending_exit = {
        "type": "closeall",
        "user_id": str(message.author.id),
        "positions": snapshot,
        "code": code,
    }

    symbols = ", ".join(item["symbol"] for item in snapshot)
    await message.channel.send(
        f"Close-all staged for `{symbols}`.\n"
        f"Approve with: `!approve {code}`\n"
        "This will submit market sells for every open Alpaca paper position."
    )


async def handle_approve(message, args):
    global pending_exit

    if pending_exit is None:
        return False
    if str(message.author.id) != pending_exit.get("user_id"):
        await message.channel.send("Only the user who staged this exit can approve it.")
        return True
    if not args or args[0] != pending_exit.get("code"):
        return False

    action = pending_exit
    pending_exit = None

    clock = paper_bot.get_clock()
    if not clock.get("is_open"):
        await message.channel.send("Safety stop: market is now closed. Nothing was sold.")
        return True

    if action["type"] == "sell":
        symbol = action["symbol"]
        qty = action["qty"]
        current = find_position(symbol)
        if current is None:
            await message.channel.send(f"The `{symbol}` position is no longer open. Nothing was sold.")
            return True
        current_qty = float(current.get("qty") or 0)
        qty = min(qty, current_qty)
        try:
            result = submit_sell_order(symbol, qty)
            status = result.get("status", "submitted")
            order_id = result.get("id", "unknown")
            paper_bot.log_trade(symbol, 0, status, f"paper sell qty={qty:g}; order_id={order_id}; {result}")
            await message.channel.send(
                f"Submitted paper sell: `{symbol}` `{qty:g}` shares. Status: `{status}`. Order ID: `{order_id}`"
            )
        except RuntimeError as error:
            paper_bot.log_trade(symbol, 0, "failed", f"paper sell qty={qty:g}; {error}")
            await message.channel.send(f"Sell failed: `{error}`")
        return True

    if action["type"] == "closeall":
        lines = ["Close-all results:"]
        current_positions = paper_bot.get_positions()
        current_by_symbol = {str(p.get("symbol", "")).upper(): p for p in current_positions}
        for item in action["positions"]:
            symbol = item["symbol"]
            current = current_by_symbol.get(symbol)
            if current is None:
                lines.append(f"- {symbol}: no longer open")
                continue
            qty = float(current.get("qty") or 0)
            try:
                result = submit_sell_order(symbol, qty)
                status = result.get("status", "submitted")
                order_id = result.get("id", "unknown")
                paper_bot.log_trade(symbol, 0, status, f"closeall paper sell qty={qty:g}; order_id={order_id}; {result}")
                lines.append(f"- {symbol}: submitted {qty:g} shares, status {status}, order {order_id}")
            except RuntimeError as error:
                paper_bot.log_trade(symbol, 0, "failed", f"closeall paper sell qty={qty:g}; {error}")
                lines.append(f"- {symbol}: failed, {error}")
        await message.channel.send("```text\n" + "\n".join(lines) + "\n```")
        return True

    return False


def cancel_pending():
    global pending_exit
    had_pending = pending_exit is not None
    pending_exit = None
    return had_pending


async def run_automatic_exits(channel, positions, open_orders):
    if not automatic_exits_enabled():
        return

    clock = paper_bot.get_clock()
    if not clock.get("is_open"):
        return

    open_sell_symbols = {
        str(order.get("symbol", "")).upper()
        for order in open_orders
        if str(order.get("side", "")).lower() == "sell"
    }
    stop_pct = bot_config.stop_loss_percent()
    target_pct = bot_config.take_profit_percent()

    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        if not symbol or symbol in open_sell_symbols:
            continue
        avg = float(position.get("avg_entry_price") or 0)
        current = float(position.get("current_price") or 0)
        qty = float(position.get("qty") or 0)
        if avg <= 0 or current <= 0 or qty <= 0:
            continue

        reason = None
        if current <= avg * (1 - stop_pct / 100):
            reason = f"stop threshold {stop_pct:g}%"
        elif current >= avg * (1 + target_pct / 100):
            reason = f"take-profit threshold {target_pct:g}%"

        if reason is None:
            continue

        try:
            result = submit_sell_order(symbol, qty)
            status = result.get("status", "submitted")
            order_id = result.get("id", "unknown")
            paper_bot.log_trade(symbol, 0, status, f"automatic exit: {reason}; qty={qty:g}; order_id={order_id}; {result}")
            await channel.send(
                f"Automatic paper exit submitted for `{symbol}`: `{qty:g}` shares because of {reason}. "
                f"Status: `{status}`. Order ID: `{order_id}`"
            )
        except RuntimeError as error:
            paper_bot.log_trade(symbol, 0, "failed", f"automatic exit: {reason}; qty={qty:g}; {error}")
            await channel.send(f"Automatic paper exit failed for `{symbol}`: `{error}`")
