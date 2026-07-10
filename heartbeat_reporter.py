import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import autonomous_trader
import bot_config
import paper_bot


def load_settings():
    paper_bot.load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_REPORT_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        raise RuntimeError("Missing Discord token or report channel ID in .env.")
    return token, channel_id


def heartbeat_enabled():
    config = bot_config.get_config(force_reload=True)
    value = bot_config.get_path(config, "reports", "market_heartbeat_enabled", default=True)
    return bot_config.as_bool(value, True)


def build_message():
    state = autonomous_trader.load_state()
    clock = paper_bot.get_clock()

    if not heartbeat_enabled():
        return None
    if not state.get("enabled"):
        return None
    if not clock.get("is_open"):
        return None

    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")
    unrealized = sum(float(position.get("unrealized_pl") or 0) for position in positions)
    realized = float(state.get("realized_pl_today") or 0)
    estimated_value = bot_config.virtual_capital() + realized + unrealized

    lines = [
        "AI Paper Trader Check-In",
        "Status: RUNNING",
        f"Estimated experiment value: {paper_bot.money(estimated_value)}",
        f"Today P/L: {paper_bot.money(realized + unrealized)}",
        f"Open positions: {len(positions)}",
        f"Open orders: {len(open_orders)}",
        f"Trades today: {state.get('trades_today', 0)}/{autonomous_trader.cfg_int('max_trades_per_day', 3)}",
    ]

    if positions:
        lines.append("")
        for position in positions:
            symbol = position.get("symbol", "?")
            value = paper_bot.money(position.get("market_value"))
            pl = paper_bot.money(position.get("unrealized_pl"))
            plpc = paper_bot.percent(float(position.get("unrealized_plpc") or 0) * 100)
            lines.append(f"- {symbol}: value {value}, P/L {pl} ({plpc})")
    else:
        lines.append("Current position: none")

    decision = state.get("last_decision") or "Waiting for the next scan."
    lines.extend(["", f"What it is doing: {decision}"])
    return "```text\n" + "\n".join(lines) + "\n```"


def send_message(token, channel_id, content):
    body = json.dumps({"content": content}).encode("utf-8")
    request = Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "AI-Paper-Trader/1.0",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"Discord returned HTTP {response.status}.")
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord rejected heartbeat: {error.code} {details}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach Discord: {error}") from error


def main():
    try:
        token, channel_id = load_settings()
        message = build_message()
        if message:
            send_message(token, channel_id, message)
            print("Heartbeat sent.")
        else:
            print("Heartbeat skipped: disabled, automation stopped, or market closed.")
    except Exception as error:
        print(f"Heartbeat failed: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
