"""Runtime settings. Everything school-specific comes from env vars / .env, never code."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class BillingPolicy(StrEnum):
    """When does an enrollment owe a month's fee?"""

    FULL_MONTH_IF_ENROLLED_ANY_DAY = "full_month_if_enrolled_any_day"
    FULL_MONTH_IF_ENROLLED_ON_FIRST = "full_month_if_enrolled_on_first"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://tuition:tuition@localhost:5432/tuition"

    school_name: str = "Example Learning Center"
    currency: str = "USD"
    billing_due_day: int = 5
    billing_policy: BillingPolicy = BillingPolicy.FULL_MONTH_IF_ENROLLED_ANY_DAY
    reminder_min_balance: Decimal = Decimal("1.00")

    private_data_dir: Path = Path("data/private")


@lru_cache
def get_settings() -> Settings:
    return Settings()
