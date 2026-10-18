"""HU-AOS-46 · presets v2: agentes especialistas desde plantillas y reglas del Coordinador (ADR-0139).

Negocio con `pack_commerce`, `pack_calendar` y `pack_subscriptions` (preset `suscripciones`), una
conexión conversacional, su release 1 (General por defecto) y `AOS_CONNECTION_ROUTING` encendida:

- **Instalar:** con `aos_runtime = 'v3'` crea Ventas, Agenda y Suscripciones (versión 1 derivada
  del General, con SUS procedimientos), una instancia de cada uno en la conexión (no por
  defecto), las reglas del Coordinador en una versión nueva y una release candidata que NO se
  activa.
- **Idempotencia:** reaplicar no crea agentes, versiones, instancias, reglas ni releases; una
  versión que publicó el dueño no se pisa y una instancia que quitó no vuelve.
- **Banderas apagadas:** con `legacy` (o la global apagada) no escribe nada.
- **Presets v1:** un manifiesto sin `agentes` deja al negocio solo con el General.
- **Dry-run** no escribe. **Aislamiento:** lo de un negocio no aparece en otro.
"""

from __future__ import annotations

import os
import uuid
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

PACKS = ["pack_commerce", "pack_calendar", "pack_subscriptions"]


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _runtime(tid: uuid.UUID, runtime: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_runtime = :r where id = :t"), {"t": tid, "r": runtime})
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


@pytest.fixture(autouse=True)
def _ruteo(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _negocio(runtime: str = "v3", *, otra_conexion: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    """Negocio con su conexión (y otra, si se pide), release 1 (General por defecto) y el runtime
    pedido. Devuelve (negocio, primera conexión)."""
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = f"aos46-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS46 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan="premium")
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
    )
    if otra_conexion:
        await _sql(
            tid,
            "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id, created_at) "
            "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta', now() + interval '1 second')",
            id=uuid.uuid4(), t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
        )
    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    await _runtime(tid, runtime)
    return tid, cid


async def _aplicar(tid: uuid.UUID, preset: str = "suscripciones", *, aplicar: bool = True) -> Any:
    from core.presets import load_preset
    from core.presets.agentes import aplicar_agentes
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await aplicar_agentes(tid, load_preset(preset), modules=_modules(), aplicar=aplicar)


async def _foto(tid: uuid.UUID) -> dict[str, Any]:
    agentes = await _sql(tid, "select key, status from agents where tenant_id = :t order by key", t=tid)
    versiones = await _sql(
        tid,
        "select a.key, count(*) from agent_versions v join agents a on a.id = v.agent_id "
        "where v.tenant_id = :t group by a.key order by a.key",
        t=tid,
    )
    instancias = await _sql(
        tid,
        "select a.key, i.is_default, i.deleted_at is null from agent_instances i join agents a on a.id = i.agent_id "
        "where i.tenant_id = :t order by a.key",
        t=tid,
    )
    releases = await _sql(tid, "select count(*) from connection_releases where tenant_id = :t", t=tid)
    return {
        "agentes": [tuple(f) for f in agentes],
        "versiones": dict((k, n) for k, n in versiones),
        "instancias": [tuple(f) for f in instancias],
        "releases": releases[0][0],
    }


async def _contenido(tid: uuid.UUID, key: str) -> dict[str, Any]:
    fila = await _sql(
        tid,
        "select v.content from agent_versions v join agents a on a.id = v.agent_id "
        "where v.tenant_id = :t and a.key = :k order by v.version desc limit 1",
        t=tid, k=key,
    )
    return dict(fila[0][0])


async def test_instala_agentes_reglas_y_release_candidata_sin_activarla() -> None:
    tid, cid = await _negocio()
    activa_antes = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)

    reporte = await _aplicar(tid)

    assert reporte.omitido is None and not reporte.errores, reporte
    assert {a.key: a.estado for a in reporte.agentes} == {
        "ventas": "creado", "agenda": "creado", "suscripciones": "creado",
    }
    foto = await _foto(tid)
    assert foto["versiones"] == {"coordinador": 2, "general": 1, "ventas": 1, "agenda": 1, "suscripciones": 1}
    por_defecto = {k for k, d, viva in foto["instancias"] if d and viva}
    assert por_defecto == {"general"}  # el General sigue atendiendo hasta HU-AOS-17
    assert {k for k, _d, viva in foto["instancias"] if viva} == {
        "coordinador", "general", "ventas", "agenda", "suscripciones",
    }

    # Cada especialista declara solo lo suyo y hereda el modelo y la memoria del General.
    general = await _contenido(tid, "general")
    ventas = await _contenido(tid, "ventas")
    agenda = await _contenido(tid, "agenda")
    assert {p["id"] for p in ventas["procedures"]} == {"pack_commerce.tomar_pedido", "pack_commerce.contratar_plan"}
    assert {p["id"] for p in agenda["procedures"]} == {"pack_calendar.agendar_cita"}
    assert all(p["version"].startswith("^") for p in ventas["procedures"])
    assert ventas["model"] == general["model"] and ventas["memory"] == general["memory"]
    assert ventas["declarativeTools"] == []
    tools_agenda = {b["tool"] for b in agenda["tools"]}
    assert "check_availability" in tools_agenda and "crear_pedido" not in tools_agenda

    # Reglas del Coordinador: agregadas, con `defaultAgent` intacto.
    kernel = await _contenido(tid, "coordinador")
    assert kernel["routing"]["defaultAgent"] == "general"
    assert {(r["id"], r["agent"]) for r in kernel["routing"]["rules"]} == {
        ("preset_ventas_palabras", "ventas"),
        ("preset_agenda_palabras", "agenda"),
        ("preset_suscripciones_palabras", "suscripciones"),
    }

    # Release candidata con los cinco componentes, sin activar.
    assert list(reporte.releases) == [str(cid)] and reporte.releases[str(cid)] == "2"
    componentes = await _sql(
        tid,
        "select c.manifest -> 'components' from connection_releases c where c.connection_id = :c and c.number = 2",
        c=cid,
    )
    assert {c["agent_key"] for c in componentes[0][0]} == {"coordinador", "general", "ventas", "agenda", "suscripciones"}
    activa = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    assert activa == activa_antes

    preset = await _sql(tid, "select settings -> 'preset' -> 'agentes' from tenants where id = :t", t=tid)
    assert preset[0][0]["preset"] == "suscripciones"
    auditoria = await _sql(
        tid, "select count(*) from audit_log where tenant_id = :t and action = 'preset_agentes_aplicados'", t=tid
    )
    assert auditoria[0][0] == 1


async def test_reaplicar_no_duplica_ni_pisa_lo_del_dueno() -> None:
    from core.agents import instancias, versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio()
    await _aplicar(tid)
    antes = await _foto(tid)

    otra = await _aplicar(tid)
    assert {a.estado for a in otra.agentes} == {"ya_estaba"}
    assert otra.reglas_nuevas == [] and otra.releases == {}
    assert await _foto(tid) == antes

    # El dueño edita Ventas (versión 2) y quita Agenda de la conexión.
    catalogo = catalogo_de_acciones(_modules())
    ventas_id = (await _sql(tid, "select id from agents where tenant_id = :t and key = 'ventas'", t=tid))[0][0]
    agenda_inst = (
        await _sql(
            tid,
            "select i.id from agent_instances i join agents a on a.id = i.agent_id "
            "where i.tenant_id = :t and a.key = 'agenda' and i.deleted_at is null",
            t=tid,
        )
    )[0][0]
    async with tenant_scope(tid):
        contenido = await _contenido(tid, "ventas")
        contenido["persona"]["maxLength"] = 321
        await versiones.publicar(tid, ventas_id, catalogo=catalogo, contenido=contenido)
        await instancias.quitar_instancia(tid, cid, agenda_inst)

    tercera = await _aplicar(tid)
    estados = {a.key: a.instancias[str(cid)] for a in tercera.agentes}
    assert estados["agenda"] == "quitada_por_el_dueno" and estados["ventas"] == "ya_estaba"
    assert (await _contenido(tid, "ventas"))["persona"]["maxLength"] == 321
    vivas = await _sql(
        tid,
        "select count(*) from agent_instances i join agents a on a.id = i.agent_id "
        "where i.tenant_id = :t and a.key = 'agenda' and i.deleted_at is null",
        t=tid,
    )
    assert vivas[0][0] == 0
    assert (await _foto(tid))["versiones"]["ventas"] == 2


async def test_regla_que_el_dueno_ya_tiene_no_se_toca() -> None:
    """Una regla con el mismo id que el dueño cambió (otro destino) se respeta."""
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    tid, _cid = await _negocio()
    coordinador = (await _sql(tid, "select id from agents where tenant_id = :t and key = 'coordinador'", t=tid))[0][0]
    kernel = await _contenido(tid, "coordinador")
    kernel["routing"]["rules"] = [
        {"id": "preset_agenda_palabras", "when": {"keywordAny": ["turno"]}, "agent": "general"}
    ]
    async with tenant_scope(tid):
        await versiones.publicar(tid, coordinador, catalogo=catalogo_de_acciones(_modules()), contenido=kernel)

    reporte = await _aplicar(tid)
    assert "preset_agenda_palabras" not in reporte.reglas_nuevas
    reglas = {r["id"]: r for r in (await _contenido(tid, "coordinador"))["routing"]["rules"]}
    assert reglas["preset_agenda_palabras"]["agent"] == "general"
    assert reglas["preset_agenda_palabras"]["when"]["keywordAny"] == ["turno"]
    assert reglas["preset_ventas_palabras"]["agent"] == "ventas"


async def test_con_banderas_apagadas_no_escribe(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    tid, _cid = await _negocio(runtime="legacy")
    antes = await _foto(tid)
    assert (await _aplicar(tid)).omitido == "banderas_apagadas"

    await _runtime(tid, "v3")
    monkeypatch.setattr(get_settings(), "aos_connection_routing", False)
    assert (await _aplicar(tid)).omitido == "banderas_apagadas"
    assert await _foto(tid) == antes


async def test_preset_v1_sigue_con_el_general() -> None:
    from core.presets import PresetManifest
    from core.presets.agentes import aplicar_agentes
    from core.shared.tenant import tenant_scope

    tid, _cid = await _negocio()
    antes = await _foto(tid)
    v1 = PresetManifest(id="comercio", version=1, nombre="Comercio", plan_minimo="basic", packs=["pack_commerce"])
    async with tenant_scope(tid):
        reporte = await aplicar_agentes(tid, v1, modules=_modules())
    assert reporte.omitido == "preset_v1"
    assert await _foto(tid) == antes


async def test_dry_run_no_escribe_y_reporta() -> None:
    tid, cid = await _negocio()
    antes = await _foto(tid)
    reporte = await _aplicar(tid, aplicar=False)
    assert {a.key: a.estado for a in reporte.agentes} == {
        "ventas": "creado", "agenda": "creado", "suscripciones": "creado",
    }
    assert reporte.releases == {str(cid): "se construiría"} and len(reporte.reglas_nuevas) == 3
    assert await _foto(tid) == antes


async def test_habilidad_sin_habilitar_omite_la_plantilla() -> None:
    """`cobranza` exige pack_cobranza, que el negocio no tiene: no se crea nada."""
    tid, _cid = await _negocio()
    reporte = await _aplicar(tid, "cobranza")
    assert [(a.key, a.estado) for a in reporte.agentes] == [("cobranza", "sin_habilidades")]
    assert reporte.reglas_nuevas == [] and reporte.releases == {}


async def test_backfill_cli_y_aislamiento() -> None:
    from core.presets.agentes import backfill_presets_v2

    tid_a, _ = await _negocio()
    tid_b, _ = await _negocio()
    secos = await backfill_presets_v2(tenant_ids=[tid_a], preset_id="comercio", modules=_modules())
    assert [r.aplicar for r in secos] == [False]
    assert (await _foto(tid_a))["versiones"].get("ventas") is None

    [hecho] = await backfill_presets_v2(tenant_ids=[tid_a], preset_id="comercio", aplicar=True, modules=_modules())
    assert [(a.key, a.estado) for a in hecho.agentes] == [("ventas", "creado")]
    # El otro negocio no ve nada (RLS y filtro por tenant).
    assert (await _foto(tid_b))["versiones"].get("ventas") is None
    ajenos = await _sql(tid_b, "select count(*) from agents where key = 'ventas'")
    assert ajenos[0][0] == 0

    # Sin --tenant no se impone un preset.
    from core.presets import PresetError

    with pytest.raises(PresetError):
        await backfill_presets_v2(preset_id="comercio", modules=_modules())


async def test_apply_preset_en_negocio_v3_instala_los_agentes() -> None:
    """El mismo `apply_preset` del alta y del CLI corre el paso de agentes en un negocio v3."""
    from core.presets.apply import apply_preset

    tid, cid = await _negocio()
    resultado = await apply_preset(tid, "comercio", sembrar_ejemplos=False)
    assert resultado.agentes is not None
    assert [(a["key"], a["estado"]) for a in resultado.agentes["agentes"]] == [("ventas", "creado")]
    assert any(p.startswith("agente ventas") for p in resultado.pasos)
    preset = await _sql(tid, "select settings -> 'preset' from tenants where id = :t", t=tid)
    assert preset[0][0]["id"] == "comercio" and preset[0][0]["agentes"]["creados"] == ["ventas"]
    # Reaplicar conserva el registro de agentes y no crea nada.
    otra = await apply_preset(tid, "comercio", sembrar_ejemplos=False)
    assert [(a["key"], a["estado"]) for a in otra.agentes["agentes"]] == [("ventas", "ya_estaba")]
    preset = await _sql(tid, "select settings -> 'preset' -> 'agentes' from tenants where id = :t", t=tid)
    assert preset[0][0] is not None
    assert str(cid) in resultado.agentes["releases"]


async def test_apply_preset_en_negocio_legacy_no_crea_agentes() -> None:
    from core.presets.apply import apply_preset

    tid, _cid = await _negocio(runtime="legacy")
    antes = await _foto(tid)
    resultado = await apply_preset(tid, "comercio", sembrar_ejemplos=False)
    assert resultado.agentes is None
    assert (await _foto(tid))["versiones"] == antes["versiones"]


async def test_agente_borrado_o_key_ajena_no_se_toca() -> None:
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    tid, _cid = await _negocio()
    # Un agente `agenda` que armó el dueño a mano: el preset no lo adopta.
    async with tenant_scope(tid):
        await versiones.crear_agente(tid, key="agenda", kind="specialist", name="Mi agenda")
    primera = await _aplicar(tid)
    estados = {a.key: a.estado for a in primera.agentes}
    assert estados["agenda"] == "key_ocupada" and estados["ventas"] == "creado"
    assert "preset_agenda_palabras" not in primera.reglas_nuevas

    # El dueño borra Suscripciones (borrado lógico): no vuelve.
    await _sql(tid, "update agent_instances set deleted_at = now() where tenant_id = :t and agent_id = "
               "(select id from agents where tenant_id = :t and key = 'suscripciones')", t=tid)
    await _sql(tid, "update agents set deleted_at = now() where tenant_id = :t and key = 'suscripciones'", t=tid)
    segunda = await _aplicar(tid)
    assert {a.key: a.estado for a in segunda.agentes}["suscripciones"] == "quitado_por_el_dueno"
    vivos = await _sql(tid, "select count(*) from agents where tenant_id = :t and key = 'suscripciones' "
                       "and deleted_at is null", t=tid)
    assert vivos[0][0] == 0


async def test_regla_borrada_por_el_dueno_no_vuelve() -> None:
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    tid, _cid = await _negocio()
    await _aplicar(tid)
    coordinador = (await _sql(tid, "select id from agents where tenant_id = :t and key = 'coordinador'", t=tid))[0][0]
    kernel = await _contenido(tid, "coordinador")
    kernel["routing"]["rules"] = [r for r in kernel["routing"]["rules"] if r["id"] != "preset_ventas_palabras"]
    async with tenant_scope(tid):
        await versiones.publicar(tid, coordinador, catalogo=catalogo_de_acciones(_modules()), contenido=kernel)

    otra = await _aplicar(tid)
    assert otra.reglas_nuevas == []
    ids = {r["id"] for r in (await _contenido(tid, "coordinador"))["routing"]["rules"]}
    assert "preset_ventas_palabras" not in ids


async def test_reglas_solo_de_agentes_en_todas_las_conexiones() -> None:
    """El Coordinador es uno por negocio: una conexión sin agenda (instalaciones propias sin
    pack_calendar) deja a Agenda solo en la otra y su regla no entra al KernelSpec compartido."""
    from core.agents.instalaciones import instalar_en_conexion
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio(otra_conexion=True)
    otra = next(
        c for (c,) in await _sql(tid, "select id from channel_connections where tenant_id = :t", t=tid) if c != cid
    )
    async with tenant_scope(tid):
        for pack in ("pack_commerce", "pack_subscriptions"):
            await instalar_en_conexion(tid, otra, pack, habilitados=set(PACKS))

    reporte = await _aplicar(tid)
    agenda = next(a for a in reporte.agentes if a.key == "agenda")
    assert agenda.instancias == {str(cid): "creada", str(otra): "sin_habilidades_en_la_conexion"}
    assert set(reporte.reglas_nuevas) == {"preset_ventas_palabras", "preset_suscripciones_palabras"}
    assert reporte.releases[str(cid)].isdigit()
    # Límite conocido de HU-AOS-16: el General de la conexión con instalaciones propias declara
    # todo lo del negocio y su build falla; se reporta por conexión sin tumbar la otra.
    assert reporte.releases[str(otra)].startswith("error:")


async def test_backfill_de_varios_negocios_no_mezcla_y_un_lock_por_negocio() -> None:
    import asyncio

    from core.presets.agentes import backfill_presets_v2

    tid_a, _ = await _negocio()
    tid_b, _ = await _negocio()
    # Dos corridas a la vez sobre el mismo negocio: una aplica y la otra queda `en_curso`
    # (o, si llega después, ya no tiene nada que hacer).
    uno, dos = await asyncio.gather(_aplicar(tid_a, "comercio"), _aplicar(tid_a, "comercio"))
    estados = sorted([uno.omitido or "aplicada", dos.omitido or "aplicada"])
    assert estados in (["aplicada", "en_curso"], ["aplicada", "aplicada"])
    assert (await _foto(tid_a))["versiones"]["ventas"] == 1

    await _sql(tid_b, "update tenants set settings = jsonb_set(coalesce(settings, '{}'), '{preset}', "
               "'{\"id\": \"cobranza\"}') where id = :t", t=tid_b)
    reportes = await backfill_presets_v2(tenant_ids=[tid_a, tid_b], aplicar=True, modules=_modules())
    por_negocio = {r.tenant_id: r for r in reportes}
    assert por_negocio[tid_b].preset == "cobranza"
    assert [(a.key, a.estado) for a in por_negocio[tid_b].agentes] == [("cobranza", "sin_habilidades")]
    assert (await _foto(tid_b))["versiones"].get("ventas") is None


async def test_corrida_cortada_tras_crear_el_agente_se_completa() -> None:
    """El agente quedó creado y anotado sin versión (la corrida se cortó): la siguiente lo publica
    y lo instancia, en vez de dejarlo sin conexión (D-35)."""
    from core.agents import versiones
    from core.presets.agentes import _anotar_creado
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio()
    async with tenant_scope(tid):
        await versiones.crear_agente(tid, key="ventas", kind="specialist", name="Ventas")
        await _anotar_creado(tid, "ventas")
    reporte = await _aplicar(tid, "comercio")
    [ventas] = reporte.agentes
    assert ventas.estado == "reactivado" and ventas.instancias == {str(cid): "creada"}
    assert (await _foto(tid))["versiones"]["ventas"] == 1
