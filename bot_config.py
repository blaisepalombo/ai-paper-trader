import copy
import json
import subprocess
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "bot_config.json"

DEFAULT_CONFIG = {
    "risk": {
        "stop_loss_pct": 0.25,
        "take_profit_pct": 0.25,
        "mode": "alert_only",
    },
    "reports": {
        "auto_reports_enabled": True,
        "report_interval_seconds": 300,
        "daily_recap_enabled": True,
        "auto_analyze_at_open": True,
        "startup_notice_enabled": True,
    },
    "trading": {
        "virtual_capital": 50.00,
        "max_dollars_per_trade": 5.00,
        "max_open_positions": 3,
        "max_open_orders": 1,
        "one_bot_order_per_day": True,
    },
    "symbols": {
        "allowed": ["AAPL", "AMZN", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA", "VOO"],
        "analyze_watchlist": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"],
    },
}

_config_cache = None


def deep_merge(base, override):
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    if not CONFIG_FILE.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_CONFIG)

    return deep_merge(DEFAULT_CONFIG, loaded)


def get_config(force_reload=False):
    global _config_cache
    if force_reload or _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def reload_config():
    return get_config(force_reload=True)


def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return reload_config()


def get_path(config, *path, default=None):
    current = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def as_float(value, default, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)

    if minimum is not None:
        number = max(float(minimum), number)
    if maximum is not None:
        number = min(float(maximum), number)
    return number


def as_int(value, default, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)

    if minimum is not None:
        number = max(int(minimum), number)
    if maximum is not None:
        number = min(int(maximum), number)
    return number


def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def stop_loss_percent(config=None):
    config = config or get_config()
    return as_float(get_path(config, "risk", "stop_loss_pct", default=0.25), 0.25, minimum=0.01)


def take_profit_percent(config=None):
    config = config or get_config()
    return as_float(get_path(config, "risk", "take_profit_pct", default=0.25), 0.25, minimum=0.01)


def report_interval_seconds(config=None):
    config = config or get_config()
    return as_int(get_path(config, "reports", "report_interval_seconds", default=300), 300, minimum=60)


def auto_reports_enabled(config=None):
    config = config or get_config()
    return as_bool(get_path(config, "reports", "auto_reports_enabled", default=True), True)


def daily_recap_enabled(config=None):
    config = config or get_config()
    return as_bool(get_path(config, "reports", "daily_recap_enabled", default=True), True)


def auto_analyze_at_open_enabled(config=None):
    config = config or get_config()
    return as_bool(get_path(config, "reports", "auto_analyze_at_open", default=True), True)


def startup_notice_enabled(config=None):
    config = config or get_config()
    return as_bool(get_path(config, "reports", "startup_notice_enabled", default=True), True)


def virtual_capital(config=None):
    config = config or get_config()
    return as_float(get_path(config, "trading", "virtual_capital", default=50.00), 50.00, minimum=1)


def max_dollars_per_trade(config=None):
    config = config or get_config()
    return as_float(get_path(config, "trading", "max_dollars_per_trade", default=5.00), 5.00, minimum=0.01)


def max_open_positions(config=None):
    config = config or get_config()
    return as_int(get_path(config, "trading", "max_open_positions", default=3), 3, minimum=1)


def max_open_orders(config=None):
    config = config or get_config()
    return as_int(get_path(config, "trading", "max_open_orders", default=1), 1, minimum=1)


def one_bot_order_per_day(config=None):
    config = config or get_config()
    return as_bool(get_path(config, "trading", "one_bot_order_per_day", default=True), True)


def allowed_symbols(config=None):
    config = config or get_config()
    values = get_path(config, "symbols", "allowed", default=DEFAULT_CONFIG["symbols"]["allowed"])
    if not isinstance(values, list):
        values = DEFAULT_CONFIG["symbols"]["allowed"]
    return {str(symbol).upper().strip() for symbol in values if str(symbol).strip()}


def analyze_watchlist(config=None):
    config = config or get_config()
    values = get_path(config, "symbols", "analyze_watchlist", default=DEFAULT_CONFIG["symbols"]["analyze_watchlist"])
    if not isinstance(values, list):
        values = DEFAULT_CONFIG["symbols"]["analyze_watchlist"]
    return [str(symbol).upper().strip() for symbol in values if str(symbol).strip()]


def git_value(args, default="unknown"):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=APP_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return default

    value = result.stdout.strip()
    return value or default


def git_commit_short():
    return git_value(["rev-parse", "--short", "HEAD"])


def git_commit_full():
    return git_value(["rev-parse", "HEAD"])


def git_branch():
    return git_value(["rev-parse", "--abbrev-ref", "HEAD"])


def version_report():
    return "\n".join([
        "AI Paper Trader Version",
        f"Branch: {git_branch()}",
        f"Commit: {git_commit_short()}",
        f"Full commit: {git_commit_full()}",
        f"Config file: {CONFIG_FILE}",
    ])


def config_report():
    config = get_config(force_reload=True)
    lines = [
        "AI Paper Trader Config",
        f"Source: {CONFIG_FILE}",
        "",
        "Risk:",
        f"- Stop-loss alert: {stop_loss_percent(config):g}% below entry",
        f"- Take-profit alert: {take_profit_percent(config):g}% above entry",
        f"- Mode: {get_path(config, 'risk', 'mode', default='alert_only')}",
        "",
        "Reports:",
        f"- Auto reports: {auto_reports_enabled(config)}",
        f"- Report interval: {report_interval_seconds(config)} seconds",
        f"- Daily recap: {daily_recap_enabled(config)}",
        f"- Analyze at open: {auto_analyze_at_open_enabled(config)}",
        f"- Startup notice: {startup_notice_enabled(config)}",
        "",
        "Trading safety:",
        f"- Virtual capital: ${virtual_capital(config):.2f}",
        f"- Max dollars per trade: ${max_dollars_per_trade(config):.2f}",
        f"- Max open positions: {max_open_positions(config)}",
        f"- Max open orders: {max_open_orders(config)}",
        f"- One bot order per day: {one_bot_order_per_day(config)}",
        "",
        "Symbols:",
        f"- Allowed: {', '.join(sorted(allowed_symbols(config)))}",
        f"- Analyze watchlist: {', '.join(analyze_watchlist(config))}",
    ]
    return "\n".join(lines)
