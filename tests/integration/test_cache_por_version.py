"""HU-AOS-35 · caché por versión de lo compilado y prefijo estable (adenda de ADR-0117).

Contra el stack real, con el loop real y un modelo guionado (sin LLM ni red), un negocio con su
release 1 y `aos_runtime = 'v3'`:
- **Una compilación por versión:** dos turnos de dos clientes compilan una vez; el prefijo (todos
  los bloques menos el dinámico) es idéntico byte a byte y es el de la ruta de siempre; los bytes
  de lo compilado cuentan en el tope de la caché.
- **Lo no disponible va al aviso:** con un pack caído para el negocio, el prefijo no cambia, las
  herramientas ofrecidas sí, y el bloque dinámico trae el aviso.
- **Un fallo no se cachea:** si la compilación falla, el turno se arma como siempre y el siguiente
  vuelve a compilar.
- **Aislamiento:** lo compilado de un negocio no se lee desde otro; dos agentes con el mismo
  contenido y distinta key compilan vistas distintas.
- **Métrica:** el turno escribe `cache_write_tokens`; Costos › por agente separa negocios y deja
  fuera del porcentaje las filas anteriores al corte (NULL) y las que no son actividad.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys
import types
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]
if "journeys" not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
    assert _SPEC and _SPEC.loader
    _modulo = importlib.util.module_from_spec(_SPEC)
    sys.modules["journeys"] = _modulo
    _SPEC.loader.exec_module(_modulo)
journeys = sys.modules["journeys"]

CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Planes"]],
    "products": [
        {"name": "Plan Premium", "description": "Todo incluido.", "category_path": ["Planes"],
         "variants": [{"sku": "PREMIUM-MES", "price": 600000, "stock": 0,
                       "attributes": {"periodicidad": "mensual", "trial_dias": 0}}]},
    ],
}
MENSAJE = "Hola, ¿qué planes tienen?"


# ---------------------------------------------------------------- helpers
def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _admin_sql(sql: str, **params: Any) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _negocio(slug: str, pagos: Any) -> tuple[uuid.UUID, uuid.UUID]:
    tid = await journeys.preparar_negocio(
        {
            "nombre": "Vendedor de planes",
            "packs": ["pack_commerce", "pack_payments"],
            "plan": "premium",
            "config": {
                "pack_payments": {"public_key": f"pub_test_{slug}", "private_key": f"prv_test_{slug}",
                                  "events_secret": f"test_events_{slug}", "sandbox": True},
            },
            "catalogo": CATALOGO,
        },
        pagos,
        slug=slug,
    )
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{cid.hex[:8]}",
    )
    journeys._CONEXION_V3[tid] = cid
    from core.releases.release_uno import backfill_releases

    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    await _admin_sql("update tenants set aos_runtime = 'v3' where id = :t", t=tid)
    return tid, cid


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID, Any]]:
    import pack_payments.payments as capacidad

    from core.config import get_settings
    from core.releases.packs import sync_packs
    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    assert not (await sync_packs(_modules())).fallo
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slugs = [f"cache35-a-{uuid.uuid4().hex[:6]}", f"cache35-b-{uuid.uuid4().hex[:6]}"]
    a, cid_a = await _negocio(slugs[0], pagos)
    b, _ = await _negocio(slugs[1], pagos)
    settings = get_settings()
    antes = settings.aos_connection_routing
    settings.aos_connection_routing = True
    yield a, cid_a, b, pagos
    settings.aos_connection_routing = antes
    capacidad.build_provider = original
    for tid, slug in zip((a, b), slugs, strict=True):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


class _Guion:
    """Modelo guionado: guarda las tools ofrecidas y los bloques del sistema de cada llamada."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.tools: list[list[str]] = []
        self.bloques: list[list[str]] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMUsageInfo

        self.tools.append(sorted(t.name for t in kwargs.get("tools") or []))
        self.bloques.append([str(b) for b in kwargs.get("system_blocks") or []])
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        uso = LLMUsageInfo(input_tokens=10, output_tokens=5, cache_read_tokens=80, cache_write_tokens=30,
                           input_includes_cache=False)
        return types.SimpleNamespace(content=paso, stop_reason="end_turn", model="fake", latency_ms=1, usage=uso,
                                     tool_calls=[])


async def _turno(tid: uuid.UUID, pagos: Any, monkeypatch: Any, nombre: str = "Rob") -> tuple[Any, _Guion, Any]:
    guion = _Guion(["Tenemos el plan Premium mensual."])
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *_a, **_k: guion)
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, nombre)
    ctx = journeys.Contexto(tid, customer_id, conversation_id, pagos)
    resultado = await journeys._turno(ctx, MENSAJE)
    return resultado, guion, ctx


async def _release(tid: uuid.UUID, cid: uuid.UUID) -> Any:
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    [(rid,)] = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    async with tenant_scope(tid):
        async with get_session() as session:
            return await cargar_release(session, tid, rid)


def _contar_compilaciones(monkeypatch: Any) -> list[int]:
    from core.agents import compiler

    original = compiler.compilar
    llamadas: list[int] = []

    async def _contando(**kwargs: Any) -> Any:
        llamadas.append(1)
        return await original(**kwargs)

    monkeypatch.setattr(compiler, "compilar", _contando)
    return llamadas


# ---------------------------------------------------------------- una compilación por versión
async def test_dos_turnos_compilan_una_vez_y_comparten_el_prefijo(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.releases import cargar

    a, cid, _b, pagos = negocios
    cargar.vaciar_cache()
    llamadas = _contar_compilaciones(monkeypatch)
    r1, g1, _ = await _turno(a, pagos, monkeypatch, "Ana")
    r2, g2, _ = await _turno(a, pagos, monkeypatch, "Beto")
    assert not r1.escalated and not r2.escalated
    assert len(llamadas) == 1
    prefijo1, prefijo2 = g1.bloques[0][:-1], g2.bloques[0][:-1]
    assert prefijo1 and prefijo1 == prefijo2  # byte a byte, con otro cliente y otra conversación
    assert any(b.startswith("## Flujo de atención") for b in prefijo1)

    release = await _release(a, cid)
    [compilado] = release._compilados.values()
    assert list(compilado.bloques_fijos) == prefijo1
    assert compilado.bytes > 0 and release.bytes >= compilado.bytes
    assert cargar._bytes_en_cache >= release.bytes

    # La ruta de siempre (legacy) arma el mismo prefijo.
    await _admin_sql("update tenants set aos_runtime = 'legacy' where id = :t", t=a)
    try:
        _r, g_legacy, _ = await _turno(a, pagos, monkeypatch, "Caro")
    finally:
        await _admin_sql("update tenants set aos_runtime = 'v3' where id = :t", t=a)
    assert g_legacy.bloques[0][:-1] == prefijo1


async def test_lo_compilado_se_entrega_como_copia(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.shared.tenant import tenant_scope

    a, cid, _b, _pagos = negocios
    release = await _release(a, cid)
    async with tenant_scope(a):
        compilado = await release.compilado(release.foco())
    assert compilado is not None
    copia = compilado.spec()
    copia.persona.maxLength = 1
    assert compilado.spec().persona.maxLength != 1


# ---------------------------------------------------------------- aviso de lo no disponible
async def test_un_pack_caido_va_al_aviso_y_no_cambia_el_prefijo(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.agents.compiler import _textos_no_disponible
    from core.plugin_manager import plugin_manager

    a, _cid, _b, pagos = negocios
    _r, sano, _ = await _turno(a, pagos, monkeypatch, "Dani")

    async def _caidos(_tid: Any) -> set[str]:
        return {"pack_commerce"}

    monkeypatch.setattr(plugin_manager, "disabled_plugins_for_tenant", _caidos)
    _r, caido, _ = await _turno(a, pagos, monkeypatch, "Eli")
    assert caido.bloques[0][:-1] == sano.bloques[0][:-1]
    assert "consultar_inventario" in sano.tools[0] and "consultar_inventario" not in caido.tools[0]
    encabezado = _textos_no_disponible()["encabezado"]
    assert encabezado not in sano.bloques[0][-1]
    dinamico = caido.bloques[0][-1]
    assert encabezado in dinamico and "consultar_inventario" in dinamico.split(encabezado, 1)[1]


# ---------------------------------------------------------------- un fallo no se cachea
async def test_un_fallo_de_compilacion_no_se_cachea(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.agents import compiler
    from core.releases import cargar

    a, cid, _b, pagos = negocios
    cargar.vaciar_cache()
    original = compiler.compilar

    async def _falla(**_kwargs: Any) -> Any:
        raise compiler.ErrorDeCompilacion("error de lectura")

    monkeypatch.setattr(compiler, "compilar", _falla)
    r, guion, _ = await _turno(a, pagos, monkeypatch, "Fede")
    assert not r.escalated and guion.bloques[0]  # el turno se armó como siempre
    assert (await _release(a, cid))._compilados == {}

    monkeypatch.setattr(compiler, "compilar", original)
    await _turno(a, pagos, monkeypatch, "Gabi")
    assert len((await _release(a, cid))._compilados) == 1


# ---------------------------------------------------------------- aislamiento
async def test_lo_compilado_no_se_lee_desde_otro_negocio(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.shared.tenant import tenant_scope

    a, cid, b, _pagos = negocios
    release = await _release(a, cid)
    async with tenant_scope(b):
        with pytest.raises(PermissionError):
            await release.compilado(release.foco())


async def test_dos_agentes_con_el_mismo_contenido_compilan_vistas_distintas(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.shared.tenant import tenant_scope

    a, cid, _b, _pagos = negocios
    release = await _release(a, cid)
    foco = release.foco()
    gemelo = dataclasses.replace(foco, key="gemelo", version_id=uuid.uuid4(), instance_id=uuid.uuid4())
    release.componentes = (*release.componentes, gemelo)
    try:
        async with tenant_scope(a):
            uno = await release.compilado(foco)
            dos = await release.compilado(gemelo)
        assert uno is not None and dos is not None and uno is not dos
        assert uno.spec().id != dos.spec().id and dos.spec().id == "gemelo"
    finally:
        release.componentes = tuple(c for c in release.componentes if c is not gemelo)
        release._compilados.pop(gemelo.version_id, None)


# ---------------------------------------------------------------- métrica
async def test_el_turno_escribe_lectura_escritura_y_total_de_entrada(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, _cid, _b, pagos = negocios
    _r, _g, ctx = await _turno(a, pagos, monkeypatch, "Hugo")
    [(entrada, leidos, escritos)] = await _sql(
        a,
        "select input_tokens, cached_tokens, cache_write_tokens from usage_ledger "
        "where tenant_id = :t and conversation_id = :c",
        t=a, c=ctx.conversation_id,
    )
    assert (entrada, leidos, escritos) == (10 + 80 + 30, 80, 30)


async def test_costos_por_agente_aislado_y_sin_filas_anteriores_al_corte(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.api.routers.costs import get_costs
    from core.shared.tenant import tenant_scope

    a, cid, b, _pagos = negocios
    [(instancia, agente)] = await _sql(
        a,
        "select ai.id, ai.agent_id from agent_instances ai join agents ag on ag.tenant_id = ai.tenant_id "
        "and ag.id = ai.agent_id where ai.tenant_id = :t and ai.connection_id = :c and ag.key = 'general'",
        t=a, c=cid,
    )
    # Una fila vieja (sin `cache_write_tokens`), una medible y una que no es actividad.
    for entrada, leidos, escritos, kind in ((1000, 900, None, "turn"), (100, 50, 0, "turn"), (500, 500, 0, "embedding")):
        await _sql(
            a,
            "insert into usage_ledger (id, tenant_id, model, input_tokens, output_tokens, cached_tokens, "
            "cache_write_tokens, cost_usd, agent_instance_id, kind) values (:id, :t, 'm', :i, 1, :r, :w, 0.01, :a, :k)",
            id=uuid.uuid4(), t=a, i=entrada, r=leidos, w=escritos, a=instancia, k=kind,
        )
    async with tenant_scope(a):
        costos = await get_costs(types.SimpleNamespace(tenant_id=a))  # type: ignore[arg-type]
    fila = next(f for f in costos.by_agent if f.agent_id == str(agente))
    medibles = await _sql(
        a,
        "select coalesce(sum(cached_tokens), 0), coalesce(sum(input_tokens), 0) from usage_ledger "
        "where tenant_id = :t and agent_instance_id = :a and cache_write_tokens is not null "
        "and kind not in ('embedding', 'generacion', 'eval')",
        t=a, a=instancia,
    )
    [(leidos_ok, entrada_ok)] = medibles
    assert fila.cached_tokens == leidos_ok
    assert fila.cached_pct == round(100 * leidos_ok / entrada_ok, 1)
    assert fila.cached_pct is not None and fila.cached_pct <= 100
    assert Decimal(str(fila.cost)) > 0

    async with tenant_scope(b):
        otros = await get_costs(types.SimpleNamespace(tenant_id=b))  # type: ignore[arg-type]
    assert all(f.agent_id != str(agente) for f in otros.by_agent)
