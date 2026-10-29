"""Geocodificación de direcciones de clientes (HU-CRM-08, ADR-0154)."""

from core.geocoding.service import (
    GeocodingError,
    ResultadoGeocodificacion,
    ResultadoGeocodificacionInversa,
    consumir_cuota,
    disponible,
    geocodificar,
    geocodificar_inverso,
)

__all__ = [
    "GeocodingError",
    "ResultadoGeocodificacion",
    "ResultadoGeocodificacionInversa",
    "consumir_cuota",
    "disponible",
    "geocodificar",
    "geocodificar_inverso",
]
