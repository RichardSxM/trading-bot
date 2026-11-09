"""Settings of the liquidity trap scalper (spec 0031 AC-2).

They live in their own module, because ``settings.py`` is in every strategy's code hash (AC-1). Every
field has a default, so no deploy can fail on a missing value. ``live_trading_enabled`` may never be
true: the lab's refusal turns a true value into exit 78. The road to live stays feature 19.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LtsSettings(BaseSettings):
    """The ``LIVE_TRADING_ENABLED`` lock and the feed health limits of the liquidity trap scalper."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # A bool plus a refusal, because the text "false" from the environment would fail a Literal[False].
    live_trading_enabled: bool = False
    lts_book_max_age_ms: int = Field(default=500, gt=0)
    lts_max_processing_lag_ms: int = Field(default=250, gt=0)
    lts_trade_silence_s: int = Field(default=30, gt=0)
    lts_instrument_meta_max_age_s: int = Field(default=3600, gt=0)
