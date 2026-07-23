import bot_config

DEFAULTS = {
    "enabled": False,
    "mode": "aggressive",
    "watchlist": ["BTC/USD", "ETH/USD", "SOL/USD"],
    "scan_interval_seconds": 300,
    "signal_timeframe": "1Hour",
    "risk_timeframe": "15Min",
    "history_days": 120,
    "position_size_balanced": 5.0,
    "position_size_aggressive": 10.0,
    "max_positions_balanced": 1,
    "max_positions_aggressive": 2,
    "max_entries_24h_balanced": 3,
    "max_entries_24h_aggressive": 5,
    "loss_limit_24h_balanced": 1.0,
    "loss_limit_24h_aggressive": 2.0,
    "cooldown_minutes_balanced": 30,
    "cooldown_minutes_aggressive": 15,
    "minimum_score_balanced": 7,
    "minimum_score_aggressive": 6,
    "stop_atr_multiple": 2.0,
    "target_atr_multiple": 3.5,
    "trailing_atr_multiple": 1.5,
    "trail_arm_atr_multiple": 1.5,
    "max_hold_hours": 72,
    "fee_bps_per_side": 25.0,
    "slippage_bps_per_side": 8.0,
    "paper_only": True,
}

def all_config():
    raw = bot_config.get_path(bot_config.get_config(force_reload=True), "crypto", default={})
    out = dict(DEFAULTS)
    if isinstance(raw, dict): out.update(raw)
    return out

def get(name): return all_config().get(name, DEFAULTS.get(name))
def mode(): return str(get("mode") or "aggressive").lower()
def position_size(): return float(get("position_size_aggressive" if mode()=="aggressive" else "position_size_balanced"))
def max_positions(): return int(get("max_positions_aggressive" if mode()=="aggressive" else "max_positions_balanced"))
def max_entries_24h(): return int(get("max_entries_24h_aggressive" if mode()=="aggressive" else "max_entries_24h_balanced"))
def loss_limit_24h(): return float(get("loss_limit_24h_aggressive" if mode()=="aggressive" else "loss_limit_24h_balanced"))
def cooldown_minutes(): return int(get("cooldown_minutes_aggressive" if mode()=="aggressive" else "cooldown_minutes_balanced"))
def minimum_score(): return int(get("minimum_score_aggressive" if mode()=="aggressive" else "minimum_score_balanced"))
