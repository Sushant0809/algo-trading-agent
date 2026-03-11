"""
Load risk parameters from risk_params.yaml.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=1)
def load_risk_params() -> dict[str, Any]:
    path = Path(__file__).parent / "risk_params.yaml"
    return yaml.safe_load(path.read_text())


@lru_cache(maxsize=1)
def load_strategy_params() -> dict[str, Any]:
    path = Path(__file__).parent / "strategy_params.yaml"
    return yaml.safe_load(path.read_text())
