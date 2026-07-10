import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

try:
    import discord
except ImportError:
    print("Missing discord.py. Install requirements first.")
    sys.exit(1)

import bot_config
import paper_bot
import trade_exit


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
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health server running on port {port}")


def load_settings():
    paper_bot.load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    allowed_user_id = os.environ.get("DISCORD_ALLOWED_USER_ID", "").strip()

    if not token or token == "put_your_discord_bot_token_here":
        print("Missing DISCORD_BOT_TOKEN in .env.")
        sys.exit(1)
    if not allowed_user_id or allowed_user_id == "put_your_discord_user_id_here":
        print("Missing DISCORD_ALLOWED_USER_ID in .env.")
        print("Type !whoami in Discord, then paste that ID into .env.")

    return token, allowed_user_id


def report_channel_id():
    value = os.environ.get("DISCORD_REPORT_CHANNEL_ID", "").strip()
    if not value or value == "put_your_discord_report_channel_id_here":
        return None
    return int(value)


def utc_today():
    return datetime.now(timezone.utc).date().isoformat()


def load_auto_state():
    if not AUTO_STATE_FILE.exists():
        return {}
    try:
        return json.loads(AUTO_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_auto_state(state):
    AUTO_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def allowed(message, allowed_user_id):
    if message.author.bot:
        return False
    if not allowed_user_id or allowed_user_id == "put_your_discord_user_id_here":
        return message.content.strip().lower() in {"!whoami", "!help"}
    return str(message.author.id) == allowed_user_id


def split_message(text, max_len=1800):
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


async def send_codeblock(channel, text):
    for chunk in split_message(text):
        await channel.send(f"```text\n{chunk}\n```")


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
    exposure_pct = 0.0
    if paper_bot.VIRTUAL_CAPITAL:
        exposure_pct = (summary["open_value"] / paper_bot.VIRTUAL_CAPITAL) * 100
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
        lines.append(f"{symbol}: value {value}, P/L {pl} ({plpc})")

    for order in open_orders:
        lines.append(paper_bot.order_label(order))

    if open_orders:
        lines.append("Next: wait for the open order.")
    elif positions:
        lines.append("Next: hold, monitor, or use !sell SYMBOL.")
    elif not clock.get("is_open"):
        lines.append("Next: wait for market open.")
    else:
        lines.append("Next: run !analyze before staging a trade.")

    return "\n".join(lines)


def build_daily_recap(account, clock, positions, open_orders, recent_orders):
    lines = [
        "AI Paper Trader Daily Recap",
        f"Version: {bot_config.git_commit_short()}",
        f"Report time UTC: {datetime.now(timezone.utc).isoformat()}",
        f"Market open: {clock.get('is_open')}",
        f"Open positions: {len(positions)}",
        f"Open orders: {len(open_orders)}",
        build_portfolio_summary(positions),
    ]

    if positions:
        lines.append("")
        lines.append("Positions:")
        for position in positions:
            symbol = position.get("symbol", "?")
            value = float(position.get("market_value") or 0)
            pl = float(position.get("unrealized_pl") or 0)
            plpc = float(position.get("unrealized_plpc") or 0) * 100
            lines.append(f"- {symbol}: value {paper_bot.money(value)}, P/L {paper_bot.money(pl)} ({paper_bot.percent(plpc)})")
    else:
        lines.append("")
        lines.append("Positions: none")

    if recent_orders:
        lines.append("")
        lines.append("Recent orders:")
        for order in recent_orders[:5]:
            lines.append(f"- {paper_bot.order_label(order)}")

    return "\n".join(lines)


def build_risk_settings_report(positions=None):
    config = bot_config.get_config(force_reload=True)
    sl_pct = bot_config.stop_loss_percent(config)
    tp_pct = bot_config.take_profit_percent(config)
    auto_exit = trade_exit.automatic_exits_enabled()
    lines = [
        "AI Paper Trader Risk Settings",
        f"Version: {bot_config.git_commit_short()}",
        f"Stop-loss threshold: {sl_pct:g}% below entry",
        f"Take-profit threshold: {tp_pct:g}% above entry",
        f"Automatic exits enabled: {auto_exit}",
        "Manual selling always requires an approval code.",
    ]

    if positions:
        lines.append("")
        lines.append("Current position threshold areas:")
        for position in positions:
            symbol = position.get("symbol", "?")
            avg = float(position.get("avg_entry_price") or 0)
            if avg <= 0:
                continue
            stop_price = avg * (1 - sl_pct / 100)
            target_price = avg * (1 + tp_pct / 100)
            lines.append(f"- {symbol}: stop {paper_bot.money(stop_price)}, target {paper_bot.money(target_price)}")

    return "\n".join(lines)


def order_state(order):
    return {
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "status": order.get("status"),
        "filled_qty": order.get("filled_qty"),
        "filled_avg_price": order.get("filled_avg_price"),
    }


def current_auto_state(clock, positions, open_orders, recent_orders):
    return {
        "market_open": bool(clock.get("is_open")),
        "position_symbols": sorted(str(position.get("symbol")) for position in positions if position.get("symbol")),
        "recent_orders": {str(order.get("id")): order_state(order) for order in recent_orders[:20] if order.get("id")},
    }


def build_change_alert(previous, current, account, clock, positions, open_orders, recent_orders):
    lines = []

    previous_market = previous.get("market_open")
    current_market = current.get("market_open")
    if previous_market is False and current_market is True:
        lines.append("Market opened.")
    elif previous_market is True and current_market is False:
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
        lines.append(f"New open position: {symbol}")
    for symbol in sorted(previous_positions - current_positions):
        lines.append(f"Position closed: {symbol}")

    if not lines:
        return None

    lines.append("")
    lines.append(build_short_update(account, clock, positions, open_orders))
    return "\n".join(lines)


def risk_alert_keys(positions):
    keys = set()
    sl_pct = bot_config.stop_loss_percent()
    tp_pct = bot_config.take_profit_percent()
    for position in positions:
        symbol = position.get("symbol")
        avg = float(position.get("avg_entry_price") or 0)
        current = float(position.get("current_price") or 0)
        if not symbol or avg <= 0 or current <= 0:
            continue
        if current <= avg * (1 - sl_pct / 100):
            keys.add(f"{symbol}:stop")
        if current >= avg * (1 + tp_pct / 100):
            keys.add(f"{symbol}:target")
    return keys


def build_risk_alerts(positions, saved_state):
    previous_keys = set(saved_state.get("risk_alert_keys", []))
    current_keys = risk_alert_keys(positions)
    new_keys = current_keys - previous_keys
    saved_state["risk_alert_keys"] = sorted(current_keys)
    if not new_keys:
        return None

    sl_pct = bot_config.stop_loss_percent()
    tp_pct = bot_config.take_profit_percent()
    lines = ["AI Paper Trader Risk Alert"]
    for position in positions:
        symbol = position.get("symbol")
        avg = float(position.get("avg_entry_price") or 0)
        current = float(position.get("current_price") or 0)
        if not symbol or avg <= 0 or current <= 0:
            continue
        stop_price = avg * (1 - sl_pct / 100)
        target_price = avg * (1 + tp_pct / 100)
        if f"{symbol}:stop" in new_keys:
            lines.append(f"- {symbol} reached stop threshold: current {paper_bot.money(current)}, stop {paper_bot.money(stop_price)}")
        if f"{symbol}:target" in new_keys:
            lines.append(f"- {symbol} reached take-profit threshold: current {paper_bot.money(current)}, target {paper_bot.money(target_price)}")
    if trade_exit.automatic_exits_enabled():
        lines.append("Automatic exits are enabled. A paper sell may be submitted.")
    else:
        lines.append("Alert only. Automatic exits are disabled.")
    return "\n".join(lines)


async def handle_status(message):
    report, *_ = get_report()
    await send_codeblock(message.channel, report)


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
    report = bot_config.config_report()
    report += f"\n- Automatic exits enabled: {trade_exit.automatic_exits_enabled()}"
    await send_codeblock(message.channel, report)


async def handle_version(message):
    await send_codeblock(message.channel, bot_config.version_report())


async def handle_reload(message):
    bot_config.reload_config()
    paper_bot.reload_config()
    await send_codeblock(message.channel, "Config reloaded.\n\n" + bot_config.config_report())


async def handle_journal(message):
    rows = paper_bot.read_trade_log(limit=15)
    recent_orders = paper_bot.get_orders("all", 15)
    lines = ["AI Paper Trader Journal"]

    if rows:
        lines.append("")
        lines.append("Local trade log:")
        for row in rows:
            lines.append(
                f"- {row.get('timestamp_utc', '?')}: {row.get('symbol', '?')} "
                f"${row.get('dollars', '?')}, status {row.get('status', '?')}"
            )
    else:
        lines.append("Local trade log: none yet")

    if recent_orders:
        lines.append("")
        lines.append("Recent Alpaca orders:")
        for order in recent_orders:
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
    report, *_ = get_report()
    marker = "Next Action:"
    suggestion = report.split(marker, 1)[1].strip() if marker in report else report
    await send_codeblock(message.channel, f"Suggested next action:\n{suggestion}")


async def handle_analyze(message):
    try:
        analysis = paper_bot.analyze_market()
    except RuntimeError as error:
        analysis = f"Analysis failed:\n{error}"
    await send_codeblock(message.channel, analysis)


async def handle_autotest(message, client):
    channel_id = report_channel_id()
    if channel_id is None:
        await message.channel.send("No DISCORD_REPORT_CHANNEL_ID is configured.")
        return
    channel = client.get_channel(channel_id)
    if channel is None:
        await message.channel.send("The configured report channel could not be found.")
        return

    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    recent_orders = paper_bot.get_orders("all", 10)
    await send_codeblock(channel, build_short_update(account, clock, positions, open_orders))
    if bot_config.daily_recap_enabled():
        await send_codeblock(channel, build_daily_recap(account, clock, positions, open_orders, recent_orders))
    await message.channel.send("Sent a test auto-report.")


async def handle_cancelorder(message, args):
    open_orders = paper_bot.get_orders("open")
    if not open_orders:
        await message.channel.send("No open Alpaca paper orders to cancel.")
        return

    if args and args[0].lower() == "all":
        targets = open_orders
    elif args:
        requested = args[0]
        matches = [
            order for order in open_orders
            if str(order.get("id", "")).startswith(requested)
            or str(order.get("symbol", "")).upper() == requested.upper()
        ]
        if not matches:
            await message.channel.send("No matching open order. Use !status or !cancelorder all.")
            return
        targets = matches[:1]
    else:
        targets = open_orders[:1]

    lines = ["Cancel order request:"]
    for order in targets:
        order_id = order.get("id")
        symbol = order.get("symbol", "?")
        try:
            paper_bot.cancel_order(order_id)
            paper_bot.log_trade(symbol, 0, "canceled", f"cancel requested for order_id={order_id}")
            lines.append(f"- Cancel requested for {symbol}: {order_id}")
        except RuntimeError as error:
            lines.append(f"- Could not cancel {symbol}: {error}")
    await send_codeblock(message.channel, "\n".join(lines))


async def handle_trade(message, args):
    global pending_trade

    if not args:
        await message.channel.send("Use `!trade SPY 5` to stage a tiny paper buy.")
        return

    symbol = args[0].upper()
    dollars_text = args[1] if len(args) > 1 else str(paper_bot.MAX_DOLLARS_PER_TRADE)
    try:
        dollars = float(dollars_text)
    except ValueError:
        await message.channel.send("Dollar amount must be a number.")
        return

    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")

    if symbol not in paper_bot.ALLOWED_SYMBOLS:
        await message.channel.send(f"{symbol} is not allowed. Allowed: {', '.join(sorted(paper_bot.ALLOWED_SYMBOLS))}")
        return
    if dollars <= 0 or dollars > paper_bot.MAX_DOLLARS_PER_TRADE:
        await message.channel.send(f"Safety stop: max trade is ${paper_bot.MAX_DOLLARS_PER_TRADE:.2f}.")
        return
    if len(positions) >= paper_bot.MAX_OPEN_POSITIONS:
        await message.channel.send("Safety stop: max open positions reached.")
        return
    if len(open_orders) >= paper_bot.MAX_OPEN_ORDERS:
        await message.channel.send("Safety stop: there is already an open order.")
        return
    if paper_bot.already_submitted_today():
        await message.channel.send("Safety stop: the bot already submitted an order today.")
        return
    if not clock.get("is_open"):
        await message.channel.send("Safety stop: market is closed.")
        return

    open_order_symbols = {order.get("symbol") for order in open_orders}
    position_symbols = {position.get("symbol") for position in positions}
    if symbol in open_order_symbols or symbol in position_symbols:
        await message.channel.send(f"Safety stop: {symbol} is already open or pending.")
        return

    code = f"BUY-{symbol}-{int(dollars * 100)}"
    pending_trade = {
        "user_id": str(message.author.id),
        "symbol": symbol,
        "dollars": dollars,
        "code": code,
    }
    await message.channel.send(
        f"Paper buy staged: `${dollars:.2f}` of `{symbol}`.\n"
        f"Approve with: `!approve {code}`\n"
        "This affects Alpaca paper trading only."
    )


async def handle_approve(message, args):
    global pending_trade

    handled_exit = await trade_exit.handle_approve(message, args)
    if handled_exit:
        return

    if pending_trade is None:
        await message.channel.send("No matching pending action. Stage one with !trade, !sell, or !closeall.")
        return
    if str(message.author.id) != pending_trade["user_id"]:
        await message.channel.send("Only the user who staged the trade can approve it.")
        return
    if not args or args[0] != pending_trade["code"]:
        await message.channel.send("Approval code does not match.")
        return

    action = pending_trade
    pending_trade = None
    try:
        result = paper_bot.submit_order(action["symbol"], action["dollars"])
        status = result.get("status", "submitted")
        order_id = result.get("id", "unknown")
        paper_bot.log_trade(action["symbol"], action["dollars"], status, str(result))
        await message.channel.send(
            f"Submitted paper buy: `{action['symbol']}` `${action['dollars']:.2f}`. "
            f"Status: `{status}`. Order ID: `{order_id}`"
        )
    except RuntimeError as error:
        paper_bot.log_trade(action["symbol"], action["dollars"], "failed", str(error))
        await message.channel.send(f"Buy failed: `{error}`")


async def handle_cancel(message):
    global pending_trade
    had_buy = pending_trade is not None
    pending_trade = None
    had_exit = trade_exit.cancel_pending()
    if had_buy or had_exit:
        await message.channel.send("Pending staged action canceled.")
    else:
        await message.channel.send("There was no pending staged action.")


def help_text():
    return """Commands:
!help - show commands
!whoami - show your Discord user ID
!channelid - show this channel ID
!status - full bot report
!brief - short report
!pnl - portfolio P/L summary
!positions - show open positions, quantities, prices, and P/L
!risk - show stop, target, and automatic-exit status
!config - show active bot configuration
!version - show deployed GitHub commit
!reload - reload bot_config.json
!journal - show trade log and recent Alpaca orders
!recap - show daily recap
!suggest - show next suggested action
!analyze - analyze the watchlist
!trade SPY 5 - stage a $5 paper buy
!sell SPY - stage a sale of the entire SPY position
!sell SPY all - same as !sell SPY
!sell SPY 50% - stage a sale of half the SPY position
!sell SPY 0.004 - stage an exact-share sale
!closeall - stage market sells for every open position
!approve CODE - approve a staged buy, sell, or close-all action
!cancel - cancel any staged action
!cancelorder - cancel the newest unfilled paper order
!cancelorder all - cancel every unfilled paper order
!autotest - send a test report

Safety:
- Alpaca paper trading only
- Buys, manual sells, and close-all require approval
- Automatic exits exist but are disabled by default
- Market orders are blocked while the market is closed
- Only the allowed Discord user can control the bot
"""


async def send_startup_notice(client):
    if not bot_config.startup_notice_enabled():
        return
    channel_id = report_channel_id()
    if channel_id is None:
        return
    channel = client.get_channel(channel_id)
    if channel is None:
        return
    await send_codeblock(channel, "\n".join([
        "AI Paper Trader online",
        f"Version: {bot_config.git_commit_short()}",
        f"Stop-loss threshold: {bot_config.stop_loss_percent():g}%",
        f"Take-profit threshold: {bot_config.take_profit_percent():g}%",
        f"Automatic exits enabled: {trade_exit.automatic_exits_enabled()}",
        "Config source: bot_config.json",
    ]))


async def auto_report_loop(client):
    global last_auto_fingerprint

    await client.wait_until_ready()
    if not bot_config.auto_reports_enabled():
        print("Auto reports disabled.")
        return

    channel_id = report_channel_id()
    if channel_id is None:
        print("Auto reports not started: DISCORD_REPORT_CHANNEL_ID is not set.")
        return
    channel = client.get_channel(channel_id)
    if channel is None:
        print(f"Auto reports not started: channel {channel_id} was not found.")
        return

    saved_state = load_auto_state()
    while not client.is_closed():
        try:
            bot_config.reload_config()
            paper_bot.reload_config()

            account = paper_bot.get_account()
            clock = paper_bot.get_clock()
            positions = paper_bot.get_positions()
            open_orders = paper_bot.get_orders("open")
            recent_orders = paper_bot.get_orders("all", 20)
            current_state = current_auto_state(clock, positions, open_orders, recent_orders)
            fingerprint = repr(current_state)
            previous_snapshot = saved_state.get("snapshot", {})

            if fingerprint != last_auto_fingerprint:
                last_auto_fingerprint = fingerprint
                alert = build_change_alert(previous_snapshot, current_state, account, clock, positions, open_orders, recent_orders)
                if alert:
                    await send_codeblock(channel, alert)

                opened_now = previous_snapshot.get("market_open") is False and current_state.get("market_open") is True
                if opened_now and bot_config.auto_analyze_at_open_enabled():
                    await send_codeblock(channel, paper_bot.analyze_market(account, clock, positions, open_orders))

                closed_now = previous_snapshot.get("market_open") is True and current_state.get("market_open") is False
                already_sent = saved_state.get("last_daily_recap_date") == utc_today()
                if closed_now and bot_config.daily_recap_enabled() and not already_sent:
                    await send_codeblock(channel, build_daily_recap(account, clock, positions, open_orders, recent_orders))
                    saved_state["last_daily_recap_date"] = utc_today()

            risk_alert = build_risk_alerts(positions, saved_state)
            if risk_alert:
                await send_codeblock(channel, risk_alert)

            await trade_exit.run_automatic_exits(channel, positions, open_orders)

            saved_state["snapshot"] = current_state
            save_auto_state(saved_state)
        except Exception as error:
            print(f"Auto report failed: {error}")

        await asyncio.sleep(bot_config.report_interval_seconds())


def make_client(allowed_user_id):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Discord bot logged in as {client.user}")
        if not getattr(client, "startup_notice_sent", False):
            client.startup_notice_sent = True
            await send_startup_notice(client)
        if not getattr(client, "auto_report_task_started", False):
            client.auto_report_task_started = True
            client.loop.create_task(auto_report_loop(client))

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
                await send_codeblock(message.channel, help_text())
            elif command == "!status":
                await handle_status(message)
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
            elif command == "!journal":
                await handle_journal(message)
            elif command == "!recap":
                await handle_recap(message)
            elif command == "!suggest":
                await handle_suggest(message)
            elif command == "!analyze":
                await handle_analyze(message)
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
                await message.channel.send("Unknown command. Try `!help`.")
        except RuntimeError as error:
            await message.channel.send(f"Command failed: `{error}`")
        except Exception as error:
            print(f"Command error: {error}")
            await message.channel.send("The command failed unexpectedly. Check the Oracle service logs.")

    return client


def main():
    token, allowed_user_id = load_settings()
    start_health_server()
    client = make_client(allowed_user_id)
    client.run(token)


if __name__ == "__main__":
    main()
