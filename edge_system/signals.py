from . import common


def stock_target(bars_by_symbol, settings):
    universe = [str(symbol).upper() for symbol in settings.get("universe", [])]
    trend_sma = int(settings.get("trend_sma", 200))
    lookbacks = settings.get("momentum_lookbacks", [63, 126, 252])
    weights = settings.get("momentum_weights", [0.20, 0.30, 0.50])
    skip = int(settings.get("skip_days", 5))

    spy = bars_by_symbol.get("SPY", [])
    spy_closes = [common.number(bar, "c") for bar in spy]
    spy_score, spy_returns = common.weighted_momentum(spy_closes, lookbacks, weights, skip)
    spy_sma = common.sma(spy_closes, trend_sma)
    if spy_score is None or spy_sma is None:
        return None, [], "Not enough completed SPY daily history."
    if spy_closes[-1] <= spy_sma or spy_returns[-1] <= 0:
        return None, [], "SPY is below its long-term trend or has negative 12-month momentum; stay in cash."

    candidates = []
    for symbol in universe:
        bars = bars_by_symbol.get(symbol, [])
        closes = [common.number(bar, "c") for bar in bars]
        score, returns = common.weighted_momentum(closes, lookbacks, weights, skip)
        trend = common.sma(closes, trend_sma)
        if score is None or trend is None:
            continue
        latest = closes[-1]
        eligible = latest > trend and returns[1] > 0 and returns[2] > 0
        item = {
            "symbol": symbol,
            "latest": latest,
            "trend_sma": trend,
            "score": score,
            "returns": returns,
            "eligible": eligible,
        }
        candidates.append(item)

    eligible = [item for item in candidates if item["eligible"]]
    eligible.sort(key=lambda item: item["score"], reverse=True)
    if not eligible:
        return None, candidates, "No stock ETF passed both absolute-trend and medium/long momentum filters."
    best = eligible[0]
    return best["symbol"], candidates, "%s has the strongest weighted 3/6/12-month momentum while above its 200-day trend." % best["symbol"]


def crypto_target(bars_by_symbol, settings, universe):
    universe = [str(symbol).upper() for symbol in universe]
    btc_sma_length = int(settings.get("btc_trend_sma", 200))
    asset_sma_length = int(settings.get("asset_trend_sma", 100))
    lookbacks = settings.get("momentum_lookbacks", [30, 90, 180])
    weights = settings.get("momentum_weights", [0.20, 0.35, 0.45])
    skip = int(settings.get("skip_days", 3))

    btc_bars = bars_by_symbol.get("BTC/USD", [])
    btc_closes = [common.number(bar, "c") for bar in btc_bars]
    btc_trend = common.sma(btc_closes, btc_sma_length)
    btc_90 = common.momentum_return(btc_closes, 90, skip)
    if btc_trend is None or btc_90 is None:
        return None, [], "Not enough completed BTC daily history."
    if btc_closes[-1] <= btc_trend or btc_90 <= 0:
        return None, [], "BTC is below its 200-day trend or has negative 90-day momentum; stay in cash."

    candidates = []
    for symbol in universe:
        bars = bars_by_symbol.get(symbol, [])
        closes = [common.number(bar, "c") for bar in bars]
        score, returns = common.weighted_momentum(closes, lookbacks, weights, skip)
        trend = common.sma(closes, asset_sma_length)
        volatility = common.annualized_volatility(closes, window=30, periods=365)
        if score is None or trend is None:
            continue
        latest = closes[-1]
        eligible = latest > trend and returns[1] > 0 and returns[2] > 0
        # Small volatility penalty prevents the ranking from automatically
        # favoring the wildest coin while preserving the momentum signal.
        adjusted_score = score / max(volatility or 1.0, 0.35)
        item = {
            "symbol": symbol,
            "latest": latest,
            "trend_sma": trend,
            "raw_score": score,
            "score": adjusted_score,
            "returns": returns,
            "volatility": volatility,
            "eligible": eligible,
        }
        candidates.append(item)

    eligible = [item for item in candidates if item["eligible"]]
    eligible.sort(key=lambda item: item["score"], reverse=True)
    if not eligible:
        return None, candidates, "No crypto asset passed the BTC regime, trend, and medium/long momentum filters."
    best = eligible[0]
    return best["symbol"], candidates, "%s has the strongest volatility-adjusted 1/3/6-month momentum in a healthy BTC regime." % best["symbol"]
