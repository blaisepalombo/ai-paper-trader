import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import bot_config
import trading_database


LOG_FILE = Path("trade_log.csv")
REPORT_FILE = Path("daily_report.txt")


def reload_config():
    global VIRTUAL_CAPITAL
    global MAX_DOLLARS_PER_TRADE
    global MAX_OPEN_POSITIONS
    global MAX_OPEN_ORDERS
    global ONE_BOT_ORDER_PER_DAY
    global ALLOWED_SYMBOLS
    global ANALYZE_WATCHLIST

    config = bot_config.reload_config()
    VIRTUAL_CAPITAL = bot_config.virtual_capital(config)
    MAX_DOLLARS_PER_TRADE = bot_config.max_dollars_per_trade(config)
    MAX_OPEN_POSITIONS = bot_config.max_open_positions(config)
    MAX_OPEN_ORDERS = bot_config.max_open_orders(config)
    ONE_BOT_ORDER_PER_DAY = bot_config.one_bot_order_per_day(config)
    ALLOWED_SYMBOLS = bot_config.allowed_symbols(config)
    ANALYZE_WATCHLIST = bot_config.analyze_watchlist(config)
    return config


reload_config()
try:
    trading_database.initialize()
except Exception as error:
    print(f"Database initialization warning: {error}")


def load_env(path=".env"):
    env_path = Path(path)
    if not env_path.exists():
        required = ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_API_BASE_URL"]
        if all(os.environ.get(key) for key in required):
            reload_config()
            return
        print("Missing .env file. Copy .env.example, rename it to .env, then add your paper keys.")
        print("On cloud hosting, set the same values as environment variables instead.")
        sys.exit(1)

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

    reload_config()


def alpaca_request(method, path, body=None):
    base_url = os.environ.get("APCA_API_BASE_URL", "").rstrip("/")
    return signed_request(method, base_url, path, body=body, require_paper=True)


def alpaca_data_request(method, path, body=None):
    base_url = os.environ.get("APCA_DATA_BASE_URL", "https://data.alpaca.markets/v2").rstrip("/")
    return signed_request(method, base_url, path, body=body, require_paper=False)


def signed_request(method, base_url, path, body=None, require_paper=False):
    key_id = os.environ.get("APCA_API_KEY_ID", "")
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "")

    if require_paper and "paper-api.alpaca.markets" not in base_url:
        raise RuntimeError("Safety stop: this bot only runs on Alpaca paper trading.")
    if not key_id or not secret_key:
        raise RuntimeError("Missing Alpaca paper API key or secret in .env.")

    data = None
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request = Request(f"{base_url}{path}", data=data, headers=headers, method=method)

    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        raw = error.read().decode("utf-8")
        raise RuntimeError(f"Alpaca rejected the request: {error.code} {raw}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach Alpaca: {error}") from error


def get_account():
    return alpaca_request("GET", "/account")


def get_clock():
    return alpaca_request("GET", "/clock")


def get_positions():
    return alpaca_request("GET", "/positions")


def get_orders(status="open", limit=50):
    query = urlencode({"status": status, "limit": limit, "direction": "desc"})
    return alpaca_request("GET", f"/orders?{query}")


def cancel_order(order_id):
    return alpaca_request("DELETE", f"/orders/{order_id}")


def get_daily_bars(symbol, limit=60):
    query = urlencode({
        "timeframe": "1Day",
        "limit": limit,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    })
    return alpaca_data_request("GET", f"/stocks/{symbol}/bars?{query}")


def submit_order(symbol, dollars):
    order = {
        "symbol": symbol,
        "notional": str(round(dollars, 2)),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }
    return alpaca_request("POST", "/orders", order)


def today_utc():
    return datetime.now(timezone.utc).date().isoformat()


def money(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def percent(value):
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def already_submitted_today():
    if not ONE_BOT_ORDER_PER_DAY:
        return False

    if not LOG_FILE.exists():
        return False

    with LOG_FILE.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            timestamp = row.get("timestamp_utc", "")
            status = row.get("status", "")
            if timestamp.startswith(today_utc()) and status not in {"canceled", "failed"}:
                return True
    return False


def log_trade(symbol, dollars, status, details):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if new_file:
            writer.writerow(["timestamp_utc", "symbol", "dollars", "status", "details"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            symbol,
            dollars,
            status,
            details,
        ])


def read_trade_log(limit=10):
    if not LOG_FILE.exists():
        return []

    with LOG_FILE.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return rows[-limit:]


def pick_symbol(positions):
    held_symbols = {position.get("symbol") for position in positions}
    for symbol in ANALYZE_WATCHLIST:
        if symbol not in held_symbols:
            return symbol
    return "SPY"


def sma(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def analyze_symbol(symbol):
    data = get_daily_bars(symbol, limit=60)
    bars = data.get("bars", [])
    closes = [float(bar["c"]) for bar in bars if "c" in bar]

    if len(closes) < 25:
        return {
            "symbol": symbol,
            "score": 0,
            "decision": "WAIT",
            "reason": "Not enough daily bar data yet.",
        }

    latest = closes[-1]
    previous = closes[-2]
    sma5 = sma(closes, 5)
    sma20 = sma(closes, 20)

    score = 0
    reasons = []

    if latest > sma5:
        score += 1
        reasons.append("price is above 5-day average")
    else:
        reasons.append("price is below 5-day average")

    if sma5 and sma20 and sma5 > sma20:
        score += 1
        reasons.append("5-day average is above 20-day average")
    else:
        reasons.append("5-day average is not above 20-day average")

    if latest > previous:
        score += 1
        reasons.append("latest daily close is up from prior close")
    else:
        reasons.append("latest daily close is not up from prior close")

    decision = "STAGE TRADE" if score >= 3 else "WAIT"
    return {
        "symbol": symbol,
        "score": score,
        "decision": decision,
        "latest": latest,
        "previous": previous,
        "sma5": sma5,
        "sma20": sma20,
        "reason": "; ".join(reasons),
    }


def analyze_market(account=None, clock=None, positions=None, open_orders=None):
    if account is None:
        account = get_account()
    if clock is None:
        clock = get_clock()
    if positions is None:
        positions = get_positions()
    if open_orders is None:
        open_orders = get_orders("open")

    header = []
    header.append("AI Paper Trading Bot Analysis")
    header.append("-----------------------------")
    header.append(f"Report time UTC: {datetime.now(timezone.utc).isoformat()}")
    header.append(f"Watchlist: {', '.join(ANALYZE_WATCHLIST)}")
    header.append("")

    if open_orders:
        header.append("Decision: WAIT")
        header.append("Reason: There is already an open order. Do not submit another trade.")
        return "\n".join(header)
    if positions:
        symbols = ", ".join(position.get("symbol", "?") for position in positions)
        header.append("Decision: HOLD")
        header.append(f"Reason: Existing open position detected: {symbols}.")
        return "\n".join(header)
    if already_submitted_today():
        header.append("Decision: WAIT")
        header.append("Reason: The bot already submitted an order today.")
        return "\n".join(header)
    if not clock.get("is_open"):
        header.append("Decision: WAIT")
        header.append("Reason: Market is closed. Run !analyze after market open.")
        header.append(f"Next market open: {clock.get('next_open')}")
        return "\n".join(header)

    results = []
    errors = []
    for symbol in ANALYZE_WATCHLIST:
        try:
            results.append(analyze_symbol(symbol))
        except RuntimeError as error:
            errors.append(f"{symbol}: {error}")

    if not results:
        header.append("Decision: WAIT")
        header.append("Reason: Could not retrieve market data.")
        if errors:
            header.append("")
            header.append("Errors:")
            header.extend(f"- {error}" for error in errors[:5])
        return "\n".join(header)

    ranked = sorted(results, key=lambda item: item.get("score", 0), reverse=True)
    best = ranked[0]

    header.append("Scores:")
    for result in ranked:
        latest = money(result.get("latest"))
        sma5_text = money(result.get("sma5"))
        sma20_text = money(result.get("sma20"))
        header.append(
            f"- {result['symbol']}: {result['score']}/3, {result['decision']}, "
            f"last {latest}, SMA5 {sma5_text}, SMA20 {sma20_text}"
        )

    header.append("")
    if best.get("score", 0) >= 3:
        header.append("Decision: STAGE TRADE")
        header.append(f"Best candidate: {best['symbol']}")
        header.append(f"Suggested paper trade: ${MAX_DOLLARS_PER_TRADE:.2f}")
        header.append(f"Reason: {best['reason']}")
        header.append(f"Command: !trade {best['symbol']} {MAX_DOLLARS_PER_TRADE:g}")
    else:
        header.append("Decision: WAIT")
        header.append(f"Reason: No watchlist symbol scored 3/3. Best was {best['symbol']} at {best.get('score', 0)}/3.")

    if errors:
        header.append("")
        header.append("Data warnings:")
        header.extend(f"- {error}" for error in errors[:5])

    return "\n".join(header)


def ask_symbol(default_symbol):
    symbol = input(f"Symbol to paper buy, default {default_symbol}: ").strip().upper() or default_symbol
    if symbol not in ALLOWED_SYMBOLS:
        print(f"{symbol} is not on the starter allow-list.")
        print(f"Allowed: {', '.join(sorted(ALLOWED_SYMBOLS))}")
        sys.exit(1)
    return symbol


def ask_dollars():
    raw = input("Dollar amount, default 5: ").strip() or "5"
    try:
        dollars = float(raw)
    except ValueError:
        print("Dollar amount must be a number.")
        sys.exit(1)

    if dollars <= 0:
        print("Dollar amount must be above 0.")
        sys.exit(1)
    if dollars > MAX_DOLLARS_PER_TRADE:
        print(f"Safety stop: max per trade is ${MAX_DOLLARS_PER_TRADE:.2f}.")
        sys.exit(1)
    return dollars


def order_label(order):
    side = order.get("side", "?")
    symbol = order.get("symbol", "?")
    amount = order.get("notional") or order.get("qty") or "?"
    status = order.get("status", "?")
    submitted = order.get("submitted_at", "")
    filled_avg_price = order.get("filled_avg_price")
    filled_qty = order.get("filled_qty")

    fill_text = ""
    if filled_avg_price and filled_qty:
        fill_text = f", filled {filled_qty} at {money(filled_avg_price)}"

    submitted_text = f", submitted {submitted}" if submitted else ""
    return f"{side} {symbol}: {amount}, status {status}{fill_text}{submitted_text}"


def build_report(account, clock, positions, open_orders, recent_orders):
    lines = []
    lines.append("AI Paper Trading Bot Report")
    lines.append("---------------------------")
    lines.append(f"Report time UTC: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Version: {bot_config.git_commit_short()}")
    lines.append(f"Virtual experiment size: {money(VIRTUAL_CAPITAL)}")
    lines.append(f"Max per paper trade: {money(MAX_DOLLARS_PER_TRADE)}")
    lines.append("Mode: Alpaca paper trading only")
    lines.append("")
    lines.append(f"Alpaca status: {account.get('status')}")
    lines.append(f"Paper buying power shown by Alpaca: {money(account.get('buying_power'))}")
    lines.append(f"Market open now: {clock.get('is_open')}")
    lines.append(f"Next market open: {clock.get('next_open')}")
    lines.append(f"Next market close: {clock.get('next_close')}")
    lines.append(f"Open positions: {len(positions)}")
    lines.append(f"Open orders: {len(open_orders)}")
    lines.append("")

    total_market_value = 0.0
    total_unrealized_pl = 0.0
    if positions:
        lines.append("Positions:")
        for position in positions:
            symbol = position.get("symbol")
            qty = position.get("qty")
            market_value = position.get("market_value")
            unrealized_pl = position.get("unrealized_pl")
            unrealized_plpc = position.get("unrealized_plpc")
            avg_entry_price = position.get("avg_entry_price")
            current_price = position.get("current_price")

            try:
                total_market_value += float(market_value)
                total_unrealized_pl += float(unrealized_pl)
            except (TypeError, ValueError):
                pass

            lines.append(
                f"- {symbol}: qty {qty}, avg {money(avg_entry_price)}, "
                f"current {money(current_price)}, value {money(market_value)}, "
                f"unrealized P/L {money(unrealized_pl)} ({percent(float(unrealized_plpc or 0) * 100)})"
            )
        lines.append(f"Total open market value: {money(total_market_value)}")
        lines.append(f"Total open unrealized P/L: {money(total_unrealized_pl)}")
    else:
        lines.append("Positions: none yet")
    lines.append("")

    if open_orders:
        lines.append("Open Orders:")
        for order in open_orders:
            lines.append(f"- {order_label(order)}")
    else:
        lines.append("Open Orders: none")
    lines.append("")

    if recent_orders:
        lines.append("Recent Orders:")
        for order in recent_orders[:5]:
            lines.append(f"- {order_label(order)}")
    else:
        lines.append("Recent Orders: none")
    lines.append("")

    lines.append("Next Action:")
    if open_orders:
        lines.append("- Wait. There is already an open order. Do not submit another trade.")
    elif not clock.get("is_open"):
        lines.append("- Wait. The market is closed. Run the status check after the next open.")
    elif positions:
        lines.append("- Monitor the open position. Do not add another trade until we review it.")
    elif already_submitted_today():
        lines.append("- Wait. The bot already submitted an order today.")
    else:
        lines.append("- Ready for one tiny paper trade if you choose to run the trade bot.")

    return "\n".join(lines)


def print_status(account, clock, positions, open_orders, recent_orders):
    report = build_report(account, clock, positions, open_orders, recent_orders)
    REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\nSaved report to: {REPORT_FILE}")

    if positions:
        print("Positions:")
        for position in positions:
            symbol = position.get("symbol")
            market_value = position.get("market_value")
            unrealized_pl = position.get("unrealized_pl")
            print(f"- {symbol}: value ${market_value}, unrealized P/L ${unrealized_pl}")
        print()

    if open_orders:
        print("Open orders:")
        for order in open_orders:
            symbol = order.get("symbol")
            side = order.get("side")
            notional = order.get("notional") or order.get("qty")
            status = order.get("status")
            print(f"- {side} {symbol}: {notional}, status {status}")
        print()


def main():
    load_env()

    print("\nAI Paper Trading Bot")
    print("--------------------")
    print(f"Virtual experiment size: ${VIRTUAL_CAPITAL:.2f}")
    print(f"Max per paper trade: ${MAX_DOLLARS_PER_TRADE:.2f}")
    print("Mode: Alpaca paper trading only\n")

    action = (sys.argv[1] if len(sys.argv) > 1 else "trade").strip().lower()

    account = get_account()
    clock = get_clock()
    positions = get_positions()
    open_orders = get_orders("open")
    recent_orders = get_orders("all", 10)

    print_status(account, clock, positions, open_orders, recent_orders)

    if action in {"status", "check"}:
        print("Status check only. No order sent.")
        return
    if action == "analyze":
        print()
        print(analyze_market(account, clock, positions, open_orders))
        print("Analysis only. No order sent.")
        return

    if len(positions) >= MAX_OPEN_POSITIONS:
        print(f"Safety stop: already at max open positions ({MAX_OPEN_POSITIONS}).")
        sys.exit(0)
    if len(open_orders) >= MAX_OPEN_ORDERS:
        print("Safety stop: there is already an open order. Wait for it to fill or cancel it.")
        sys.exit(0)
    if already_submitted_today():
        print("Safety stop: the bot already submitted a non-failed order today.")
        sys.exit(0)
    if not clock.get("is_open"):
        print("Safety stop: market is closed. Run this again when the market is open.")
        sys.exit(0)

    default_symbol = pick_symbol(positions)
    symbol = ask_symbol(default_symbol)
    open_order_symbols = {order.get("symbol") for order in open_orders}
    position_symbols = {position.get("symbol") for position in positions}
    if symbol in open_order_symbols or symbol in position_symbols:
        print(f"Safety stop: {symbol} is already open or pending.")
        sys.exit(0)
    dollars = ask_dollars()

    print("\nProposed paper order:")
    print(f"Buy ${dollars:.2f} of {symbol}")
    print("Order type: market")
    print("This is fake paper trading, but treat it like a real test.\n")

    approval = input("Type YES to submit this paper order: ").strip()
    if approval != "YES":
        print("Canceled. No order sent.")
        log_trade(symbol, dollars, "canceled", "User did not type YES")
        return

    try:
        result = submit_order(symbol, dollars)
        order_id = result.get("id", "unknown")
        status = result.get("status", "submitted")
        print(f"\nSubmitted. Order ID: {order_id}")
        print(f"Status: {status}")
        log_trade(symbol, dollars, status, json.dumps(result))
    except RuntimeError as error:
        print(f"\nOrder failed: {error}")
        log_trade(symbol, dollars, "failed", str(error))


if __name__ == "__main__":
    main()
