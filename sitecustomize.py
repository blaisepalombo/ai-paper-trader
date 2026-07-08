"""Runtime overrides for local paper-trading settings.

Python imports sitecustomize automatically at startup when this file is on
sys.path. The bot uses os.environ.setdefault() when loading .env, so values set
here take priority without editing secret .env files on Oracle.
"""

import json
import os
from pathlib import Path


RISK_SETTINGS_FILE = Path(__file__).with_name("risk_settings.json")
RISK_KEYS = {"STOP_LOSS_PCT", "TAKE_PROFIT_PCT"}


def load_risk_settings():
    if not RISK_SETTINGS_FILE.exists():
        return

    try:
        settings = json.loads(RISK_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    for key in RISK_KEYS:
        value = settings.get(key)
        if value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if numeric_value <= 0:
            continue
        os.environ[key] = str(numeric_value)


load_risk_settings()
