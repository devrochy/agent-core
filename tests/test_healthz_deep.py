"""`/healthz/deep` (ADR-0188): 200/503 por componente, sin detalles internos, con caché de 5 s,
y `/healthz` sin cambios."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

import core.observability.salud as salud


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.setattr(salud, "_cache", None)
    monkeypatch.setattr(salud, "_candado", asyncio.Lock())


async def _bien() -> None:
    return None


def _sondas(monkeypatch, **reemplazos):
    sondas = {"db": _bien, "redis": _bien, "worker": _bien, **reemplazos}
    monkeypatch.setattr(salud, "SONDAS", sondas)


def _respuesta() -> tuple[int, dict]:
    r = asyncio.run(salud.healthz_deep())
    return r.status_code, json.loads(r.body)


def test_todo_bien_da_200(monkeypatch):
    _sondas(monkeypatch)
    assert _respuesta() == (200, {"status": "ok", "componentes": {"db": "ok", "redis": "ok", "worker": "ok"}})


def test_un_fallo_da_503_sin_el_mensaje(monkeypatch):
    async def _cae() -> None:
        raise ConnectionError("postgresql://usuario:clave@db-interna:5432/agent_core")

    _sondas(monkeypatch, db=_cae)
    codigo, cuerpo = _respuesta()
    assert codigo == 503
    assert cuerpo == {"status": "fallo", "componentes": {"db": "fallo", "redis": "ok", "worker": "ok"}}
    assert "clave" not in json.dumps(cuerpo) and "db-interna" not in json.dumps(cuerpo)


def test_una_sonda_colgada_falla_al_segundo(monkeypatch):
    async def _colgada() -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(salud, "TOPE_SONDA_S", 0.05)
    _sondas(monkeypatch, redis=_colgada)
    codigo, cuerpo = _respuesta()
    assert codigo == 503 and cuerpo["componentes"]["redis"] == "fallo"


def test_cachea_cinco_segundos_tambien_el_503(monkeypatch):
    llamadas: list[int] = []

    async def _cae() -> None:
        llamadas.append(1)
        raise RuntimeError("x")

    _sondas(monkeypatch, worker=_cae)

    async def _dos_veces():
        a = await salud.healthz_deep()
        b = await salud.healthz_deep()
        return a.status_code, b.status_code

    assert asyncio.run(_dos_veces()) == (503, 503)
    assert llamadas == [1]

    monkeypatch.setattr(salud, "_cache", (salud._cache[0] - salud.CACHE_S - 1, salud._cache[1]))
    asyncio.run(salud.healthz_deep())
    assert llamadas == [1, 1]  # vencida la caché, se vuelve a medir


def test_peticiones_simultaneas_comparten_una_medicion(monkeypatch):
    llamadas: list[int] = []

    async def _lenta() -> None:
        llamadas.append(1)
        await asyncio.sleep(0.05)

    _sondas(monkeypatch, db=_lenta)

    async def _a_la_vez():
        return await asyncio.gather(*(salud.healthz_deep() for _ in range(5)))

    assert {r.status_code for r in asyncio.run(_a_la_vez())} == {200}
    assert llamadas == [1]


class _RedisFalso:
    def __init__(self, valor):
        self.valor = valor

    async def get(self, _clave):
        return self.valor


@pytest.mark.parametrize(
    ("hace_s", "sano"),
    # Desfases fijos (no `datetime.now()` en el parametrize: xdist exige los mismos ids en cada worker).
    [(30, True), (salud.LATIDO_MAX_S + 60, False), (None, False), ("no-es-fecha", False)],
)
def test_sonda_worker_por_edad_del_latido(monkeypatch, hace_s, sano):
    valor = (datetime.now(UTC) - timedelta(seconds=hace_s)).isoformat() if isinstance(hace_s, int) else hace_s
    monkeypatch.setattr(salud, "_redis", lambda: _RedisFalso(valor))
    if sano:
        asyncio.run(salud.sonda_worker())
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(salud.sonda_worker())


def test_edad_del_latido():
    hace = (datetime.now(UTC) - timedelta(seconds=42)).isoformat()
    edad = asyncio.run(salud.edad_del_latido(_RedisFalso(hace.encode())))
    assert edad is not None and 41 <= edad <= 60
    assert asyncio.run(salud.edad_del_latido(_RedisFalso(None))) is None


def test_ruta_montada_publica_y_healthz_intacto(monkeypatch):
    from fastapi.testclient import TestClient

    from core.main import app

    _sondas(monkeypatch)
    cliente = TestClient(app)  # sin `with`: no corre el lifespan (no hace falta base)
    assert cliente.get("/healthz").json() == {"status": "ok"}
    r = cliente.get("/healthz/deep")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers["cache-control"] == "no-store"
