import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import autonomous_trader
import bot_config
import paper_bot


STATE_FILE = Path(__file__).with_name("daily_recap_state.json")
DISCORD_API = "https://discord.com/api/v10"


def today_utc():
    return datetime.now(timezone.utc).date().isoformat()


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_runtime_settings():
    paper_bot.load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_REPORT_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        raise RuntimeError("Missing Discord bot token or report channel ID in .env.")
    return token, channel_id


def recap_enabled():
    return bot_config.daily_recap_enabled()


def send_message(token, channel_id, content):
    body = json.dumps({"content": content}).encode("utf-8")
    request = Request(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "AI-Paper-Trader/2.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"Discord returned HTTP {response.status}.")
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord rejected daily recap: {error.code} {details}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach Discord: {error}") from error


def build_recap():
    state = autonomous_trader.load_state()
    positions = paper_bot.get_positions()

    realized = float(state.get("realized_pl_today") or 0)
    unrealized = sum(float(position.get("unrealized_pl") or 0) for position in positions)
    today_pl = realized + unrealized

    closed_today = [
        trade for trade in state.get("closed_trades", [])
        if str(trade.get("time", "")).startswith(today_utc())
    ]
    estimated_value = bot_config.virtual_capital() + sum(
        float(trade.get("estimated_pl") or 0) for trade in state.get("closed_trades", [])
    ) + unrealized

    if int(state.get("trades_today") or 0) == 0:
        takeaway = "No trades today. The bot stayed patient."
    elif today_pl > 0:
        takeaway = "The bot finished the day ahead."
    elif today_pl < 0:
        takeaway = "The bot finished the day down and kept the loss limited."
    else:
        takeaway = "The bot finished about even."

    holdings = ", ".join(str(position.get("symbol", "?")) for position in positions) or "None"

    lines = [
        "AI Paper Trader Daily Recap",
        f"Ending value: {paper_bot.money(estimated_value)}",
        f"Today's P/L: {paper_bot.money(today_pl)}",
        f"Trades opened: {state.get('trades_today', 0)}",
        f"Trades closed: {len(closed_today)}",
        f"Still holding: {holdings}",
        "",
        takeaway,
    ]
    return "```text\n" + "\n".join(lines) + "\n```"


def run():
    if not recap_enabled():
        print("Daily recap disabled.")
        return

    token, channel_id = load_runtime_settings()
    clock = paper_bot.get_clock()
    market_open = bool(clock.get("is_open"))
    today = today_utc()
    state = load_state()

    if market_open:
        state["saw_market_open_date"] = today
        state["last_market_open"] = True
        save_state(state)
        print("Market is open. Daily recap not due yet.")
        return

    saw_open_today = state.get("saw_market_open_date") == today
    already_sent = state.get("last_recap_date") == today
    just_closed = state.get("last_market_open") is True

    if not already_sent and saw_open_today and just_closed:
        send_message(token, channel_id, build_recap())
        state["last_recap_date"] = today
        print("Daily recap sent.")
    else:
        print("Daily recap not due.")

    state["last_market_open"] = False
    save_state(state)


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"Daily recap failed: {error}", file=sys.stderr)
        sys.exit(1)
