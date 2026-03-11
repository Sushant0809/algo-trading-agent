"""
Central configuration using pydantic-settings.
Loads from .env file automatically.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Zerodha ---
    kite_api_key: str = Field(default="", description="KiteConnect API key")
    kite_api_secret: str = Field(default="", description="KiteConnect API secret")
    kite_access_token: str = Field(default="", description="Daily access token (auto-refreshed)")

    # --- Zerodha Login (Playwright) ---
    zerodha_user_id: str = Field(default="")
    zerodha_password: str = Field(default="")
    zerodha_totp_secret: str = Field(default="", description="Base32 TOTP secret")

    # --- Anthropic ---
    anthropic_api_key: str = Field(default="")
    anthropic_model: str = Field(default="claude-sonnet-4-6")

    # --- Trading Mode ---
    paper_trading: bool = Field(default=True, description="Route orders to paper simulator")
    trading_mode: Literal["intraday", "swing", "both"] = Field(default="both")

    # --- Capital ---
    paper_trading_capital: float = Field(default=1_000_000.0, description="₹10 lakh virtual capital")
    live_capital_limit: float = Field(default=100_000.0, description="Max real capital")

    # --- Telegram ---
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # --- Logging ---
    log_level: str = Field(default="INFO")
    log_dir: Path = Field(default=Path("./logs"))

    # --- Paths ---
    project_root: Path = Field(default=Path("."))

    @field_validator("log_dir", "project_root", mode="before")
    @classmethod
    def expand_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @property
    def is_paper(self) -> bool:
        return self.paper_trading

    @property
    def run_intraday(self) -> bool:
        return self.trading_mode in ("intraday", "both")

    @property
    def run_swing(self) -> bool:
        return self.trading_mode in ("swing", "both")

    @property
    def token_cache_path(self) -> Path:
        return self.log_dir / ".kite_token_cache"

    @property
    def instrument_cache_path(self) -> Path:
        return self.log_dir / "instruments_cache.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
