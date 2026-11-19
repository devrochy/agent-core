"""Geocodificación de la dirección del cliente (HU-CRM-08, ADR-0154)."""

from __future__ import annotations

import pytest

from core.geocoding import service
from core.geocoding.config import get_geocoding_settings


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "locationiq")
    monkeypatch.setenv("GEOCODING_API_KEY", "test-key")
    monkeypatch.setenv("GEOCODING_BASE_URL", "https://us1.locationiq.com/v1")
    monkeypatch.setenv("GEOCODING_COUNTRY", "co")
    monkeypatch.setenv("GEOCODING_MAX_POR_DIA", "2")
    monkeypatch.setenv("GEOCODING_MAX_GLOBAL_POR_DIA", "10")
    get_geocoding_settings.cache_clear()
    yield
    get_geocoding_settings.cache_clear()


class _Respuesta:
    def __init__(self, status_code: int, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _Cliente:
    def __init__(self, respuesta: _Respuesta, capturado: dict):
        self.respuesta = respuesta
        self.capturado = capturado

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url: str, params: dict):
        self.capturado["url"] = url
        self.capturado["params"] = params
        return self.respuesta


@pytest.mark.asyncio
async def test_solo_envia_la_direccion_y_devuelve_coordenadas(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict = {}
    respuesta = _Respuesta(
        200,
        [{"lat": "3.4516", "lon": "-76.532", "display_name": "Calle 1, Cali", "type": "house"}],
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: _Cliente(respuesta, capturado))

    resultado = await service.geocodificar("Calle 1, Cali, Valle del Cauca, Colombia")
    assert resultado.latitud == 3.4516
    assert resultado.longitud == -76.532
    assert resultado.direccion_normalizada == "Calle 1, Cali"
    assert resultado.precision == "exacta"
    assert capturado["params"]["q"] == "Calle 1, Cali, Valle del Cauca, Colombia"
    assert capturado["params"]["countrycodes"] == "co"
    assert "name" not in capturado["params"] and "phone" not in capturado["params"]


@pytest.mark.asyncio
async def test_sin_resultado_es_422(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict = {}
    monkeypatch.setattr(
        service.httpx, "AsyncClient", lambda **kw: _Cliente(_Respuesta(200, []), capturado)
    )
    with pytest.raises(service.GeocodingError) as exc:
        await service.geocodificar("Dirección inventada")
    assert exc.value.status == 422
    assert exc.value.code == "geocoding_no_encontrada"


@pytest.mark.asyncio
async def test_sin_configurar_es_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOCODING_PROVIDER", "none")
    get_geocoding_settings.cache_clear()
    with pytest.raises(service.GeocodingError) as exc:
        await service.geocodificar("Calle 1")
    assert exc.value.status == 503


class _Redis:
    def __init__(self) -> None:
        self.n: dict[str, int] = {}

    async def incr(self, clave: str) -> int:
        self.n[clave] = self.n.get(clave, 0) + 1
        return self.n[clave]

    async def expire(self, clave: str, ttl: int) -> None:
        return None


@pytest.mark.asyncio
async def test_la_cuota_por_negocio_corta_en_el_tope(monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid

    fake = _Redis()
    monkeypatch.setattr(service, "get_redis", lambda: fake)
    tenant_id = uuid.uuid4()
    await service.consumir_cuota(tenant_id)
    await service.consumir_cuota(tenant_id)
    with pytest.raises(service.GeocodingError) as exc:
        await service.consumir_cuota(tenant_id)
    assert exc.value.status == 429


@pytest.mark.asyncio
async def test_reverso_propone_direccion_sin_escribir(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict = {}
    respuesta = _Respuesta(
        200,
        {
            "lat": "3.4516",
            "lon": "-76.532",
            "display_name": "Calle 1 #2-3, Cali, Valle del Cauca, Colombia",
            "addresstype": "house",
            "address": {
                "house_number": "2-3",
                "road": "Calle 1",
                "city": "Cali",
                "state": "Valle del Cauca",
                "country": "Colombia",
            },
        },
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: _Cliente(respuesta, capturado))

    resultado = await service.geocodificar_inverso(3.4516, -76.532)
    assert resultado.direccion == "Calle 1 2-3"
    assert resultado.municipio == "Cali"
    assert resultado.departamento == "Valle del Cauca"
    assert resultado.pais == "Colombia"
    assert resultado.precision == "exacta"
    assert resultado.latitud == 3.4516 and resultado.longitud == -76.532
    assert capturado["url"].endswith("/reverse")
    assert capturado["params"]["lat"] == 3.4516
    assert capturado["params"]["lon"] == -76.532
    assert "q" not in capturado["params"]


@pytest.mark.asyncio
async def test_reverso_coordenadas_invalidas_es_400() -> None:
    with pytest.raises(service.GeocodingError) as exc:
        await service.geocodificar_inverso(120, -76)
    assert exc.value.status == 400
    assert exc.value.code == "geocoding_coordenadas_invalidas"


@pytest.mark.asyncio
async def test_reverso_sin_resultado_es_422(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict = {}
    monkeypatch.setattr(
        service.httpx, "AsyncClient", lambda **kw: _Cliente(_Respuesta(200, {}), capturado)
    )
    with pytest.raises(service.GeocodingError) as exc:
        await service.geocodificar_inverso(2, -75)
    assert exc.value.status == 422
    assert exc.value.code == "geocoding_no_encontrada"
