"""HU-AOS-21 parte 2 · el gate de promoción anota qué tocó la candidata y respeta las exenciones.

Sin LLM ni red. Hasta el corredor de pass^k (parte 3) el gate no bloquea: anota `gate` y
`gate_tocados` en el evento `promoted`. Las exenciones las pasan los caminos internos por
palabra clave y no se pueden falsificar desde la API.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, banderas_como_dueno, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
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


async def _negocio(prefijo: str = "gate") -> tuple[uuid.UUID, uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = uniq(prefijo)
    tid, _ = await onboard_tenant(name=f"Gate {slug}", slug=slug, persona={"tone": "cercano"})
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
    return tid, cid


async def _candidata(tid: uuid.UUID, cid: uuid.UUID, nombre: str) -> uuid.UUID:
    from core.releases.build import construir
    from core.shared.tenant import tenant_scope

    await _sql(tid, "update app_settings set assistant_name = :n where tenant_id = :t", n=nombre, t=tid)
    async with tenant_scope(tid):
        return (await construir(tid, cid, actor="test", modules=_modules())).release_id


async def _promover(tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, **kw: Any) -> Any:
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await promover(tid, cid, rid, actor="test", **kw)


async def _evento(tid: uuid.UUID, rid: uuid.UUID) -> dict[str, Any]:
    (fila,) = await _sql(
        tid, "select data from release_events where release_id = :r and type = 'promoted'", r=rid
    )
    return dict(fila[0])


async def test_release_1_exenta_y_un_cambio_queda_sin_evaluar_con_lo_que_toco() -> None:
    tid, cid = await _negocio()
    (uno,) = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    assert (await _evento(tid, uno[0]))["gate"] == "exento_release_1"

    dos = await _candidata(tid, cid, "Otro nombre")
    cambio = await _promover(tid, cid, dos)
    assert cambio.gate == "sin_evaluar"
    datos = await _evento(tid, dos)
    assert datos["gate"] == "sin_evaluar"
    # El nombre del asistente entra al prompt: toca todos los procedimientos de la release.
    assert datos["gate_tocados"] and set(datos["gate_tocados"].values()) == {"app_settings"}


async def test_exenciones_internas_y_qa_solo_en_negocio_de_prueba() -> None:
    from core.releases.promocion import ConflictoDeRelease

    tid, cid = await _negocio()
    dos = await _candidata(tid, cid, "Migración")
    with pytest.raises(ConflictoDeRelease) as exc:
        await _promover(tid, cid, dos, exencion="qa_datos")
    assert exc.value.codigo == "exencion_invalida"
    with pytest.raises(ValueError):
        await _promover(tid, cid, dos, exencion="cualquiera")
    cambio = await _promover(tid, cid, dos, motivo="migrar_a_v3", exencion="migrar_a_v3")
    assert cambio.gate == "exento_migrar_a_v3"

    qa, qa_cid = await _negocio("qa")
    tres = await _candidata(qa, qa_cid, "Datos del tester")
    assert (await _promover(qa, qa_cid, tres, motivo="qa_datos", exencion="qa_datos")).gate == "exento_qa_datos"


async def test_la_api_no_acepta_los_motivos_de_las_exenciones(app_client) -> None:
    tid, cid = await _negocio()
    await banderas_como_dueno(tid, aos_runtime="v3")
    dos = await _candidata(tid, cid, "Desde la API")
    master = await api_headers(app_client, tid)
    base = f"/api/v1/admin/connections/{cid}/releases/{dos}/promote"
    for motivo in ("migrar_a_v3", "qa_datos", "release_1"):
        r = await app_client.post(base, headers=master, json={"reason": motivo})
        assert r.status_code == 422, (motivo, r.text)
    r = await app_client.post(base, headers=master, json={"reason": "manual"})
    assert r.status_code == 200, r.text
    assert (await _evento(tid, dos))["gate"] == "sin_evaluar"
