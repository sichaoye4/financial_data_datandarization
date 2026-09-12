"""Typed, backend-only configuration for the mapping provider."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Environment-backed settings; secret values are never serialized."""

    model_config = SettingsConfigDict(
        env_file=(REPOSITORY_ROOT / ".env",),
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="DEEPSEEK_MODEL",
    )
    deepseek_thinking: Literal["enabled", "disabled"] = Field(
        default="enabled",
        validation_alias="DEEPSEEK_THINKING",
    )
    deepseek_max_tokens: int = Field(
        default=4096,
        validation_alias="DEEPSEEK_MAX_TOKENS",
        ge=128,
        le=8192,
    )
    deepseek_api_key_file: Path | None = Field(
        default=REPOSITORY_ROOT / "llm_key.txt",
        validation_alias="DEEPSEEK_API_KEY_FILE",
    )

    @property
    def provider_configured(self) -> bool:
        """Check credential availability without opening or returning the secret."""

        if os.getenv("DEEPSEEK_API_KEY", "").strip():
            return True
        return bool(self.deepseek_api_key_file and self.deepseek_api_key_file.is_file())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
