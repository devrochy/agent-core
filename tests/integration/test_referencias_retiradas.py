"""HU-AOS-59 · Referencias retiradas en agentes y releases, contra el stack real (ADR-0184).

Reproduce el incidente del 2026-11-03: un agente con una versión publicada que nombra
`informar_plan` y `pack_commerce.contratar_plan` (retirados por SPEC-43 PR-4) y una release activa
que los congeló. Con DOS negocios, para comprobar que auditar y migrar uno no toca ni muestra al otro.

Sin LLM ni red. La release «vieja» se arma a mano (insert del manifiesto): el build de hoy ya no la
construiría, que es justo el caso.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # dos negocios: ni el informe ni la migración cruzan de uno a otro
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

VIEJO = "pack_commerce.contratar_plan"
NUEVO = "pack_subscriptions.contratar_plan"
ACCION = "informar_plan"


# ---------------------------------------------------------------- helpers
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


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte


async def _negocio(*, packs: list[str] | None = None) -> tuple[uuid.UUID, str, uuid.UUID]:
    """Negocio con una conexión conversacional, sus agentes base y la release 1. (tid, slug, conexión)."""
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = uniq("aos59")
    tid, _ = await onboard_tenant(name=f"AOS-59 {slug}", slug=slug, persona={"tone": "cercano"}, packs=packs)
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid,
        t=tid,
        ext=uniq("ig"),
    )
    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    return tid, slug, cid


async def _general(tid: uuid.UUID) -> uuid.UUID:
    from core.agents.instancias import agentes_base
    from core.agents.tolerante import KEY_GENERAL
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (await agentes_base(session, tid))[KEY_GENERAL]


def _catalogo_de_antes() -> Any:
    """El catálogo de ANTES del retiro: con `informar_plan` (lo que permitía publicar la versión)."""
    from core.kernel.procedures import ActionInfo, catalogo_de_acciones

    catalogo = dict(catalogo_de_acciones(_modules()))
    catalogo[ACCION] = ActionInfo(name=ACCION, owner="pack_commerce")
    return catalogo


async def _publicar_con_lo_retirado(tid: uuid.UUID, aid: uuid.UUID, *, distinto: bool = True) -> Any:
    """Publica una versión del General que nombra la acción y el procedimiento retirados."""
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        ultima = (await versiones.listar_versiones(tid, aid))[0]
        contenido = copy.deepcopy(ultima.content)
        contenido["tools"] = [*contenido.get("tools", []), {"tool": ACCION, "enabled": True, "scopes": []}]
        contenido["procedures"] = [
            *(p for p in contenido.get("procedures", []) if p["id"] != VIEJO),
            {"id": VIEJO, "version": "^1.0"},
        ]
        if distinto:
            contenido["description"] = "Ventas de planes (versión del dueño)"
        version, creada = await versiones.publicar(
            tid, aid, catalogo=_catalogo_de_antes(), contenido=contenido, label="con lo viejo", notes="antes del retiro"
        )
        assert creada
        return version


async def _release_a_mano(tid: uuid.UUID, cid: uuid.UUID, version: Any, aid: uuid.UUID) -> uuid.UUID:
    """Una release activa cuyo manifiesto congela `version` y resolvió el procedimiento retirado:
    lo que el build de entonces habría escrito. Devuelve su id."""
    from core.releases import manifiesto as man

    (fila,) = await _sql(
        tid,
        "select r.id, r.number, r.manifest::text from connection_releases r "
        "join channel_connections c on c.tenant_id = r.tenant_id and c.active_release_id = r.id "
        "where c.tenant_id = :t and c.id = :c",
        t=tid,
        c=cid,
    )
    manifiesto = json.loads(fila[2])
    for comp in manifiesto["components"]:
        if comp["agent_id"] == str(aid):
            comp["agent_version_id"] = str(version.id)
            comp["version"] = version.version
            comp["content_hash"] = version.content_hash
            comp["procedures"] = [*comp.get("procedures", []), {"id": VIEJO, "version": "1.0.0"}]
    nueva = uuid.uuid4()
    await _sql(
        tid,
        "insert into connection_releases (id, tenant_id, connection_id, number, origin, manifest, manifest_hash) "
        "values (:id, :t, :c, :n, 'platform', cast(:m as jsonb), :h)",
        id=nueva,
        t=tid,
        c=cid,
        n=fila[1] + 1,
        m=json.dumps(manifiesto),
        h=man.hash_de(manifiesto),
    )
    await _sql(
        tid,
        "insert into release_components (tenant_id, connection_id, release_id, agent_instance_id, agent_id, agent_version_id) "
        "select tenant_id, connection_id, :nueva, agent_instance_id, agent_id, "
        "case when agent_id = :a then :v else agent_version_id end "
        "from release_components where tenant_id = :t and release_id = :vieja",
        nueva=nueva,
        a=aid,
        v=version.id,
        t=tid,
        vieja=fila[0],
    )
    await _sql(tid, "update channel_connections set active_release_id = :r where tenant_id = :t and id = :c", r=nueva, t=tid, c=cid)
    return nueva


async def _negocio_del_incidente(*, distinto: bool = True, **kw: Any) -> tuple[uuid.UUID, str, uuid.UUID, uuid.UUID, Any]:
    """`distinto`: la versión con lo retirado cambia además otro campo, así que corregida no repite
    ninguna versión anterior (con False, queda idéntica a la 1: el caso del hash repetido)."""
    tid, slug, cid = await _negocio(**kw)
    aid = await _general(tid)
    version = await _publicar_con_lo_retirado(tid, aid, distinto=distinto)
    await _release_a_mano(tid, cid, version, aid)
    return tid, slug, cid, aid, version


async def _conteos(tid: uuid.UUID) -> dict[str, int]:
    (fila,) = await _sql(
        tid,
        "select (select count(*) from agent_versions where tenant_id = :t), "
        "(select count(*) from connection_releases where tenant_id = :t), "
        "(select count(*) from audit_log where tenant_id = :t and actor = 'system:migrar-referencias')",
        t=tid,
    )
    return {"versiones": fila[0], "releases": fila[1], "auditoria": fila[2]}


async def _auditar(*tids: uuid.UUID) -> Any:
    from core.releases.referencias_retiradas import auditar

    return await auditar(tenant_ids=list(tids), modules=_modules())


async def _migrar(tid: uuid.UUID, *, aplicar: bool) -> Any:
    from core.releases.referencias_retiradas import migrar

    return await migrar(tenant_id=tid, aplicar=aplicar, modules=_modules())


async def _activa(tid: uuid.UUID, cid: uuid.UUID) -> tuple[int, dict[str, Any]]:
    (fila,) = await _sql(
        tid,
        "select r.number, r.manifest::text from channel_connections c "
        "join connection_releases r on r.tenant_id = c.tenant_id and r.id = c.active_release_id "
        "where c.tenant_id = :t and c.id = :c",
        t=tid,
        c=cid,
    )
    return fila[0], json.loads(fila[1])


# ---------------------------------------------------------------- auditar
async def test_auditar_ve_la_version_y_la_release_activa_y_no_escribe_nada() -> None:
    tid, _slug, cid, _aid, version = await _negocio_del_incidente()
    antes = await _conteos(tid)

    (negocio,) = (await _auditar(tid)).negocios

    assert negocio.afectado
    en_versiones = {(f.agente, f.version, f.ruta, f.codigo, f.nombre) for f in negocio.filas if f.ambito == "version"}
    assert any(
        (agente, numero, codigo, nombre) == ("general", version.version, "procedimiento_inexistente", VIEJO)
        and ruta.startswith("procedures[")
        for agente, numero, ruta, codigo, nombre in en_versiones
    )
    assert any(
        nombre == ACCION and codigo == "accion_inexistente" and ruta.startswith("tools[")
        for _a, _v, ruta, codigo, nombre in en_versiones
    )
    # La release activa también cuenta, y la versión dice en qué conexión corre.
    assert negocio.conexiones_afectadas == {cid: 2}
    assert any(f.ambito == "release" for f in negocio.filas)
    assert any(f"conexión {cid} release #2" in d for f in negocio.filas if f.ambito == "version" for d in f.donde)
    assert (await _auditar(tid)).fallo is True
    assert await _conteos(tid) == antes


async def test_la_release_en_uso_cuenta_aunque_la_version_nueva_ya_este_limpia() -> None:
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    tid, _slug, cid, aid, version = await _negocio_del_incidente()
    async with tenant_scope(tid):
        limpio = copy.deepcopy(version.content)
        limpio["tools"] = [t for t in limpio["tools"] if t["tool"] != ACCION]
        limpio["procedures"] = [p for p in limpio["procedures"] if p["id"] != VIEJO]
        limpio["description"] = "Versión nueva, ya sin lo retirado"  # sin esto el hash repite la versión 1
        await versiones.publicar(tid, aid, catalogo=catalogo_de_acciones(_modules()), contenido=limpio, label="limpia")

    (negocio,) = (await _auditar(tid)).negocios

    assert not [f for f in negocio.filas if f.ambito == "version"]  # la última versión está limpia
    assert negocio.conexiones_afectadas == {cid: 2}  # pero la release que corre sigue nombrando lo viejo
    assert negocio.releases_afectadas == 1 and negocio.agentes_afectados == 0


async def test_conexion_borrada_se_reporta_aparte_y_no_cuenta() -> None:
    tid, _slug, cid, _aid, _version = await _negocio_del_incidente()
    # La conexión se borra pero conserva el puntero a su release vieja.
    await _sql(tid, "update channel_connections set deleted_at = now() where tenant_id = :t and id = :c", t=tid, c=cid)

    (negocio,) = (await _auditar(tid)).negocios

    assert negocio.conexiones_afectadas == {}
    assert [(a.conexion_id, a.motivo) for a in negocio.aparte] == [(cid, "borrada")]
    assert not [f for f in negocio.filas if f.ambito == "release"]


# ---------------------------------------------------------------- aislamiento
async def test_dos_negocios_el_informe_y_la_migracion_no_se_cruzan() -> None:
    a, slug_a, _cid_a, _aid_a, _ = await _negocio_del_incidente()
    b, slug_b, cid_b, _aid_b, _ = await _negocio_del_incidente()
    limpio, _slug_l, _cid_l = await _negocio()
    antes_b, antes_limpio = await _conteos(b), await _conteos(limpio)

    solo_a = (await _auditar(a)).negocios
    assert [n.slug for n in solo_a] == [slug_a]
    todos = {n.slug: n for n in (await _auditar(a, b, limpio)).negocios}
    assert todos[slug_a].afectado and todos[slug_b].afectado and not todos[_slug_l].afectado

    reporte = await _migrar(a, aplicar=True)
    assert [n.slug for n in reporte.negocios] == [slug_a]

    assert await _conteos(b) == antes_b and await _conteos(limpio) == antes_limpio
    assert (await _activa(b, cid_b))[0] == 2  # la release de B sigue siendo la suya
    assert (await _auditar(b)).negocios[0].afectado
    # Cada negocio solo ve sus conexiones: ninguna fila de B nombra una conexión de A.
    assert all(str(_cid_a) not in "".join(f.donde) for f in (await _auditar(b)).negocios[0].filas)
    assert not (await _auditar(a)).fallo


# ---------------------------------------------------------------- migrar
async def test_migrar_dry_run_no_escribe_y_aplicar_publica_reconstruye_y_es_idempotente() -> None:
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    tid, _slug, cid, aid, version = await _negocio_del_incidente(packs=["pack_subscriptions"])
    antes = await _conteos(tid)

    seco = await _migrar(tid, aplicar=False)
    (n,) = seco.negocios
    assert seco.dry_run and [(a.agente, a.accion) for a in n.agentes] == [("general", "por_migrar")]
    assert [c.accion for c in n.conexiones] == ["por_reconstruir"]
    assert await _conteos(tid) == antes

    real = await _migrar(tid, aplicar=True)
    (n,) = real.negocios
    assert not real.fallo and [(a.agente, a.accion) for a in n.agentes] == [("general", "migrada")]
    assert [c.accion for c in n.conexiones] == ["promovida"]

    async with tenant_scope(tid):
        nueva = (await versiones.listar_versiones(tid, aid))[0]
    assert nueva.version == version.version + 1 and nueva.label == "sin referencias retiradas"
    assert f"versión {version.version}" in nueva.notes and VIEJO in nueva.notes
    assert ACCION not in [t["tool"] for t in nueva.content["tools"]]
    ids = [p["id"] for p in nueva.content["procedures"]]
    assert VIEJO not in ids and NUEVO in ids  # el negocio tiene pack_subscriptions

    numero, manifiesto = await _activa(tid, cid)
    assert numero == 3
    general = next(c for c in manifiesto["components"] if c["agent_id"] == str(aid))
    assert general["version"] == nueva.version
    assert VIEJO not in [p["id"] for p in general["procedures"]]
    assert any(p["id"] == NUEVO for p in general["procedures"])

    # Quedó auditado y una segunda corrida no hace nada.
    despues = await _conteos(tid)
    assert despues["auditoria"] == antes["auditoria"] + 2
    otra = await _migrar(tid, aplicar=True)
    assert otra.negocios[0].sin_cambios
    assert await _conteos(tid) == despues
    assert not (await _auditar(tid)).fallo


async def test_sin_la_habilidad_se_quita_lo_retirado_y_no_se_agrega_el_reemplazo() -> None:
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    tid, _slug, cid, aid, _version = await _negocio_del_incidente()  # sin pack_subscriptions

    reporte = await _migrar(tid, aplicar=True)

    (n,) = reporte.negocios
    assert not reporte.fallo
    assert any("no se agregó" in aviso and NUEVO in aviso for aviso in n.agentes[0].avisos)
    async with tenant_scope(tid):
        nueva = (await versiones.listar_versiones(tid, aid))[0]
    assert VIEJO not in [p["id"] for p in nueva.content["procedures"]]
    assert NUEVO not in [p["id"] for p in nueva.content["procedures"]]
    assert (await _activa(tid, cid))[0] == 3


async def test_parte_de_la_ultima_version_publicada_y_no_toca_el_borrador() -> None:
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    tid, _slug, _cid, aid, _version = await _negocio_del_incidente()
    async with tenant_scope(tid):
        borrador = await versiones.obtener_borrador(tid, aid)
        contenido = copy.deepcopy(borrador.content)
        contenido["description"] = "Lo que el dueño está redactando"
        contenido["tools"] = [*contenido.get("tools", []), {"tool": ACCION, "enabled": True, "scopes": []}]
        await versiones.guardar_borrador(
            tid, aid, catalogo=_catalogo_de_antes(), contenido=contenido, lock_version=borrador.lock_version
        )
        antes = (await versiones.obtener_borrador(tid, aid)).content

    (n,) = (await _migrar(tid, aplicar=True)).negocios

    async with tenant_scope(tid):
        despues = await versiones.obtener_borrador(tid, aid)
        publicada = (await versiones.listar_versiones(tid, aid))[0]
    assert despues.content == antes  # el borrador no se tocó
    assert publicada.content["description"] != "Lo que el dueño está redactando"  # salió de la publicada
    assert [(f.agente, f.nombre) for f in n.borradores if f.nombre == ACCION] == [("general", ACCION)]


async def test_con_un_canary_en_curso_construye_la_release_pero_no_la_promueve() -> None:
    tid, _slug, cid, _aid, _version = await _negocio_del_incidente()
    (viejo,) = await _sql(tid, "select active_release_id from channel_connections where tenant_id = :t and id = :c", t=tid, c=cid)
    # Un canary en curso: el puntero de la conexión a una release candidata.
    (candidata,) = await _sql(
        tid, "select id from connection_releases where tenant_id = :t and connection_id = :c and number = 1", t=tid, c=cid
    )
    await _sql(
        tid,
        "update channel_connections set canary_release_id = :r where tenant_id = :t and id = :c",
        r=candidata[0],
        t=tid,
        c=cid,
    )

    reporte = await _migrar(tid, aplicar=True)

    (n,) = reporte.negocios
    assert [c.accion for c in n.conexiones] == ["construida_sin_promover"]
    assert "canary" in n.conexiones[0].detalle
    (activa,) = await _sql(tid, "select active_release_id from channel_connections where tenant_id = :t and id = :c", t=tid, c=cid)
    assert activa[0] == viejo[0]  # la activa no cambió: nadie reemplazó el canary a ciegas
    (canary,) = await _sql(tid, "select canary_release_id from channel_connections where tenant_id = :t and id = :c", t=tid, c=cid)
    assert canary[0] == candidata[0]


async def test_si_lo_corregido_repite_una_version_anterior_igual_publica_una_nueva_para_que_el_build_la_tome() -> None:
    from core.agents import versiones
    from core.releases.referencias_retiradas import MARCA
    from core.shared.tenant import tenant_scope

    # Corregida, la versión 2 queda idéntica a la 1: `publicar` devolvería la 1 y el build seguiría con la 2.
    tid, _slug, _cid, aid, version = await _negocio_del_incidente(distinto=False)

    reporte = await _migrar(tid, aplicar=True)

    (n,) = reporte.negocios
    assert not reporte.fallo and [a.accion for a in n.agentes] == ["migrada"]
    assert any(MARCA in aviso for aviso in n.agentes[0].avisos)
    async with tenant_scope(tid):
        nueva = (await versiones.listar_versiones(tid, aid))[0]
    assert nueva.version == version.version + 1 and MARCA in nueva.content["extensions"]
    assert [c.accion for c in n.conexiones] == ["promovida"]
    assert not (await _auditar(tid)).fallo
