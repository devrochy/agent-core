"""Servicio de embeddings del conocimiento (ADR-0145 §3, HU-AOS-49 parte 1).

- `hash` es determinista, unitario, sin tildes y acerca textos con palabras en común.
- `openai` solo sale a la red con `APP_ENV=production`; fuera de producción se usa `hash`.
- La ingesta escribe una fila `usage_ledger` `kind='embedding'` con el tenant del scope y
  créditos en 0; la consulta no escribe fila.
"""

from __future__ import annotations

import math
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from core.config import get_settings
from core.llm import embeddings
from core.llm.embeddings import (
    DIMENSION,
    MODELO_HASH,
    MODELO_OPENAI,
    EmbeddingsError,
    costo_usd,
    embeber_conocimiento,
    vector_hash,
)
from core.shared.tenant import tenant_scope

TENANT = uuid.UUID("20000000-0000-0000-0000-000000000049")


def _coseno(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@pytest.fixture
def ajustes(monkeypatch: pytest.MonkeyPatch):
    def _fijar(**valores: str) -> None:
        for clave, valor in valores.items():
            monkeypatch.setenv(clave, valor)
        get_settings.cache_clear()

    yield _fijar
    get_settings.cache_clear()


class _SesionFalsa:
    def __init__(self, filas: list[Any]) -> None:
        self._filas = filas

    async def __aenter__(self) -> _SesionFalsa:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def add(self, fila: Any) -> None:
        self._filas.append(fila)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    filas: list[Any] = []
    import core.db.session as session_mod

    monkeypatch.setattr(session_mod, "get_session", lambda: _SesionFalsa(filas))
    return filas


# ------------------------------------------------------------------ hash
def test_vector_hash_determinista_y_unitario() -> None:
    a = vector_hash("Hacemos envíos a todo el país")
    assert a == vector_hash("Hacemos envíos a todo el país")
    assert len(a) == DIMENSION
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-9)


def test_vector_hash_ignora_tildes_y_mayusculas() -> None:
    assert vector_hash("Política de ENVÍO") == vector_hash("politica de envio")


def test_vector_hash_acerca_textos_con_palabras_en_comun() -> None:
    pregunta = vector_hash("¿Cuánto cuesta el envío?")
    afin = vector_hash("El envío cuesta 10.000 pesos en Bogotá")
    ajeno = vector_hash("Atendemos de lunes a viernes")
    assert _coseno(pregunta, afin) > _coseno(pregunta, ajeno)


def test_vector_hash_texto_vacio_no_es_el_vector_cero() -> None:
    vacio = vector_hash("  ¿? ")
    assert vacio[0] == 1.0
    assert sum(abs(x) for x in vacio) == 1.0


def test_costo_por_modelo() -> None:
    assert costo_usd(MODELO_OPENAI, 1_000_000) == 0.02
    assert costo_usd(MODELO_OPENAI, 35_000) == 0.0007
    assert costo_usd(MODELO_HASH, 1_000_000) == 0.0


# ------------------------------------------------------------------ proveedor
async def test_por_defecto_es_hash(ajustes, ledger) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="hash", APP_ENV="development")
    async with tenant_scope(TENANT):
        r = await embeber_conocimiento(["hola"], proposito="consulta")
    assert r.modelo == MODELO_HASH
    assert r.cost_usd == 0.0
    assert r.vectores == [vector_hash("hola")]


async def test_openai_fuera_de_produccion_usa_hash_sin_red(ajustes, ledger, monkeypatch) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="openai", APP_ENV="development", OPENAI_API_KEY="sk-prueba")

    def _sin_red(*_: Any, **__: Any) -> Any:
        raise AssertionError("no debe salir a la red fuera de producción")

    monkeypatch.setattr(httpx, "AsyncClient", _sin_red)
    async with tenant_scope(TENANT):
        r = await embeber_conocimiento(["hola"], proposito="consulta")
    assert r.modelo == MODELO_HASH


async def test_openai_en_produccion_sin_llave_falla(ajustes, ledger) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="openai", APP_ENV="production", OPENAI_API_KEY="")
    async with tenant_scope(TENANT):
        with pytest.raises(EmbeddingsError) as exc:
            await embeber_conocimiento(["hola"], proposito="consulta")
    assert exc.value.codigo == "sin_llave"


def _cliente_con(handler):  # type: ignore[no-untyped-def]
    real = httpx.AsyncClient

    def _fabrica(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    return _fabrica


async def test_openai_en_produccion_lee_vectores_y_tokens(ajustes, ledger, monkeypatch) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="openai", APP_ENV="production", OPENAI_API_KEY="sk-prueba")
    vistos: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        cuerpo = json.loads(request.content)
        vistos.append(cuerpo)
        datos = [{"index": i, "embedding": [float(i)] * DIMENSION} for i in reversed(range(len(cuerpo["input"])))]
        return httpx.Response(200, json={"data": datos, "usage": {"prompt_tokens": 12}})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(handler))
    async with tenant_scope(TENANT):
        r = await embeber_conocimiento(["a", "b"], proposito="consulta")
    assert vistos == [{"model": MODELO_OPENAI, "input": ["a", "b"]}]
    # Ordenados por `index`, aunque la respuesta venga desordenada.
    assert [v[0] for v in r.vectores] == [0.0, 1.0]
    assert r.tokens == 12
    assert r.modelo == MODELO_OPENAI


async def test_openai_error_http_sin_cuerpo(ajustes, ledger, monkeypatch) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="openai", APP_ENV="production", OPENAI_API_KEY="sk-prueba")
    monkeypatch.setattr(
        httpx, "AsyncClient", _cliente_con(lambda _r: httpx.Response(429, json={"error": "texto secreto"}))
    )
    async with tenant_scope(TENANT):
        with pytest.raises(EmbeddingsError) as exc:
            await embeber_conocimiento(["texto secreto"], proposito="ingesta")
    assert exc.value.codigo == "http_429"
    assert "secreto" not in str(exc.value)
    assert ledger == []


# ------------------------------------------------------------------ límites del lote
async def test_requiere_tenant_scope(ajustes) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="hash")
    with pytest.raises(RuntimeError):
        await embeber_conocimiento(["hola"], proposito="consulta")


@pytest.mark.parametrize(
    "textos",
    [[], ["x"] * (embeddings.MAX_LOTE + 1), ["x" * (embeddings.MAX_CARACTERES + 1)]],
    ids=["vacio", "lote_grande", "texto_largo"],
)
async def test_rechaza_lotes_invalidos(ajustes, textos: list[str]) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="hash")
    async with tenant_scope(TENANT):
        with pytest.raises(ValueError):
            await embeber_conocimiento(textos, proposito="consulta")


# ------------------------------------------------------------------ ledger
async def test_ingesta_escribe_fila_embedding_del_scope(ajustes, ledger) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="hash")
    doc = uuid.uuid4()
    async with tenant_scope(TENANT):
        r = await embeber_conocimiento(["uno", "dos"], proposito="ingesta", document_id=doc)
    assert len(ledger) == 1
    fila = ledger[0]
    assert fila.tenant_id == TENANT
    assert fila.kind == "embedding"
    assert fila.conversation_id is None
    assert fila.turn_id is None
    assert fila.model == MODELO_HASH
    assert fila.input_tokens == r.tokens
    assert fila.credits == Decimal("0.0")
    assert fila.key_mode == "platform"
    # `ck_usage_credits_completos` (ADR-0144): con `credits`, tarifario y TRM.
    assert fila.tarifario_version is not None and fila.trm_cop is not None
    assert fila.result == {"proposito": "ingesta", "document_id": str(doc)}
    # Nunca el texto embebido.
    assert "uno" not in repr(fila.result)


async def test_consulta_no_escribe_fila(ajustes, ledger) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="hash")
    async with tenant_scope(TENANT):
        await embeber_conocimiento(["hola"], proposito="consulta")
    assert ledger == []


async def test_falla_del_ledger_no_corta_la_ingesta(ajustes, monkeypatch) -> None:
    ajustes(KNOWLEDGE_EMBEDDING_PROVIDER="hash")
    import core.db.session as session_mod

    def _rota() -> Any:
        raise RuntimeError("base caída")

    monkeypatch.setattr(session_mod, "get_session", _rota)
    async with tenant_scope(TENANT):
        r = await embeber_conocimiento(["hola"], proposito="ingesta")
    assert len(r.vectores) == 1
