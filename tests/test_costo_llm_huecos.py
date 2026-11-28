"""Tres huecos de costo/calidad que no tocan el prompt (revisión de costo LLM, 2026-11-27).

1. Historial de respaldo (Postgres): los mensajes MÁS RECIENTES, no los más antiguos.
2. Modelo sin tarifa: nunca factura 0 en silencio.
3. `usage_ledger`: el costo ya gastado se escribe también si el turno revienta, una sola vez.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from core.llm import openai_compatible
from core.orchestrator import loop


# ------------------------------------------------------------------ 1. historial de respaldo
class _Sesion:
    def __init__(self, filas: list[Any]) -> None:
        self.filas = filas
        self.sentencia: Any = None

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sentencia: Any) -> Any:
        self.sentencia = sentencia
        filas = self.filas
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(filas)))


def _msg(n: int) -> Any:
    return SimpleNamespace(direction="inbound" if n % 2 else "outbound", content=f"m{n}")


async def _historial(monkeypatch: pytest.MonkeyPatch, short_term_turns: int | None) -> tuple[list[str], _Sesion]:
    async def _sin_redis(_cid: uuid.UUID) -> list[Any]:
        return []

    # La base devuelve lo más reciente primero (ORDER BY created_at DESC): m5 es el último mensaje.
    sesion = _Sesion([_msg(n) for n in (5, 4, 3, 2, 1)])
    monkeypatch.setattr(loop.working_memory, "get_recent_turns", _sin_redis)
    monkeypatch.setattr(loop, "get_session", lambda: sesion)
    monkeypatch.setattr(loop, "get_current_tenant_id", lambda: uuid.uuid4())
    historia = await loop._load_history(uuid.uuid4(), short_term_turns=short_term_turns)
    return [m.content for m in historia], sesion


async def test_el_respaldo_trae_lo_mas_reciente_en_orden_cronologico(monkeypatch: pytest.MonkeyPatch) -> None:
    contenidos, sesion = await _historial(monkeypatch, None)
    assert contenidos == ["m1", "m2", "m3", "m4", "m5"]  # invertido a cronológico
    sql = str(sesion.sentencia.compile())
    assert "ORDER BY messages.created_at DESC" in sql
    assert sesion.sentencia.compile().params["param_1"] == loop.HISTORIAL_RESPALDO_MAX


@pytest.mark.parametrize(("turnos", "esperado"), [(12, 12), (500, loop.HISTORIAL_RESPALDO_MAX), (0, loop.HISTORIAL_RESPALDO_MAX)])
async def test_el_respaldo_respeta_short_term_turns_sin_pasar_del_tope(
    monkeypatch: pytest.MonkeyPatch, turnos: int, esperado: int
) -> None:
    _, sesion = await _historial(monkeypatch, turnos)
    assert sesion.sentencia.compile().params["param_1"] == esperado


# ------------------------------------------------------------------ 2. modelo sin tarifa
def test_un_modelo_sin_tarifa_no_se_factura_en_cero_y_avisa_una_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    avisos: list[dict[str, Any]] = []
    monkeypatch.setattr(openai_compatible, "_MODELOS_SIN_TARIFA_AVISADOS", set())
    monkeypatch.setattr(openai_compatible.log, "warning", lambda evento, **kw: avisos.append({"evento": evento, **kw}))

    p = openai_compatible.OpenAICompatibleProvider("k", "modelo-que-no-existe", "https://x.test")
    assert p.pricing_per_mtok == openai_compatible.TARIFA_POR_DEFECTO
    assert all(precio > 0 for precio in p.pricing_per_mtok)
    openai_compatible.OpenAICompatibleProvider("k", "modelo-que-no-existe", "https://x.test")
    assert [a["evento"] for a in avisos] == ["llm_modelo_sin_tarifa"]  # una vez por modelo
    assert avisos[0]["model"] == "modelo-que-no-existe"


def test_un_modelo_con_tarifa_no_avisa(monkeypatch: pytest.MonkeyPatch) -> None:
    avisos: list[str] = []
    monkeypatch.setattr(openai_compatible.log, "warning", lambda evento, **kw: avisos.append(evento))
    assert openai_compatible.tarifa_del_modelo("gpt-4o-mini") == (0.15, 0.60)
    assert avisos == []


def test_resolve_provider_usa_la_misma_tarifa_conservadora(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings
    from core.llm import cassette, resolve_provider

    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    cassette.activar("replay")
    try:
        p = resolve_provider(provider_key="deepseek", model="modelo-que-no-existe", api_key="k")
    finally:
        cassette.activar("off")
        get_settings.cache_clear()
    assert tuple(p.pricing_per_mtok) == openai_compatible.TARIFA_POR_DEFECTO


# ------------------------------------------------------------------ 3. ledger una sola vez
@pytest.fixture
def filas(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    escritas: list[Any] = []

    class _Sesion:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def add(self, fila: Any) -> None:
            escritas.append(fila)

    monkeypatch.setattr(loop, "get_session", lambda: _Sesion())
    monkeypatch.setattr(loop, "get_current_tenant_id", lambda: uuid.uuid4())
    monkeypatch.setattr(loop, "anotar_uso", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_creditos_del_turno", lambda usage: {})
    monkeypatch.setattr(loop.release_vigente, "vigente", lambda: None)
    monkeypatch.setattr(loop, "UsageLedger", lambda **kw: SimpleNamespace(**kw))
    return escritas


def _uso_con_gasto() -> loop._TurnUsage:
    uso = loop._TurnUsage()
    uso.input_tokens, uso.output_tokens, uso.cost_usd, uso.llamadas, uso.model = 100, 20, 0.0123, 1, "m"
    return uso


_SPEC = SimpleNamespace(version=1, model=SimpleNamespace(primary="m"))


async def test_el_ledger_es_idempotente_por_turno(filas: list[Any]) -> None:
    uso, cid = _uso_con_gasto(), uuid.uuid4()
    await loop._write_usage_ledger(cid, _SPEC, uso)
    await loop._write_usage_ledger(cid, _SPEC, uso)
    assert len(filas) == 1 and filas[0].cost_usd == 0.0123


async def test_si_el_turno_revienta_el_costo_gastado_se_escribe(
    monkeypatch: pytest.MonkeyPatch, filas: list[Any]
) -> None:
    uso, cid = _uso_con_gasto(), uuid.uuid4()

    async def _turno_roto(conversation_id: uuid.UUID, texto: str, costo: loop._CostoDelTurno) -> None:
        costo.conversation_id, costo.spec, costo.usage, costo.span = conversation_id, _SPEC, uso, None
        raise RuntimeError("falló entre dos llamadas al LLM")

    monkeypatch.setattr(loop, "_procesar_turno", _turno_roto)
    proceso = inspect.unwrap(loop.process_conversation)
    with pytest.raises(RuntimeError):
        await proceso(cid, "hola")
    assert len(filas) == 1 and filas[0].cost_usd == 0.0123


async def test_si_el_turno_ya_escribio_el_ledger_la_excepcion_no_lo_duplica(
    monkeypatch: pytest.MonkeyPatch, filas: list[Any]
) -> None:
    uso, cid = _uso_con_gasto(), uuid.uuid4()

    async def _turno(conversation_id: uuid.UUID, texto: str, costo: loop._CostoDelTurno) -> None:
        costo.conversation_id, costo.spec, costo.usage, costo.span = conversation_id, _SPEC, uso, None
        await loop._write_usage_ledger(conversation_id, _SPEC, uso)  # ruta normal
        raise RuntimeError("falló después de cerrar")

    monkeypatch.setattr(loop, "_procesar_turno", _turno)
    with pytest.raises(RuntimeError):
        await inspect.unwrap(loop.process_conversation)(cid, "hola")
    assert len(filas) == 1


async def test_un_turno_que_revienta_sin_llamar_al_llm_no_escribe_fila(
    monkeypatch: pytest.MonkeyPatch, filas: list[Any]
) -> None:
    async def _turno(conversation_id: uuid.UUID, texto: str, costo: loop._CostoDelTurno) -> None:
        costo.usage = loop._TurnUsage()
        raise RuntimeError("antes del primer LLM")

    monkeypatch.setattr(loop, "_procesar_turno", _turno)
    with pytest.raises(RuntimeError):
        await inspect.unwrap(loop.process_conversation)(uuid.uuid4(), "hola")
    assert filas == []
