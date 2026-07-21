import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from discord import message

try:
    import discord
except ImportError:
    print("Missing discord.py. Install requirements first.")
    sys.exit(1)

import autonomous_trader
import backtester
import optimizer
import bot_config
import paper_bot
import trade_exit
import trading_database


COMMAND_PREFIX = "!"
AUTO_STATE_FILE = Path("auto_report_state.json")
pending_trade = None
last_auto_fingerprint = None


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"AI Paper Trader is running\n")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    print(f"Health server running on port {port}")


def load_settings():
    paper_bot.load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    allowed_user_id = os.environ.get("DISCORD_ALLOWED_USER_ID", "").strip()
    if not token or token == "put_your_discord_bot_token_here":
        print("Missing DISCORD_BOT_TOKEN in .env.")
        sys.exit(1)
    return token, allowed_user_id


def report_channel_id():
    value = os.environ.get("DISCORD_REPORT_CHANNEL_ID", "").strip()
    if not value or value == "put_your_discord_report_channel_id_here":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def allowed(message, allowed_user_id):
    if message.author.bot:
        return False
    if not allowed_user_id or allowed_user_id == "put_your_discord_user_id_here":
        return message.content.strip().lower() in {"!whoami", "!help"}
    return str(message.author.id) == allowed_user_id


def split_message(text, max_len=1800):
    return [text[i:i + max_len] for i in range(0, len(text), max_len)] or [""]


async def send_codeblock(channel, text):
    for chunk in split_message(str(text)):
        await channel.send(f"```text\n{chunk}\n```")


def load_auto_state():
    if not AUTO_STATE_FILE.exists():
        return {}
    try:
        return json.loads(AUTO_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_auto_state(state):
    AUTO_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def get_report():
    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    recent_orders = paper_bot.get_orders("all", 10)
    report = paper_bot.build_report(account, clock, positions, open_orders, recent_orders)
    paper_bot.REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    return report, account, clock, positions, open_orders, recent_orders


def portfolio_summary(positions):
    open_value = sum(float(position.get("market_value") or 0) for position in positions)
    open_pl = sum(float(position.get("unrealized_pl") or 0) for position in positions)
    return {
        "virtual_capital": paper_bot.VIRTUAL_CAPITAL,
        "open_value": open_value,
        "open_pl": open_pl,
        "estimated_value": paper_bot.VIRTUAL_CAPITAL + open_pl,
    }


def build_portfolio_summary(positions):
    summary = portfolio_summary(positions)
    exposure_pct = (summary["open_value"] / paper_bot.VIRTUAL_CAPITAL * 100) if paper_bot.VIRTUAL_CAPITAL else 0
    return "\n".join([
        "AI Paper Trader Portfolio",
        f"Virtual starting capital: {paper_bot.money(summary['virtual_capital'])}",
        f"Open market value: {paper_bot.money(summary['open_value'])}",
        f"Open unrealized P/L: {paper_bot.money(summary['open_pl'])}",
        f"Estimated experiment value: {paper_bot.money(summary['estimated_value'])}",
        f"Open exposure: {paper_bot.percent(exposure_pct)}",
    ])


def build_short_update(account, clock, positions, open_orders):
    lines = [
        "AI Paper Trader Update",
        f"Version: {bot_config.git_commit_short()}",
        f"Autonomous trading: {'RUNNING' if autonomous_trader.is_enabled() else 'STOPPED'}",
        f"Market open: {clock.get('is_open')}",
        f"Open positions: {len(positions)}",
        f"Open orders: {len(open_orders)}",
    ]
    summary = portfolio_summary(positions)
    lines.append(f"Estimated experiment value: {paper_bot.money(summary['estimated_value'])}")
    lines.append(f"Open P/L: {paper_bot.money(summary['open_pl'])}")
    for position in positions:
        symbol = position.get("symbol", "?")
        value = paper_bot.money(position.get("market_value"))
        pl = paper_bot.money(position.get("unrealized_pl"))
        plpc = paper_bot.percent(float(position.get("unrealized_plpc") or 0) * 100)
        lines.append(f"- {symbol}: value {value}, P/L {pl} ({plpc})")
    return "\n".join(lines)


def build_daily_recap(account, clock, positions, open_orders, recent_orders):
    lines = [
        "AI Paper Trader Daily Recap",
        f"Version: {bot_config.git_commit_short()}",
        f"Report time UTC: {datetime.now(timezone.utc).isoformat()}",
        autonomous_trader.summary_report(),
    ]
    if recent_orders:
        lines.append("")
        lines.append("Recent orders:")
        for order in recent_orders[:5]:
            lines.append(f"- {paper_bot.order_label(order)}")
    return "\n".join(lines)


def build_risk_settings_report(positions=None):
    config = bot_config.get_config(force_reload=True)
    lines = [
        "AI Paper Trader Risk Settings",
        f"Fixed alert stop: {bot_config.stop_loss_percent(config):g}% below entry",
        f"Fixed alert target: {bot_config.take_profit_percent(config):g}% above entry",
        "Autonomous exits: ATR stop, ATR target, trailing stop, trend reversal, and max hold time",
        f"Automatic autonomous trading: {'RUNNING' if autonomous_trader.is_enabled() else 'STOPPED'}",
        "Paper trading only.",
    ]
    if positions:
        lines.append("")
        lines.append("Current positions:")
        for position in positions:
            lines.append(
                f"- {position.get('symbol', '?')}: avg {paper_bot.money(position.get('avg_entry_price'))}, "
                f"current {paper_bot.money(position.get('current_price'))}"
            )
    return "\n".join(lines)


def current_snapshot(clock, positions, open_orders, recent_orders):
    return {
        "market_open": bool(clock.get("is_open")),
        "position_symbols": sorted(str(p.get("symbol")) for p in positions if p.get("symbol")),
        "open_orders": sorted(
            (str(o.get("id")), str(o.get("status")), str(o.get("filled_qty")))
            for o in open_orders if o.get("id")
        ),
        "recent_orders": {
            str(order.get("id")): {
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "status": order.get("status"),
                "filled_qty": order.get("filled_qty"),
            }
            for order in recent_orders[:20] if order.get("id")
        },
    }


def build_change_alert(previous, current, account, clock, positions, open_orders, recent_orders):
    lines = []
    if previous.get("market_open") is False and current.get("market_open") is True:
        lines.append("Market opened.")
    elif previous.get("market_open") is True and current.get("market_open") is False:
        lines.append("Market closed.")

    previous_orders = previous.get("recent_orders", {})
    for order in recent_orders[:10]:
        order_id = str(order.get("id") or "")
        if not order_id:
            continue
        old_status = previous_orders.get(order_id, {}).get("status")
        new_status = order.get("status")
        if old_status != "filled" and new_status == "filled":
            lines.append(f"Order filled: {paper_bot.order_label(order)}")
        elif old_status and old_status != new_status:
            lines.append(f"Order update: {paper_bot.order_label(order)}")

    previous_positions = set(previous.get("position_symbols", []))
    current_positions = set(current.get("position_symbols", []))
    for symbol in sorted(current_positions - previous_positions):
        lines.append(f"New position: {symbol}")
    for symbol in sorted(previous_positions - current_positions):
        lines.append(f"Position closed: {symbol}")

    if not lines:
        return None
    lines.append("")
    lines.append(build_short_update(account, clock, positions, open_orders))
    return "\n".join(lines)


async def handle_start(message):
    autonomous_trader.start()
    await send_codeblock(message.channel, "Autonomous paper trading started.\nThe bot will scan during market hours and manage entries and exits automatically.\nUse !stop to stop new entries or !panic to stop and close positions.")


async def handle_stop(message):
    autonomous_trader.stop()
    await send_codeblock(message.channel, "Autonomous paper trading stopped.\nNo new positions will be opened. Existing positions will still be monitored by the autonomous exit rules until they are closed or you use !panic.")


async def handle_status(message, args):
    if args and args[0].lower() == "full":
        report, *_ = get_report()
        await send_codeblock(message.channel, report)
    else:
        await send_codeblock(message.channel, autonomous_trader.status_report())


async def handle_summary(message):
    await send_codeblock(message.channel, autonomous_trader.summary_report())


async def handle_panic(message):
    events = autonomous_trader.panic_close()
    await send_codeblock(message.channel, "\n".join(events))


async def handle_brief(message):
    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    await send_codeblock(message.channel, build_short_update(account, clock, positions, open_orders))


async def handle_pnl(message):
    await send_codeblock(message.channel, build_portfolio_summary(paper_bot.get_positions()))


async def handle_positions(message):
    await send_codeblock(message.channel, trade_exit.position_report())


async def handle_risk(message):
    await send_codeblock(message.channel, build_risk_settings_report(paper_bot.get_positions()))


async def handle_config(message):
    await send_codeblock(message.channel, bot_config.config_report())


async def handle_version(message):
    await send_codeblock(message.channel, bot_config.version_report())


async def handle_reload(message):
    bot_config.reload_config()
    paper_bot.reload_config()
    await send_codeblock(message.channel, "Configuration reloaded.")


async def handle_backtest(message, args):
    if args and args[0].lower() == "results":
        await send_codeblock(message.channel, backtester.format_result(trading_database.latest_backtest_result()))
        return

    symbol = None
    days = 365
    if args:
        first = args[0].upper()
        if first.isdigit():
            days = int(first)
        else:
            symbol = first
    if len(args) > 1:
        try:
            days = int(args[1])
        except ValueError:
            await message.channel.send("Days must be a whole number, such as `!backtest SPY 365`.")
            return
    days = max(90, min(days, 1500))
    await message.channel.send(
        f"Running daily backtest for `{symbol or 'configured watchlist'}` over about `{days}` calendar days. This can take a minute."
    )
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, backtester.run_backtest, symbol, days)
        await send_codeblock(message.channel, backtester.format_result(result))
    except Exception as error:
        await message.channel.send(f"Backtest failed: `{error}`")


async def handle_optimize(message, args):
    if args and args[0].lower() == "results":
        await send_codeblock(message.channel, optimizer.format_result(trading_database.latest_optimizer_result()))
        return

    days = 730
    if args:
        try:
            days = int(args[0])
        except ValueError:
            await message.channel.send("Use `!optimize`, `!optimize 730`, or `!optimize results`.")
            return
    days = max(365, min(days, 1500))
    await message.channel.send(
        f"Running train/validation optimization over about `{days}` calendar days. "
        "This tests hundreds of configurations and may take several minutes."
    )
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, optimizer.run_optimization, days)
        await send_codeblock(message.channel, optimizer.format_result(result))
    except Exception as error:
        await message.channel.send(f"Optimization failed: `{error}`")


async def handle_stats(message):
    trades, decisions = trading_database.stats_summary(days=30)
    closed = int(trades.get("wins") or 0) + int(trades.get("losses") or 0)
    win_rate = (int(trades.get("wins") or 0) / closed * 100) if closed else 0
    lines = [
        "AI Paper Trader Database Stats - Last 30 Days",
        f"Database: {trading_database.database_path()}",
        f"Decisions recorded: {int(decisions.get('decisions') or 0)}",
        f"Buy decisions: {int(decisions.get('buy_decisions') or 0)}",
        f"Pass decisions: {int(decisions.get('passes') or 0)}",
        f"Trade events recorded: {int(trades.get('trade_events') or 0)}",
        f"Closed wins / losses: {int(trades.get('wins') or 0)} / {int(trades.get('losses') or 0)}",
        f"Win rate: {win_rate:.1f}%",
        f"Recorded net P/L: {paper_bot.money(trades.get('net_pnl'))}",
    ]
    await send_codeblock(message.channel, "\n".join(lines))


async def handle_journal(message):
    rows = paper_bot.read_trade_log(limit=15)
    recent_orders = paper_bot.get_orders("all", 15)
    lines = ["AI Paper Trader Journal"]
    if rows:
        lines.append("")
        lines.append("Local log:")
        for row in rows:
            lines.append(
                f"- {row.get('timestamp_utc', '?')}: {row.get('symbol', '?')} "
                f"${row.get('dollars', '?')}, {row.get('status', '?')}"
            )
    if recent_orders:
        lines.append("")
        lines.append("Recent Alpaca orders:")
        for order in recent_orders[:10]:
            lines.append(f"- {paper_bot.order_label(order)}")
    await send_codeblock(message.channel, "\n".join(lines))


async def handle_recap(message):
    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    recent_orders = paper_bot.get_orders("all", 20)
    await send_codeblock(message.channel, build_daily_recap(account, clock, positions, open_orders, recent_orders))


async def handle_suggest(message):
    try:
        market_healthy, candidates, regime = autonomous_trader.scan_candidates()
        lines = [regime]
        if candidates:
            lines.append("")
            lines.append("Top candidates:")
            for item in candidates[:5]:
                lines.append(f"- {item['symbol']}: {item['score']}/8, ATR {item['atr_pct']:.2f}%")
        lines.append("")
        lines.append("The autonomous bot will decide whether to trade during its next scan.")
        await send_codeblock(message.channel, "\n".join(lines))
    except Exception as error:
        await message.channel.send(f"Scan failed: `{error}`")


async def handle_analyze(message):
    await handle_suggest(message)


async def handle_cancelorder(message, args):
    open_orders = paper_bot.get_orders("open")
    if not open_orders:
        await message.channel.send("No open paper orders to cancel.")
        return
    if args and args[0].lower() == "all":
        targets = open_orders
    elif args:
        requested = args[0].upper()
        targets = [
            order for order in open_orders
            if str(order.get("id", "")).startswith(args[0])
            or str(order.get("symbol", "")).upper() == requested
        ][:1]
    else:
        targets = open_orders[:1]
    lines = []
    for order in targets:
        try:
            paper_bot.cancel_order(order.get("id"))
            lines.append(f"Cancel requested for {order.get('symbol', '?')}.")
        except RuntimeError as error:
            lines.append(f"Cancel failed: {error}")
    await send_codeblock(message.channel, "\n".join(lines))


async def handle_trade(message, args):
    global pending_trade
    if not args:
        await message.channel.send("Use `!trade SPY 5`.")
        return
    symbol = args[0].upper()
    try:
        dollars = float(args[1] if len(args) > 1 else paper_bot.MAX_DOLLARS_PER_TRADE)
    except ValueError:
        await message.channel.send("Dollar amount must be a number.")
        return

    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    if not clock.get("is_open"):
        await message.channel.send("Market is closed.")
        return
    if symbol not in paper_bot.ALLOWED_SYMBOLS:
        await message.channel.send("That symbol is not on the allowlist.")
        return
    if dollars <= 0 or dollars > paper_bot.MAX_DOLLARS_PER_TRADE:
        await message.channel.send(f"Manual trade limit is ${paper_bot.MAX_DOLLARS_PER_TRADE:.2f}.")
        return
    if open_orders:
        await message.channel.send("An order is already open.")
        return
    if symbol in {str(p.get('symbol', '')).upper() for p in positions}:
        await message.channel.send(f"{symbol} is already open.")
        return

    code = f"BUY-{symbol}-{int(dollars * 100)}"
    pending_trade = {
        "user_id": str(message.author.id),
        "symbol": symbol,
        "dollars": dollars,
        "code": code,
    }
    await message.channel.send(
        f"Paper buy staged: `${dollars:.2f}` of `{symbol}`.\nApprove with: `!approve {code}`"
    )


async def handle_approve(message, args):
    global pending_trade
    if await trade_exit.handle_approve(message, args):
        return
    if pending_trade is None:
        await message.channel.send("No matching staged action.")
        return
    if str(message.author.id) != pending_trade.get("user_id"):
        await message.channel.send("Only the user who staged it can approve it.")
        return
    if not args or args[0] != pending_trade.get("code"):
        await message.channel.send("Approval code does not match.")
        return

    action = pending_trade
    pending_trade = None
    try:
        result = paper_bot.submit_order(action["symbol"], action["dollars"])
        status = result.get("status", "submitted")
        order_id = result.get("id", "unknown")
        paper_bot.log_trade(action["symbol"], action["dollars"], status, f"manual buy order_id={order_id}")
        await message.channel.send(f"Submitted paper buy for `{action['symbol']}`. Status `{status}`. Order `{order_id}`")
    except RuntimeError as error:
        await message.channel.send(f"Buy failed: `{error}`")


async def handle_cancel(message):
    global pending_trade
    had_buy = pending_trade is not None
    pending_trade = None
    had_exit = trade_exit.cancel_pending()
    await message.channel.send("Pending action canceled." if had_buy or had_exit else "No pending action.")


async def handle_autotest(message, client):
    channel_id = report_channel_id()
    channel = client.get_channel(channel_id) if channel_id else None
    if channel is None:
        await message.channel.send("Report channel is not configured.")
        return
    await send_codeblock(channel, autonomous_trader.status_report())
    await message.channel.send("Test report sent.")


def simple_help():
    return """Main commands:
!start - start autonomous paper trading
!stop - stop new autonomous entries
!status - show what the bot is doing
!summary - show results and P/L
!stats - show SQLite memory stats
!backtest - test the current strategy on daily historical bars
!optimize - train and validate strategy settings
!panic - stop automation and close all positions during market hours
!help advanced - show every manual and diagnostic command

The bot uses a fixed liquid-stock allowlist, momentum/trend scoring, an SPY market filter, ATR-based exits, position limits, a daily loss limit, cooldowns, and paper trading only.
"""


def advanced_help():
    return """Advanced commands:
!status full - full account report
!brief - short account report
!pnl - open-position P/L
!positions - open positions
!risk - risk and exit settings
!config - tracked configuration
!version - deployed GitHub commit
!reload - reload configuration
!stats - SQLite decision and trade stats
!backtest - test configured watchlist for 365 days
!backtest SPY 365 - test one symbol
!backtest results - show latest saved result
!optimize - test parameter combinations over 730 days
!optimize 1095 - choose a longer history
!optimize results - show latest optimization
!journal - local log and recent orders
!recap - daily recap
!suggest or !analyze - scan current candidates
!trade SPY 5 - stage a manual paper buy
!sell SPY - stage a full manual paper sell
!sell SPY 50% - stage a partial sell
!closeall - stage closing all positions
!approve CODE - approve a staged manual action
!cancel - cancel a staged manual action
!cancelorder - cancel newest unfilled order
!cancelorder all - cancel all unfilled orders
!autotest - send a test report
!whoami - show your Discord ID
!channelid - show this channel ID
"""


async def automation_loop(client):
    await client.wait_until_ready()
    channel_id = report_channel_id()
    channel = client.get_channel(channel_id) if channel_id else None
    while not client.is_closed():
        try:
            events = autonomous_trader.run_cycle()
            if channel:
                for event in events:
                    await send_codeblock(channel, event)
        except Exception as error:
            print(f"Automation loop failed: {error}")
        interval = bot_config.as_int(
            bot_config.get_path(bot_config.get_config(force_reload=True), "autonomous", "scan_interval_seconds", default=300),
            300,
            minimum=60,
        )
        await asyncio.sleep(interval)


async def report_loop(client):
    global last_auto_fingerprint
    await client.wait_until_ready()
    if not bot_config.auto_reports_enabled():
        return
    channel_id = report_channel_id()
    channel = client.get_channel(channel_id) if channel_id else None
    if channel is None:
        return

    saved_state = load_auto_state()
    while not client.is_closed():
        try:
            account = paper_bot.get_account()
            clock = paper_bot.get_clock()
            positions = paper_bot.get_positions()
            open_orders = paper_bot.get_orders("open")
            recent_orders = paper_bot.get_orders("all", 20)
            current = current_snapshot(clock, positions, open_orders, recent_orders)
            fingerprint = repr(current)
            previous = saved_state.get("snapshot", {})
            if fingerprint != last_auto_fingerprint:
                last_auto_fingerprint = fingerprint
                alert = build_change_alert(previous, current, account, clock, positions, open_orders, recent_orders)
                if alert:
                    await send_codeblock(channel, alert)
            if previous.get("market_open") is True and current.get("market_open") is False:
                today = datetime.now(timezone.utc).date().isoformat()
                if saved_state.get("last_daily_recap_date") != today and bot_config.daily_recap_enabled():
                    await send_codeblock(channel, build_daily_recap(account, clock, positions, open_orders, recent_orders))
                    saved_state["last_daily_recap_date"] = today
            saved_state["snapshot"] = current
            save_auto_state(saved_state)
        except Exception as error:
            print(f"Report loop failed: {error}")
        await asyncio.sleep(bot_config.report_interval_seconds())


def make_client(allowed_user_id):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Discord bot logged in as {client.user}")
        if not getattr(client, "background_tasks_started", False):
            client.background_tasks_started = True
            client.loop.create_task(automation_loop(client))
            client.loop.create_task(report_loop(client))
        if bot_config.startup_notice_enabled() and not getattr(client, "startup_notice_sent", False):
            client.startup_notice_sent = True
            channel_id = report_channel_id()
            channel = client.get_channel(channel_id) if channel_id else None
            if channel:
                await send_codeblock(channel, "\n".join([
                    "AI Paper Trader online",
                    f"Version: {bot_config.git_commit_short()}",
                    f"Autonomous trading: {'RUNNING' if autonomous_trader.is_enabled() else 'STOPPED'}",
                    "Use !start, !stop, !status, !summary, !panic, or !help.",
                ]))

    @client.event
    async def on_message(message):
        if not message.content.startswith(COMMAND_PREFIX):
            return
        parts = message.content.strip().split()
        command = parts[0].lower()
        args = parts[1:]

        if command == "!whoami":
            await message.channel.send(f"Your Discord user ID is `{message.author.id}`")
            return
        if command == "!channelid":
            await message.channel.send(f"This channel ID is `{message.channel.id}`")
            return
        if not allowed(message, allowed_user_id):
            await message.channel.send("You are not allowed to control this bot.")
            return

        try:
            if command == "!help":
                await send_codeblock(message.channel, advanced_help() if args and args[0].lower() == "advanced" else simple_help())
            elif command == "!start":
                await handle_start(message)
            elif command == "!stop":
                await handle_stop(message)
            elif command == "!status":
                await handle_status(message, args)
            elif command == "!summary":
                await handle_summary(message)
            elif command == "!panic":
                await handle_panic(message)
            elif command == "!brief":
                await handle_brief(message)
            elif command == "!pnl":
                await handle_pnl(message)
            elif command == "!positions":
                await handle_positions(message)
            elif command == "!risk":
                await handle_risk(message)
            elif command == "!config":
                await handle_config(message)
            elif command == "!version":
                await handle_version(message)
            elif command == "!reload":
                await handle_reload(message)
            elif command == "!stats":
                await handle_stats(message)
            elif command == "!backtest":
                await handle_backtest(message, args)
            elif command == "!optimize":
                await handle_optimize(message, args)
            elif command == "!journal":
                await handle_journal(message)
            elif command == "!recap":
                await handle_recap(message)
            elif command in {"!suggest", "!analyze"}:
                await handle_suggest(message)
            elif command == "!trade":
                await handle_trade(message, args)
            elif command == "!sell":
                await trade_exit.handle_sell(message, args)
            elif command == "!closeall":
                await trade_exit.handle_closeall(message)
            elif command == "!approve":
                await handle_approve(message, args)
            elif command == "!cancel":
                await handle_cancel(message)
            elif command == "!cancelorder":
                await handle_cancelorder(message, args)
            elif command == "!autotest":
                await handle_autotest(message, client)
            else:
                await message.channel.send("Unknown command. Use `!help`.")
        except Exception as error:
            await message.channel.send(f"Command failed: `{error}`")

    return client


def main():
    token, allowed_user_id = load_settings()
    start_health_server()
    make_client(allowed_user_id).run(token)


if __name__ == "__main__":
    main()
