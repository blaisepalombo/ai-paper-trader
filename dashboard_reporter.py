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


STATE_FILE = Path(__file__).with_name("dashboard_state.json")
DISCORD_API = "https://discord.com/api/v10"


def load_runtime_settings():
    paper_bot.load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_REPORT_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        raise RuntimeError("Missing Discord bot token or report channel ID in .env.")
    return token, channel_id


def dashboard_enabled():
    config = bot_config.get_config(force_reload=True)
    value = bot_config.get_path(config, "reports", "dashboard_enabled", default=True)
    return bot_config.as_bool(value, True)


def dashboard_pin_enabled():
    config = bot_config.get_config(force_reload=True)
    value = bot_config.get_path(config, "reports", "dashboard_pin_enabled", default=True)
    return bot_config.as_bool(value, True)


def read_state():
    if not STATE_FILE.exists():
        return {}
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def discord_request(token, method, path, body=None, allow_not_found=False):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        f"{DISCORD_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "AI-Paper-Trader/2.0",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord API error {error.code}: {details}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach Discord: {error}") from error


def money(value):
    return paper_bot.money(value)


def build_dashboard():
    state = autonomous_trader.load_state()
    clock = paper_bot.get_clock()
    positions = paper_bot.get_positions()
    open_orders = paper_bot.get_orders("open")

    closed = state.get("closed_trades", [])
    realized_total = sum(float(item.get("estimated_pl") or 0) for item in closed)
    unrealized = sum(float(item.get("unrealized_pl") or 0) for item in positions)
    estimated_value = bot_config.virtual_capital() + realized_total + unrealized
    today_pl = float(state.get("realized_pl_today") or 0) + unrealized

    running = bool(state.get("enabled"))
    market_open = bool(clock.get("is_open"))
    mode = "RUNNING" if running else "STOPPED"
    market = "OPEN" if market_open else "CLOSED"

    lines = [
        "AI PAPER TRADER DASHBOARD",
        "=========================",
        f"Bot: {mode}     Market: {market}",
        f"Paper balance: {money(estimated_value)}",
        f"Today's P/L: {money(today_pl)}",
        f"Open positions: {len(positions)}     Open orders: {len(open_orders)}",
        f"Trades today: {state.get('trades_today', 0)}/{autonomous_trader.cfg_int('max_trades_per_day', 3)}",
    ]

    lines.append("")
    lines.append("POSITIONS")
    if positions:
        for position in positions:
            symbol = position.get("symbol", "?")
            value = money(position.get("market_value"))
            pl = money(position.get("unrealized_pl"))
            plpc = paper_bot.percent(float(position.get("unrealized_plpc") or 0) * 100)
            lines.append(f"- {symbol}: {value} | P/L {pl} ({plpc})")
    else:
        lines.append("- None")

    lines.append("")
    if running and market_open:
        activity = state.get("last_decision") or "Scanning for the next qualified trade."
    elif running:
        activity = "Waiting for the market to open."
    elif positions:
        activity = "New entries are stopped. Existing positions are still being managed."
    else:
        activity = "Automation is stopped. Use !start when ready."

    lines.append("WHAT IT IS DOING")
    lines.append(activity)
    lines.append("")
    lines.append(f"Last scan: {state.get('last_scan_at') or 'not yet'}")
    lines.append(f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"Version: {bot_config.git_commit_short()}")
    lines.append("")
    lines.append("Main commands: !start  !stop  !status  !summary  !panic  !help")

    return "```text\n" + "\n".join(lines)[:1850] + "\n```"


def create_dashboard(token, channel_id, content):
    message = discord_request(
        token,
        "POST",
        f"/channels/{channel_id}/messages",
        {"content": content},
    )
    message_id = str(message.get("id") or "")
    if not message_id:
        raise RuntimeError("Discord did not return a dashboard message ID.")
    return message_id


def update_dashboard(token, channel_id, message_id, content):
    return discord_request(
        token,
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        {"content": content},
        allow_not_found=True,
    )


def try_pin(token, channel_id, message_id):
    if not dashboard_pin_enabled():
        return
    try:
        discord_request(token, "PUT", f"/channels/{channel_id}/pins/{message_id}")
    except RuntimeError as error:
        print(f"Dashboard pin skipped: {error}")


def run():
    if not dashboard_enabled():
        print("Dashboard disabled in bot_config.json.")
        return

    token, channel_id = load_runtime_settings()
    content = build_dashboard()
    state = read_state()
    message_id = str(state.get("message_id") or "")

    if message_id:
        updated = update_dashboard(token, channel_id, message_id, content)
        if updated is not None:
            print(f"Dashboard updated: {message_id}")
            return

    message_id = create_dashboard(token, channel_id, content)
    state["message_id"] = message_id
    state["channel_id"] = channel_id
    write_state(state)
    try_pin(token, channel_id, message_id)
    print(f"Dashboard created: {message_id}")


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"Dashboard failed: {error}", file=sys.stderr)
        sys.exit(1)
