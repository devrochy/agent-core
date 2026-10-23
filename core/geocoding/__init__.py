"""Geocodificación de direcciones de clientes (HU-CRM-08, ADR-0154)."""

from core.geocoding.service import (
    GeocodingError,
    ResultadoGeocodificacion,
    consumir_cuota,
    disponible,
    geocodificar,
)

__all__ = [
    "GeocodingError",
    "ResultadoGeocodificacion",
    "consumir_cuota",
    "disponible",
    "geocodificar",
]
