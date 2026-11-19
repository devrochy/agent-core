"""Geocodificación de la dirección del cliente con un proveedor externo.

El proveedor por defecto es LocationIQ (ADR-0154): permite almacenar la respuesta, tiene
5.000 llamadas/día gratis y cubre LatAm con OpenStreetMap. La API key es de la plataforma y
los negocios no la ven. El servicio **solo** envía dirección, municipio, departamento y país;
nunca nombre, teléfono, correo ni documento. Los logs no llevan la dirección.

Los topes diarios viven en Redis (global y por negocio). Si Redis no responde se falla
cerrado: es preferible no geocodificar a gastar sin control.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from core.geocoding.config import get_geocoding_settings
from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

PROVEEDOR_LOCATIONIQ = "locationiq"
_TTL_CUOTA = 90_000  # 25 h: cubre el cambio de día sin depender de la zona horaria
_PRECISION = {
    "house": "exacta",
    "building": "exacta",
    "street": "calle",
    "road": "calle",
    "residential": "calle",
    "neighbourhood": "barrio",
    "suburb": "barrio",
    "city": "ciudad",
    "town": "ciudad",
    "village": "ciudad",
    "county": "municipio",
    "state": "departamento",
    "country": "país",
}


class GeocodingError(Exception):
    """Error de negocio de la geocodificación, con `status` y `code` estables para la API."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResultadoGeocodificacion:
    latitud: float
    longitud: float
    direccion_normalizada: str | None
    precision: str | None
    proveedor: str


@dataclass(frozen=True)
class ResultadoGeocodificacionInversa:
    """Dirección propuesta a partir de unas coordenadas (mapa táctil de la ficha)."""

    latitud: float
    longitud: float
    direccion: str | None
    municipio: str | None
    departamento: str | None
    pais: str | None
    precision: str | None
    proveedor: str


def disponible() -> bool:
    settings = get_geocoding_settings()
    return settings.provider == PROVEEDOR_LOCATIONIQ and bool(settings.api_key)


def _clave_del_dia(tenant_id: uuid.UUID | None) -> list[tuple[str, int]]:
    settings = get_geocoding_settings()
    dia = datetime.now(UTC).strftime("%Y%m%d")
    claves = [(f"geocoding:global:{dia}", settings.max_global_por_dia)]
    if tenant_id is not None:
        claves.append((f"geocoding:tenant:{tenant_id}:{dia}", settings.max_por_dia))
    return claves


async def consumir_cuota(tenant_id: uuid.UUID) -> None:
    """Suma una llamada a los contadores del día; si se pasa del tope, 429.

    Se incrementa primero el global y después el del negocio. Si el segundo falla, el
    global ya quedó consumido (el lado seguro: no se regala cupo).
    """
    for clave, tope in _clave_del_dia(tenant_id):
        if tope <= 0:
            raise GeocodingError(429, "geocoding_cuota", "La geocodificación está deshabilitada hoy.")
        try:
            redis = get_redis()
            usadas = await redis.incr(clave)
            if usadas == 1:
                await redis.expire(clave, _TTL_CUOTA)
        except GeocodingError:
            raise
        except Exception as exc:
            log.warning("geocoding_cuota_no_disponible", tenant_id=str(tenant_id))
            raise GeocodingError(
                503, "geocoding_no_disponible", "No se pudo verificar la cuota de geocodificación."
            ) from exc
        if usadas > tope:
            raise GeocodingError(429, "geocoding_cuota", "Se agotó la geocodificación de hoy. Intenta mañana.")


def _precision(tipo: str | None) -> str | None:
    if not tipo:
        return None
    return _PRECISION.get(tipo.lower(), tipo)


async def geocodificar(direccion: str, *, pais: str | None = None) -> ResultadoGeocodificacion:
    """Convierte una dirección en coordenadas. Solo se le pasa la dirección al proveedor."""
    settings = get_geocoding_settings()
    if not disponible():
        raise GeocodingError(
            503, "geocoding_no_configurado", "La geocodificación no está configurada en la plataforma."
        )
    consulta = " ".join(str(direccion or "").split())
    if not consulta:
        raise GeocodingError(400, "geocoding_direccion_vacia", "Escribe una dirección para geocodificar.")

    params: dict[str, str | int] = {
        "key": settings.api_key,
        "q": consulta,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
        "accept-language": "es",
    }
    country = (pais or settings.country or "").strip().lower()
    if country:
        params["countrycodes"] = country

    url = f"{settings.base_url.rstrip('/')}/search"
    inicio = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            respuesta = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        log.warning("geocoding_timeout", provider=settings.provider)
        raise GeocodingError(
            504, "geocoding_timeout", "El proveedor de mapas tardó demasiado. Intenta de nuevo."
        ) from exc
    except httpx.HTTPError as exc:
        log.warning("geocoding_proveedor_error", provider=settings.provider, error=type(exc).__name__)
        raise GeocodingError(
            502, "geocoding_proveedor", "No se pudo consultar el proveedor de mapas."
        ) from exc

    latencia = int((time.monotonic() - inicio) * 1000)
    if respuesta.status_code == 429:
        log.warning("geocoding_cuota_proveedor", provider=settings.provider, latency_ms=latencia)
        raise GeocodingError(
            429, "geocoding_cuota", "El proveedor de mapas no acepta más consultas por hoy."
        )
    if respuesta.status_code in (404, 400):
        log.info("geocoding_sin_resultado", provider=settings.provider, latency_ms=latencia)
        raise GeocodingError(
            422, "geocoding_no_encontrada", "No pudimos ubicar la dirección. Revisa los datos o complétalos."
        )
    if respuesta.status_code != 200:
        log.warning(
            "geocoding_respuesta",
            provider=settings.provider,
            status=respuesta.status_code,
            latency_ms=latencia,
        )
        raise GeocodingError(502, "geocoding_proveedor", "El proveedor de mapas respondió con un error.")

    try:
        datos = respuesta.json()
    except ValueError as exc:
        raise GeocodingError(502, "geocoding_proveedor", "El proveedor de mapas respondió mal.") from exc
    if not isinstance(datos, list) or not datos:
        log.info("geocoding_sin_resultado", provider=settings.provider, latency_ms=latencia)
        raise GeocodingError(
            422, "geocoding_no_encontrada", "No pudimos ubicar la dirección. Revisa los datos o complétalos."
        )

    primero = datos[0] if isinstance(datos[0], dict) else {}
    try:
        latitud = float(primero["lat"])
        longitud = float(primero["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodingError(502, "geocoding_proveedor", "El proveedor de mapas respondió mal.") from exc

    log.info(
        "geocoding_llamado",
        provider=settings.provider,
        status=200,
        latency_ms=latencia,
        precision=_precision(primero.get("type")),
    )
    return ResultadoGeocodificacion(
        latitud=latitud,
        longitud=longitud,
        direccion_normalizada=primero.get("display_name"),
        precision=_precision(primero.get("type")),
        proveedor=settings.provider,
    )


def _texto_direccion(valor: object) -> str | None:
    texto = " ".join(str(valor or "").split())
    return texto or None


async def geocodificar_inverso(latitud: float, longitud: float) -> ResultadoGeocodificacionInversa:
    """Convierte coordenadas en una dirección propuesta (mapa táctil de la ficha).

    Solo se envían latitud y longitud al proveedor. El resultado no se escribe: el
    panel/la APK lo propone y la persona decide si lo usa antes de guardar.
    """
    settings = get_geocoding_settings()
    if not disponible():
        raise GeocodingError(
            503, "geocoding_no_configurado", "La geocodificación no está configurada en la plataforma."
        )
    try:
        lat = float(latitud)
        lon = float(longitud)
    except (TypeError, ValueError) as exc:
        raise GeocodingError(400, "geocoding_coordenadas_invalidas", "Las coordenadas no son válidas.") from exc
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise GeocodingError(400, "geocoding_coordenadas_invalidas", "Las coordenadas no son válidas.")

    params: dict[str, str | int] = {
        "key": settings.api_key,
        "lat": lat,
        "lon": lon,
        "format": "json",
        "addressdetails": 1,
        "accept-language": "es",
        "zoom": 18,
    }
    url = f"{settings.base_url.rstrip('/')}/reverse"
    inicio = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            respuesta = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        log.warning("geocoding_inverso_timeout", provider=settings.provider)
        raise GeocodingError(
            504, "geocoding_timeout", "El proveedor de mapas tardó demasiado. Intenta de nuevo."
        ) from exc
    except httpx.HTTPError as exc:
        log.warning("geocoding_inverso_proveedor_error", provider=settings.provider, error=type(exc).__name__)
        raise GeocodingError(
            502, "geocoding_proveedor", "No se pudo consultar el proveedor de mapas."
        ) from exc

    latencia = int((time.monotonic() - inicio) * 1000)
    if respuesta.status_code == 429:
        log.warning("geocoding_inverso_cuota_proveedor", provider=settings.provider, latency_ms=latencia)
        raise GeocodingError(
            429, "geocoding_cuota", "El proveedor de mapas no acepta más consultas por hoy."
        )
    if respuesta.status_code in (404, 400):
        log.info("geocoding_inverso_sin_resultado", provider=settings.provider, latency_ms=latencia)
        raise GeocodingError(
            422, "geocoding_no_encontrada", "No pudimos ubicar una dirección en ese punto."
        )
    if respuesta.status_code != 200:
        log.warning(
            "geocoding_inverso_respuesta",
            provider=settings.provider,
            status=respuesta.status_code,
            latency_ms=latencia,
        )
        raise GeocodingError(502, "geocoding_proveedor", "El proveedor de mapas respondió con un error.")

    try:
        datos = respuesta.json()
    except ValueError as exc:
        raise GeocodingError(502, "geocoding_proveedor", "El proveedor de mapas respondió mal.") from exc
    if not isinstance(datos, dict) or not datos:
        log.info("geocoding_inverso_sin_resultado", provider=settings.provider, latency_ms=latencia)
        raise GeocodingError(
            422, "geocoding_no_encontrada", "No pudimos ubicar una dirección en ese punto."
        )

    direccion_datos = datos.get("address") if isinstance(datos.get("address"), dict) else {}
    via = " ".join(
        parte
        for parte in (
            _texto_direccion(direccion_datos.get("road")),
            _texto_direccion(direccion_datos.get("house_number")),
        )
        if parte
    )
    municipio = next(
        (
            _texto_direccion(direccion_datos.get(clave))
            for clave in ("city", "town", "village", "municipality", "county")
            if _texto_direccion(direccion_datos.get(clave))
        ),
        None,
    )
    departamento = _texto_direccion(
        direccion_datos.get("state") or direccion_datos.get("state_district")
    )
    pais = _texto_direccion(direccion_datos.get("country"))
    direccion = via or _texto_direccion(datos.get("display_name"))
    if direccion and len(direccion) > 300:
        direccion = direccion[:300].rstrip()
    if municipio and len(municipio) > 120:
        municipio = municipio[:120].rstrip()
    if departamento and len(departamento) > 120:
        departamento = departamento[:120].rstrip()
    if pais and len(pais) > 60:
        pais = pais[:60].rstrip()
    precision = _precision(datos.get("addresstype") or datos.get("type"))
    try:
        lat_resultado = float(datos["lat"])
        lon_resultado = float(datos["lon"])
    except (KeyError, TypeError, ValueError):
        lat_resultado, lon_resultado = lat, lon

    log.info(
        "geocoding_inverso_llamado",
        provider=settings.provider,
        status=200,
        latency_ms=latencia,
        precision=precision,
    )
    return ResultadoGeocodificacionInversa(
        latitud=lat_resultado,
        longitud=lon_resultado,
        direccion=direccion,
        municipio=municipio,
        departamento=departamento,
        pais=pais,
        precision=precision,
        proveedor=settings.provider,
    )
