"""HU-AOS-27 (backend) · diferencias legibles, detener una candidata y permisos en el gate.

- El diff de una candidata frente a la activa agrupa agentes (con las secciones que cambiaron, sin
  textos), superpoderes y permisos, y dice qué medirá el gate. Una release de otra conexión da 404.
- Cambiar los permisos A2A toca los procedimientos de los agentes del enlace (`permiso:a>b`).
- Detener: 409 si es la activa; después no se puede activar (`release_retirada`), el listado la
  muestra `detenida` y reconstruir lo mismo responde 409 `version_detenida`.
- Un veredicto aprobado que no cubre lo tocado de ahora no vale.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers, banderas_como_dueno
from test_enrutamiento_especialistas import (
    CITA,
    PLAN,
    _especialista,
    _modules,
    _negocio,
    _publicar_release,
    _sql,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.tools.core_skill import register_core_skills

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    await plugin_manager.ensure_process_activation()
    register_core_skills(mcp_host)


async def _equipo() -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    tid, cid = await _negocio()
    agentes = {
        "ventas": await _especialista(tid, cid, "ventas", PLAN, {"informar_plan"}, defecto=True),
        "agenda": await _especialista(tid, cid, "agenda", CITA, {"list_appointment_types"}, defecto=False),
    }
    await _publicar_release(tid, cid)
    return tid, cid, agentes


async def _candidata(tid: uuid.UUID, cid: uuid.UUID) -> uuid.UUID:
    from core.releases.build import construir
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return (await construir(tid, cid, actor="test", modules=_modules())).release_id


async def _republicar_agenda(tid: uuid.UUID, agenda: uuid.UUID) -> None:
    """Agenda con una herramienta más (otra versión del agente)."""
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    catalogo = catalogo_de_acciones(_modules())
    async with tenant_scope(tid):
        borrador = await versiones.obtener_borrador(tid, agenda)
        contenido = dict(borrador.content)
        contenido["tools"] = [*contenido.get("tools", []), {"tool": "check_availability", "enabled": True}]
        await versiones.guardar_borrador(tid, agenda, contenido, borrador.lock_version, catalogo=catalogo)
        await versiones.publicar(tid, agenda, catalogo=catalogo)


async def test_el_diff_agrupa_agentes_permisos_y_lo_que_se_mide(app_client) -> None:
    from core.agents import grafo_a2a
    from core.shared.tenant import tenant_scope

    tid, cid, ag = await _equipo()
    await _republicar_agenda(tid, ag["agenda"])
    async with tenant_scope(tid):
        await grafo_a2a.publicar(tid, [{"from_agent_id": str(ag["ventas"]), "to_agent_id": str(ag["agenda"]), "modes": ["query"]}])
    rid = await _candidata(tid, cid)
    r = await app_client.get(f"/api/v1/admin/connections/{cid}/releases/{rid}/diff", headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    d = r.json()
    agenda = next(a for a in d["agentes"] if a["key"] == "agenda")
    assert agenda["cambio"] == "actualizado" and "tools" in agenda["secciones"]
    assert agenda["herramientas_agregadas"] == ["check_availability"]
    assert d["permisos"] == [
        {"desde": "Ventas", "hacia": "Agenda", "cambio": "agregado", "modos_antes": [], "modos_ahora": ["query"]}
    ]
    motivos = {m["procedure_id"]: m["motivo"] for m in d["medira"]}
    assert motivos[CITA].startswith(("agente:agenda", "permiso:"))
    assert all(m["titulo"] for m in d["medira"])
    assert d["sin_linea_base"] is False


async def test_solo_cambiar_permisos_toca_los_procedimientos_del_enlace() -> None:
    from core.agents import grafo_a2a
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.releases.diferencias import procedimientos_tocados
    from core.shared.tenant import tenant_scope

    tid, cid, ag = await _equipo()
    ((activa,),) = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    async with tenant_scope(tid):
        await grafo_a2a.publicar(tid, [{"from_agent_id": str(ag["ventas"]), "to_agent_id": str(ag["agenda"]), "modes": ["handoff"]}])
    rid = await _candidata(tid, cid)
    async with tenant_scope(tid):
        async with get_session() as s:
            tocados = procedimientos_tocados(await cargar_release(s, tid, rid), await cargar_release(s, tid, activa))
    assert tocados == {PLAN: "permiso:ventas>agenda", CITA: "permiso:ventas>agenda"}


async def test_el_diff_no_compara_con_otra_conexion(app_client) -> None:
    tid, cid, _ = await _equipo()
    _otro_tid, otra_cid, _ = await _equipo()
    ((ajena,),) = await _sql(_otro_tid, "select active_release_id from channel_connections where id = :c", c=otra_cid)
    rid = await _candidata(tid, cid)
    h = await api_headers(app_client, tid)
    r = await app_client.get(f"/api/v1/admin/connections/{cid}/releases/{rid}/diff?against={ajena}", headers=h)
    assert r.status_code == 404


async def test_detener_una_candidata(app_client) -> None:
    tid, cid, ag = await _equipo()
    await banderas_como_dueno(tid, aos_runtime="v3")
    ((activa,),) = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    await _republicar_agenda(tid, ag["agenda"])
    rid = await _candidata(tid, cid)
    h = await api_headers(app_client, tid)
    base = f"/api/v1/admin/connections/{cid}/releases"

    r = await app_client.post(f"{base}/{activa}/stop", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ya_activa"
    r = await app_client.post(f"{base}/{rid}/stop", headers=h)
    assert r.status_code == 200 and r.json()["detenida"] is True and r.json()["lifecycle"] == "invalid", r.text
    r = await app_client.post(f"{base}/{rid}/stop", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ya_detenida"
    r = await app_client.post(f"{base}/{rid}/promote", headers=h, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "release_retirada"
    r = await app_client.get(base, headers=h)
    fila = next(x for x in r.json() if x["id"] == str(rid))
    assert fila["detenida"] is True and fila["lifecycle"] == "invalid"
    # Reconstruir el mismo contenido devuelve la detenida: 409 claro, no una candidata imposible.
    r = await app_client.post(base, headers=h, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_detenida", r.text


async def test_un_veredicto_que_no_cubre_lo_tocado_no_vale() -> None:
    from core.releases import veredicto as v
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    tid, cid, ag = await _equipo()
    ((activa,),) = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    await _republicar_agenda(tid, ag["agenda"])
    rid = await _candidata(tid, cid)
    medicion = v.Medicion("pack_x.otro", (v.Escenario("01", 4, 1.0, 1.0, True),), "s")
    async with tenant_scope(tid):
        await v.registrar(
            tid, cid, rid, base_release_id=activa, tocados={}, mediciones=[medicion],
            veredicto=v.calcular([medicion]), costo_usd=0.0, modelo="m",
        )
        cambio = await promover(tid, cid, rid, actor="test")
    assert cambio.gate == "sin_evaluar"  # aprobado, pero no midió agendar_cita
