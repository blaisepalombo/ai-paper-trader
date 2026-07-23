import bot_config

DEFAULTS = {
    "paper_only": True,
    "scan_interval_seconds": 900,
    "stock": {
        "universe": ["SPY", "QQQ", "IWM", "DIA"],
        "position_size": 30.0,
        "trend_sma": 200,
        "momentum_lookbacks": [63, 126, 252],
        "momentum_weights": [0.20, 0.30, 0.50],
        "skip_days": 5,
        "emergency_stop_pct": 12.0,
        "history_days": 2200,
        "slippage_bps_per_side": 5.0,
    },
    "crypto": {
        "mode": "aggressive",
        "universe_balanced": ["BTC/USD", "ETH/USD"],
        "universe_aggressive": ["BTC/USD", "ETH/USD", "SOL/USD"],
        "position_size_balanced": 10.0,
        "position_size_aggressive": 15.0,
        "btc_trend_sma": 200,
        "asset_trend_sma": 100,
        "momentum_lookbacks": [30, 90, 180],
        "momentum_weights": [0.20, 0.35, 0.45],
        "skip_days": 3,
        "emergency_stop_pct": 18.0,
        "history_days": 1800,
        "fee_bps_per_side": 25.0,
        "slippage_bps_per_side": 8.0,
    },
}


def all_config():
    raw = bot_config.get_path(bot_config.get_config(force_reload=True), "edge", default={})
    result = dict(DEFAULTS)
    result["stock"] = dict(DEFAULTS["stock"])
    result["crypto"] = dict(DEFAULTS["crypto"])
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in {"stock", "crypto"} and isinstance(value, dict):
                result[key].update(value)
            else:
                result[key] = value
    return result


def get(name, default=None):
    return all_config().get(name, default)


def stock():
    return all_config()["stock"]


def crypto():
    return all_config()["crypto"]


def crypto_mode():
    return str(crypto().get("mode") or "aggressive").lower()


def crypto_universe():
    cfg = crypto()
    key = "universe_aggressive" if crypto_mode() == "aggressive" else "universe_balanced"
    values = cfg.get(key) or []
    return [str(value).upper() for value in values]


def crypto_position_size():
    cfg = crypto()
    key = "position_size_aggressive" if crypto_mode() == "aggressive" else "position_size_balanced"
    return float(cfg.get(key) or 0)
