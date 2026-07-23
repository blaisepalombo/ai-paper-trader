from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import paper_bot
from crypto import broker as crypto_broker


def rfc3339(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def stock_bars(symbol, days=2200):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(days))
    params = {
        "timeframe": "1Day",
        "start": rfc3339(start),
        "end": rfc3339(end),
        "limit": 10000,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    }
    bars = []
    page_token = None
    while True:
        query = dict(params)
        if page_token:
            query["page_token"] = page_token
        response = paper_bot.alpaca_data_request("GET", "/stocks/%s/bars?%s" % (symbol, urlencode(query)))
        block = response.get("bars", [])
        if isinstance(block, list):
            bars.extend(block)
        page_token = response.get("next_page_token")
        if not page_token:
            break
    return bars


def crypto_bars(symbol, days=1800):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(days))
    return crypto_broker.historical_bars(
        symbol,
        "1Day",
        rfc3339(start),
        rfc3339(end),
        limit=10000,
    )
