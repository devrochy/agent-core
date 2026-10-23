"""Configuración de la geocodificación (HU-CRM-08, ADR-0152).

Vive aparte de `core.config.Settings` porque es una integración opcional: no cambia el
contrato del core ni el `repr` de la configuración principal.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GeocodingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    provider: Literal["none", "locationiq"] = Field(default="none", alias="GEOCODING_PROVIDER")
    api_key: str = Field(default="", alias="GEOCODING_API_KEY")
    base_url: str = Field(default="https://us1.locationiq.com/v1", alias="GEOCODING_BASE_URL")
    country: str = Field(default="co", alias="GEOCODING_COUNTRY")
    timeout_seconds: float = Field(default=5.0, alias="GEOCODING_TIMEOUT_SECONDS")
    max_por_dia: int = Field(default=200, alias="GEOCODING_MAX_POR_DIA")
    max_global_por_dia: int = Field(default=4000, alias="GEOCODING_MAX_GLOBAL_POR_DIA")


@lru_cache
def get_geocoding_settings() -> GeocodingSettings:
    return GeocodingSettings()
