import asyncio
import os
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

try:
    import discord
except ImportError:
    print("Missing discord.py. Run install_discord_windows.bat first.")
    sys.exit(1)

import paper_bot


COMMAND_PREFIX = "!"
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
        print("Run the bot after adding the token, type !whoami in Discord, then paste that ID into .env.")

    return token, allowed_user_id


def auto_reports_enabled():
    return os.environ.get("AUTO_REPORTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def report_interval_seconds():
    raw = os.environ.get("REPORT_INTERVAL_SECONDS", "300").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 300


def report_channel_id():
    value = os.environ.get("DISCORD_REPORT_CHANNEL_ID", "").strip()
    if not value or value == "put_your_discord_report_channel_id_here":
        return None
    return int(value)


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


def get_report():
    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    recent_orders = paper_bot.get_orders("all", 10)
    report = paper_bot.build_report(account, clock, positions, open_orders, recent_orders)
    paper_bot.REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    return report, account, clock, positions, open_orders, recent_orders


def next_action_from_report(report):
    marker = "Next Action:"
    if marker not in report:
        return report
    return report.split(marker, 1)[1].strip()


def build_short_update(account, clock, positions, open_orders):
    lines = [
        "AI Paper Trader Update",
        f"Market open: {clock.get('is_open')}",
        f"Open positions: {len(positions)}",
        f"Open orders: {len(open_orders)}",
    ]

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
        lines.append("Next: hold and monitor.")
    elif not clock.get("is_open"):
        lines.append("Next: wait for market open.")
    else:
        lines.append("Next: run !analyze before staging a trade.")

    return "\n".join(lines)


def status_fingerprint(clock, positions, open_orders):
    position_bits = [
        (
            position.get("symbol"),
            position.get("qty"),
            position.get("market_value"),
            position.get("unrealized_pl"),
        )
        for position in positions
    ]
    order_bits = [
        (
            order.get("id"),
            order.get("symbol"),
            order.get("status"),
            order.get("filled_qty"),
        )
        for order in open_orders
    ]
    return repr((clock.get("is_open"), position_bits, order_bits))


async def send_codeblock(channel, text):
    for chunk in split_message(text):
        await channel.send(f"```text\n{chunk}\n```")


async def handle_status(message):
    report, *_ = get_report()
    await send_codeblock(message.channel, report)


async def handle_suggest(message):
    report, *_ = get_report()
    await send_codeblock(message.channel, f"Suggested next action:\n{next_action_from_report(report)}")


async def handle_analyze(message):
    try:
        analysis = paper_bot.analyze_market()
    except RuntimeError as error:
        analysis = f"Analysis failed:\n{error}"
    await send_codeblock(message.channel, analysis)


async def handle_brief(message):
    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    await send_codeblock(message.channel, build_short_update(account, clock, positions, open_orders))


async def handle_channelid(message):
    await message.channel.send(f"This channel ID is `{message.channel.id}`")


async def handle_autotest(message):
    channel_id = report_channel_id()
    if channel_id is None:
        await message.channel.send("No `DISCORD_REPORT_CHANNEL_ID` is set yet. Run `!channelid`, put that number in `.env`, then restart the bot.")
        return

    channel = message.client.get_channel(channel_id)
    if channel is None:
        await message.channel.send("I could not find the report channel. Check `DISCORD_REPORT_CHANNEL_ID` and restart the bot.")
        return

    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    await send_codeblock(channel, build_short_update(account, clock, positions, open_orders))
    await message.channel.send("Sent a test auto-report.")


async def handle_trade(message, args):
    global pending_trade

    if len(args) < 1:
        await message.channel.send("Use `!trade SPY 5` to request a tiny paper trade.")
        return

    symbol = args[0].upper()
    dollars_text = args[1] if len(args) >= 2 else "5"

    try:
        dollars = float(dollars_text)
    except ValueError:
        await message.channel.send("Dollar amount must be a number. Example: `!trade SPY 5`")
        return

    account = paper_bot.get_account()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")

    if symbol not in paper_bot.ALLOWED_SYMBOLS:
        await message.channel.send(f"`{symbol}` is not allowed yet. Allowed: `{', '.join(sorted(paper_bot.ALLOWED_SYMBOLS))}`")
        return
    if dollars <= 0 or dollars > paper_bot.MAX_DOLLARS_PER_TRADE:
        await message.channel.send(f"Safety stop: max trade is `${paper_bot.MAX_DOLLARS_PER_TRADE:.2f}`.")
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
        await message.channel.send("Safety stop: market is closed. Use `!status` to see the next open.")
        return

    open_order_symbols = {order.get("symbol") for order in open_orders}
    position_symbols = {position.get("symbol") for position in positions}
    if symbol in open_order_symbols or symbol in position_symbols:
        await message.channel.send(f"Safety stop: `{symbol}` is already open or pending.")
        return

    code = f"{symbol}-{int(dollars * 100)}"
    pending_trade = {
        "user_id": str(message.author.id),
        "symbol": symbol,
        "dollars": dollars,
        "code": code,
    }

    await message.channel.send(
        f"Paper trade staged: buy `${dollars:.2f}` of `{symbol}`.\n"
        f"To submit it, reply exactly: `!approve {code}`\n"
        "This is Alpaca paper trading only."
    )


async def handle_approve(message, args):
    global pending_trade

    if pending_trade is None:
        await message.channel.send("No pending trade. Use `!trade SPY 5` first.")
        return
    if str(message.author.id) != pending_trade["user_id"]:
        await message.channel.send("Only the user who staged the trade can approve it.")
        return
    if not args or args[0] != pending_trade["code"]:
        await message.channel.send("Approval code does not match.")
        return

    symbol = pending_trade["symbol"]
    dollars = pending_trade["dollars"]
    pending_trade = None

    try:
        result = paper_bot.submit_order(symbol, dollars)
        status = result.get("status", "submitted")
        order_id = result.get("id", "unknown")
        paper_bot.log_trade(symbol, dollars, status, str(result))
        await message.channel.send(f"Submitted paper order. `{symbol}` `${dollars:.2f}`. Status: `{status}`. Order ID: `{order_id}`")
    except RuntimeError as error:
        paper_bot.log_trade(symbol, dollars, "failed", str(error))
        await message.channel.send(f"Order failed: `{error}`")


async def handle_cancel(message):
    global pending_trade
    pending_trade = None
    await message.channel.send("Pending staged trade canceled.")


def help_text():
    return """Commands:
!help - show commands
!whoami - show your Discord user ID
!channelid - show this channel's ID for auto reports
!status - show full bot report
!brief - show short bot report
!suggest - show next recommended action
!analyze - score the watchlist and suggest wait/hold/stage trade
!trade SPY 5 - stage a $5 paper trade
!approve CODE - approve staged trade
!cancel - cancel staged trade
!autotest - send a test auto-report to the configured report channel

Safety:
- Paper trading only
- Max $5 per trade
- One open order max
- One bot-submitted order per day
- Only the allowed Discord user can control it
"""


async def auto_report_loop(client):
    global last_auto_fingerprint

    await client.wait_until_ready()
    if not auto_reports_enabled():
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

    print(f"Auto reports enabled for channel {channel_id}.")

    while not client.is_closed():
        try:
            account = paper_bot.get_account()
            clock = paper_bot.get_clock()
            positions = paper_bot.get_positions()
            open_orders = paper_bot.get_orders("open")
            fingerprint = status_fingerprint(clock, positions, open_orders)

            if fingerprint != last_auto_fingerprint:
                last_auto_fingerprint = fingerprint
                await send_codeblock(channel, build_short_update(account, clock, positions, open_orders))
        except Exception as error:
            print(f"Auto report failed: {error}")

        await asyncio.sleep(report_interval_seconds())


def make_client(allowed_user_id):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Discord bot logged in as {client.user}")
        print("Type !help in your Discord channel.")
        if not getattr(client, "auto_report_task_started", False):
            client.auto_report_task_started = True
            client.loop.create_task(auto_report_loop(client))

    @client.event
    async def on_message(message):
        if not message.content.startswith(COMMAND_PREFIX):
            return

        command_line = message.content.strip()
        parts = command_line.split()
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

        if command == "!help":
            await send_codeblock(message.channel, help_text())
        elif command == "!channelid":
            await handle_channelid(message)
        elif command == "!status":
            await handle_status(message)
        elif command == "!brief":
            await handle_brief(message)
        elif command == "!suggest":
            await handle_suggest(message)
        elif command == "!analyze":
            await handle_analyze(message)
        elif command == "!trade":
            await handle_trade(message, args)
        elif command == "!approve":
            await handle_approve(message, args)
        elif command == "!cancel":
            await handle_cancel(message)
        elif command == "!autotest":
            await handle_autotest(message)
        else:
            await message.channel.send("Unknown command. Try `!help`.")

    return client


def main():
    token, allowed_user_id = load_settings()
    start_health_server()
    client = make_client(allowed_user_id)
    client.run(token)


if __name__ == "__main__":
    main()
