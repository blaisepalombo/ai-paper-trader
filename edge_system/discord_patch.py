"""Attach evidence-strategy commands to the existing Discord client.

The existing project uses a small single-file Discord client rather than an
extension framework. This module wraps discord.Client.event before that client
registers its handlers, allowing the edge subsystem to remain isolated.
"""

import asyncio
import functools
import os

import bot_config
import paper_bot
from crypto import broker as crypto_broker
from crypto import trader as legacy_crypto_trader

from . import backtest, config, crypto_trader, stock_trader, storage

_INSTALLED = False


def _channel_id(name):
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return None


def _allowed(message):
    if getattr(message.author, "bot", False):
        return False
    allowed_id = os.environ.get("DISCORD_ALLOWED_USER_ID", "").strip()
    return bool(allowed_id and allowed_id != "put_your_discord_user_id_here" and str(message.author.id) == allowed_id)


def _chunks(text, size=1800):
    text = str(text)
    return [text[index:index + size] for index in range(0, len(text), size)] or [""]


async def _send(channel, text):
    for chunk in _chunks(text):
        await channel.send("```text\n%s\n```" % chunk)


def _market_channel_ok(message, market):
    env_name = "DISCORD_CRYPTO_CHANNEL_ID" if market == "crypto" else "DISCORD_REPORT_CHANNEL_ID"
    expected = _channel_id(env_name)
    return expected is None or message.channel.id == expected


async def _run_test(message, markets, days):
    loop = asyncio.get_event_loop()
    for market in markets:
        if not _market_channel_ok(message, market) and len(markets) == 1:
            channel_name = "#crypto-bot" if market == "crypto" else "the stock report channel"
            await message.channel.send("Use this command in %s." % channel_name)
            return
        await message.channel.send(
            "Running fixed-rule %s evidence test over about %d days. This uses daily bars and may take several minutes."
            % (market, days)
        )
        result = await loop.run_in_executor(None, backtest.run, market, days)
        await _send(message.channel, backtest.format_result(result))


async def handle(message):
    if not _allowed(message):
        await message.channel.send("You are not allowed to control this bot.")
        return
    parts = message.content.strip().split()
    args = parts[1:]
    sub = args[0].lower() if args else "status"

    if sub == "status":
        await _send(message.channel, stock_trader.status() + "\n\n" + crypto_trader.status())
        return

    if sub == "test":
        market = args[1].lower() if len(args) > 1 else "both"
        if market not in {"stock", "crypto", "both"}:
            await message.channel.send("Use `!edge test stock 1825`, `!edge test crypto 1825`, or `!edge test both 1825`.")
            return
        try:
            days = int(args[2]) if len(args) > 2 else 1825
        except ValueError:
            await message.channel.send("Days must be a whole number, such as `!edge test stock 1825`.")
            return
        days = max(730, min(days, 3000))
        markets = ["stock", "crypto"] if market == "both" else [market]
        await _run_test(message, markets, days)
        return

    if sub == "results":
        market = args[1].lower() if len(args) > 1 else "stock"
        if market not in {"stock", "crypto"}:
            await message.channel.send("Use `!edge results stock` or `!edge results crypto`.")
            return
        result = storage.latest_strategy_result(market)
        await _send(message.channel, backtest.format_result(result))
        return

    if sub not in {"stock", "crypto"}:
        await _send(
            message.channel,
            "Evidence commands:\n"
            "!edge status\n"
            "!edge test stock 1825\n"
            "!edge test crypto 1825\n"
            "!edge test both 1825\n"
            "!edge results stock|crypto\n"
            "!edge stock start|stop|status|panic\n"
            "!edge crypto start|stop|status|panic",
        )
        return

    market = sub
    if not _market_channel_ok(message, market):
        channel_name = "#crypto-bot" if market == "crypto" else "the stock report channel"
        await message.channel.send("Use evidence %s commands in %s." % (market, channel_name))
        return
    action = args[1].lower() if len(args) > 1 else "status"
    trader = stock_trader if market == "stock" else crypto_trader

    if action == "start":
        latest = storage.latest_strategy_result(market)
        if not latest or not latest.get("passed"):
            output = "Start blocked. Run `!edge test %s 1825`; the latest unseen-data result must PASS first." % market
        elif market == "stock":
            stock_positions = [
                position for position in paper_bot.get_positions()
                if "/" not in crypto_broker.normalize(position.get("symbol"))
            ]
            stock_orders = [
                order for order in paper_bot.get_orders("open")
                if "/" not in crypto_broker.normalize(order.get("symbol"))
            ]
            if stock_positions or stock_orders:
                output = "Start blocked. Close existing stock positions and orders before handing control to the evidence engine."
            else:
                import autonomous_trader
                autonomous_trader.stop()
                trader.start()
                output = "Evidence stock paper strategy started. The older stock entry engine was stopped."
        else:
            if crypto_broker.crypto_positions() or crypto_broker.crypto_orders("open"):
                output = "Start blocked. Close existing crypto positions and orders before handing control to the evidence engine."
            else:
                legacy_crypto_trader.stop()
                trader.start()
                output = "Evidence crypto paper strategy started. The older crypto entry engine was stopped."
    elif action == "stop":
        trader.stop()
        output = "Evidence %s entries stopped; emergency protection remains active." % market
    elif action == "status":
        output = trader.status()
    elif action == "panic":
        output = "\n".join(trader.panic())
    else:
        output = "Use `!edge %s start`, `stop`, `status`, or `panic`." % market
    await _send(message.channel, output)


async def automation_loop(client):
    await client.wait_until_ready()
    stock_id = _channel_id("DISCORD_REPORT_CHANNEL_ID")
    crypto_id = _channel_id("DISCORD_CRYPTO_CHANNEL_ID")
    while not client.is_closed():
        try:
            stock_channel = client.get_channel(stock_id) if stock_id else None
            for event in stock_trader.run_cycle():
                if stock_channel:
                    await _send(stock_channel, event)
        except Exception as error:
            print("Evidence stock loop failed: %s" % error)
        try:
            crypto_channel = client.get_channel(crypto_id) if crypto_id else None
            for event in crypto_trader.run_cycle():
                if crypto_channel:
                    await _send(crypto_channel, event)
        except Exception as error:
            print("Evidence crypto loop failed: %s" % error)
        interval = bot_config.as_int(config.get("scan_interval_seconds", 900), 900, minimum=300)
        await asyncio.sleep(interval)


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        import discord
    except ImportError:
        return
    original_event = discord.Client.event

    def patched_event(client, coroutine):
        if coroutine.__name__ == "on_message":
            @functools.wraps(coroutine)
            async def wrapped_message(message):
                content = str(getattr(message, "content", "")).strip().lower()
                if content.startswith("!edge"):
                    try:
                        await handle(message)
                    except Exception as error:
                        await message.channel.send("Evidence command failed: `%s`" % error)
                    return
                if content == "!start" and stock_trader.load().get("enabled"):
                    await message.channel.send("The evidence stock engine is running. Stop it with `!edge stock stop` before starting the older engine.")
                    return
                if content.startswith("!crypto start") and crypto_trader.load().get("enabled"):
                    await message.channel.send("The evidence crypto engine is running. Stop it with `!edge crypto stop` before starting the older engine.")
                    return
                await coroutine(message)
            return original_event(client, wrapped_message)

        if coroutine.__name__ == "on_ready":
            @functools.wraps(coroutine)
            async def wrapped_ready(*args, **kwargs):
                await coroutine(*args, **kwargs)
                if not getattr(client, "edge_background_started", False):
                    client.edge_background_started = True
                    client.loop.create_task(automation_loop(client))
            return original_event(client, wrapped_ready)

        return original_event(client, coroutine)

    discord.Client.event = patched_event
    _INSTALLED = True
