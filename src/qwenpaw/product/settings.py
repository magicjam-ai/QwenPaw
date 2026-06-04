# -*- coding: utf-8 -*-
"""Small persisted product settings shared by product adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..constant import WORKING_DIR

PRODUCT_STATE_DIR = WORKING_DIR / "product"
PRODUCT_SETTINGS_PATH = PRODUCT_STATE_DIR / "settings.json"


def get_product_settings() -> dict[str, Any]:
    """Read product settings; missing or broken files behave as empty."""
    if not PRODUCT_SETTINGS_PATH.is_file():
        return {}
    try:
        with open(PRODUCT_SETTINGS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_product_settings(settings: dict[str, Any]) -> Path:
    """Persist product settings and return the written path."""
    PRODUCT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(PRODUCT_SETTINGS_PATH, "w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        PRODUCT_SETTINGS_PATH.chmod(0o600)
    except OSError:
        pass
    return PRODUCT_SETTINGS_PATH
