"""HU-AOS-21 parte 3a · el gate lee veredictos, el master fuerza y el gasto de evals no es del negocio.

Sin LLM ni red: el veredicto lo escribe el test con `veredicto.registrar`, como lo hará el
corredor (parte 3b).
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

PASSWORD = "clave-segura-123"


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


async def _negocio() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(tid, conexión, release activa)."""
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = uniq("veredicto")
    tid, _ = await onboard_tenant(name=f"Veredicto {slug}", slug=slug, persona={"tone": "cercano"})
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
    ((activa,),) = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    return tid, cid, activa


async def _candidata(tid: uuid.UUID, cid: uuid.UUID, nombre: str) -> uuid.UUID:
    from core.releases.build import construir
    from core.shared.tenant import tenant_scope

    await _sql(tid, "update app_settings set assistant_name = :n where tenant_id = :t", n=nombre, t=tid)
    async with tenant_scope(tid):
        return (await construir(tid, cid, actor="test", modules=_modules())).release_id


async def _registrar(tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, base: uuid.UUID, *, aprueba: bool) -> uuid.UUID:
    from core.releases import veredicto as v
    from core.shared.tenant import tenant_scope

    medicion = v.Medicion(
        "pack_commerce.contratar_plan",
        (v.Escenario("01", 4, 1.0, 1.0, True), v.Escenario("02", 4, 1.0, 1.0 if aprueba else 0.0, aprueba)),
        "sha-escenarios",
    )
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.releases.diferencias import procedimientos_tocados

    async with tenant_scope(tid):
        # Como el corredor: lo tocado que no se midió va en `sin_medir` (el veredicto lo cubre).
        async with get_session() as session:
            tocados = procedimientos_tocados(
                await cargar_release(session, tid, rid), await cargar_release(session, tid, base)
            )
        return await v.registrar(
            tid,
            cid,
            rid,
            base_release_id=base,
            tocados={"pack_commerce.contratar_plan": "app_settings"},
            mediciones=[medicion],
            veredicto=v.calcular([medicion]),
            costo_usd=0.75,
            modelo="test-model",
            sin_medir=sorted(set(tocados) - {"pack_commerce.contratar_plan"}),
        )


async def _promover(tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, **kw: Any) -> Any:
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await promover(tid, cid, rid, actor="test", **kw)


async def test_un_veredicto_rechazado_bloquea_y_uno_aprobado_pasa() -> None:
    from core.releases.promocion import ConflictoDeRelease

    tid, cid, uno = await _negocio()
    dos = await _candidata(tid, cid, "Rechazada")
    await _registrar(tid, cid, dos, uno, aprueba=False)
    with pytest.raises(ConflictoDeRelease) as exc:
        await _promover(tid, cid, dos)
    assert exc.value.codigo == "gate_rechazado"

    # Un segundo intento aprobado (misma base) gana: es el último veredicto.
    await _registrar(tid, cid, dos, uno, aprueba=True)
    assert (await _promover(tid, cid, dos)).gate == "aprobado"
    intentos = await _sql(
        tid,
        "select type, (data->>'intento')::int from release_events where release_id = :r "
        "and type in ('approved','rejected') order by created_at",
        r=dos,
    )
    assert intentos == [("rejected", 1), ("approved", 2)]
    # La corrida, sus resultados por procedimiento y el gasto quedaron en la base del negocio.
    corridas = await _sql(tid, "select origen, release_id from eval_runs where release_id = :r", r=dos)
    assert corridas == [("gate", dos), ("gate", dos)]
    procs = await _sql(
        tid,
        "select distinct procedure_id from eval_results r join eval_runs e on e.id = r.run_id "
        "where e.release_id = :r",
        r=dos,
    )
    assert procs == [("pack_commerce.contratar_plan",)]


async def test_un_veredicto_contra_otra_base_no_cuenta() -> None:
    tid, cid, uno = await _negocio()
    dos = await _candidata(tid, cid, "Dos")
    await _promover(tid, cid, dos)  # sin veredicto: sin_evaluar
    tres = await _candidata(tid, cid, "Tres")
    # Medida contra la 1, pero la activa ahora es la 2: el veredicto rechazado no cuenta.
    await _registrar(tid, cid, tres, uno, aprueba=False)
    assert (await _promover(tid, cid, tres)).gate == "sin_evaluar"


async def test_el_gasto_de_evals_no_es_del_negocio_y_cuenta_para_el_tope() -> None:
    from core.billing.metrics import count_metric
    from core.db.session import get_session
    from core.releases import veredicto as v
    from core.shared.tenant import tenant_scope

    tid, cid, uno = await _negocio()
    dos = await _candidata(tid, cid, "Con gasto")
    await _registrar(tid, cid, dos, uno, aprueba=True)
    async with tenant_scope(tid):
        async with get_session() as session:
            gastado, tope = await v.tope_del_mes(session, tid)
    assert gastado == pytest.approx(0.75) and tope == pytest.approx(10.0)
    await _sql(
        tid,
        "update tenants set settings = jsonb_set(coalesce(settings,'{}'::jsonb), '{evals}', '{\"tope_usd_mensual\": 3}') where id = :t",
        t=tid,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            assert (await v.tope_del_mes(session, tid))[1] == pytest.approx(3.0)
    # La cuota de costo del plan no cuenta el gate (lo paga la plataforma).
    assert float(await count_metric(tid, "costo_llm_usd", period_start=None) or 0) == pytest.approx(0.0)


async def test_solo_el_master_fuerza_con_motivo_y_queda_auditado(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid, cid, uno = await _negocio()
    await banderas_como_dueno(tid, aos_runtime="v3")
    dos = await _candidata(tid, cid, "Forzada")
    await _registrar(tid, cid, dos, uno, aprueba=False)
    base = f"/api/v1/admin/connections/{cid}/releases/{dos}/promote"

    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    dueno = await api_headers(app_client, tid, email=email, password=PASSWORD)
    master = await api_headers(app_client, tid)

    r = await app_client.post(base, headers=dueno, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "gate_rechazado", r.text
    r = await app_client.post(base, headers=dueno, json={"forzar": True, "motivo_forzado": "urgencia"})
    assert r.status_code == 403
    r = await app_client.post(base, headers=master, json={"forzar": True})
    assert r.status_code == 422
    r = await app_client.post(base, headers=master, json={"forzar": True, "motivo_forzado": "Parche urgente de precios"})
    assert r.status_code == 200, r.text
    ((datos,),) = await _sql(tid, "select data from release_events where release_id = :r and type = 'promoted'", r=dos)
    assert datos["gate"] == "forzado" and datos["gate_motivo"] == "Parche urgente de precios"
    auditoria = await _sql(tid, "select count(*) from audit_log where action = 'release_gate_forzado'")
    assert auditoria[0][0] >= 1


async def test_veredictos_y_tope_no_cruzan_negocios() -> None:
    from core.db.session import get_session
    from core.releases import veredicto as v
    from core.releases.promocion import ConflictoDeRelease
    from core.shared.tenant import tenant_scope

    a, a_cid, _ = await _negocio()
    b, b_cid, b_uno = await _negocio()
    a_dos = await _candidata(a, a_cid, "A dos")
    b_dos = await _candidata(b, b_cid, "B dos")
    # Un veredicto rechazado en B no cambia el gate de A.
    await _registrar(b, b_cid, b_dos, b_uno, aprueba=False)
    assert (await _promover(a, a_cid, a_dos)).gate == "sin_evaluar"
    with pytest.raises(ConflictoDeRelease):
        await _promover(b, b_cid, b_dos)
    # Fuera del scope del negocio, las lecturas y la escritura se niegan.
    async with tenant_scope(a):
        async with get_session() as session:
            with pytest.raises(PermissionError):
                await v.tope_del_mes(session, b)
            with pytest.raises(PermissionError):
                await v.ultimo(session, b, b_dos, b_uno)
    with pytest.raises(PermissionError):
        async with tenant_scope(a):
            medicion = v.Medicion("x", (v.Escenario("01", 4, 1.0, 1.0, True),), "s")
            await v.registrar(
                b, b_cid, b_dos, base_release_id=b_uno, tocados={}, mediciones=[medicion],
                veredicto=v.calcular([medicion]), costo_usd=0.0, modelo="m",
            )
