"""HU-AOS-21 parte 3b · el corredor mide la candidata en `agentcore_eval` y deja el veredicto.

Sin LLM real ni red: el subproceso (`evals/gate_release.py`) corre de verdad contra una SEGUNDA base
(`agentcore_eval_test`), pero cada intento lo resuelve un ejecutor guionado
(`gate_ejecutor_guionado.py`) que comprueba que la release quedó montada en el negocio efímero y
devuelve lo que dicta el guion. Lo que se prueba es el camino completo: reclamo, foto, negocio efímero,
presupuesto, purga, validación de la salida, veredicto, gasto y efectos sobre el gate de promoción.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import _eval_db
import pytest
from _api_helpers import uniq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Escenarios declarados en evals/journeys/procedimientos y los procedimientos que ejercitan.
ESCENARIOS = {
    "01-contratar-premium-mensual": "pack_commerce.contratar_plan",
    "02-cambia-de-plan-antes-de-confirmar": "pack_commerce.contratar_plan",
    "03-pedido-a-domicilio": "pack_commerce.tomar_pedido",
    "04-agendar-una-demo": "pack_calendar.agendar_cita",
}
INTENTOS = len(ESCENARIOS) * 4


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


@pytest.fixture(scope="module")
async def bases_de_eval() -> tuple[str, str]:
    from core.releases.packs import sync_packs

    urls = await _eval_db.asegurar()
    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    return urls


@pytest.fixture
def entorno_de_eval(bases_de_eval: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """El worker de evals: URLs de eval en la configuración y el ejecutor guionado en el subproceso."""
    from core.config import get_settings

    runtime, dueno = bases_de_eval
    guion = tmp_path / "guion.json"
    guion.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EVAL_DATABASE_URL", runtime)
    monkeypatch.setenv("EVAL_MIGRATIONS_DATABASE_URL", dueno)
    monkeypatch.setenv("EVAL_GATE_EJECUTOR", "gate_ejecutor_guionado:ejecutor")
    monkeypatch.setenv("EVAL_GATE_GUION", str(guion))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")])))
    get_settings.cache_clear()
    yield guion
    get_settings.cache_clear()


def _guion(ruta: Path, datos: dict[str, list[bool]]) -> None:
    ruta.write_text(json.dumps(datos), encoding="utf-8")


async def _negocio_y_candidata(nombre: str = "Otro") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """(tenant, conexión, release activa, candidata que toca todos los procedimientos)."""
    from core.alta_negocio import onboard_tenant
    from core.releases.build import construir
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    slug = uniq("corredor")
    tid, _ = await onboard_tenant(
        name=f"Corredor {slug}", slug=slug, packs=["pack_commerce", "pack_payments", "pack_calendar"], plan="premium"
    )
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
    await _sql(tid, "update app_settings set assistant_name = :n where tenant_id = :t", n=nombre, t=tid)
    async with tenant_scope(tid):
        candidata = (await construir(tid, cid, actor="test", modules=_modules())).release_id
    return tid, cid, activa, candidata


async def _medir(tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, **kw: Any) -> Any:
    from core.releases.corredor import medir
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await medir(tid, cid, rid, actor="test", **kw)


async def _tenants_en_eval(dueno: str) -> int:
    motor = create_async_engine(dueno)
    try:
        async with motor.connect() as conn:
            return int((await conn.execute(text("select count(*) from tenants where slug like 'gate-eval-%'"))).scalar_one())
    finally:
        await motor.dispose()


async def test_mide_aprueba_registra_y_limpia(entorno_de_eval: Path, bases_de_eval: tuple[str, str]) -> None:
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    tid, cid, _activa, candidata = await _negocio_y_candidata()
    r = await _medir(tid, cid, candidata)
    assert r.estado == "aprobada", (r.estado, r.detalle)
    assert set(r.por_procedimiento) == set(ESCENARIOS.values())
    assert all(p["ok"] and p["pass_k"] == 1.0 for p in r.por_procedimiento.values())
    # El veredicto y el gasto quedaron en la base del NEGOCIO, el gasto como kind = eval a cargo de la plataforma.
    eventos = await _sql(tid, "select type, data->>'intento' from release_events where release_id = :r order by created_at", r=candidata)
    assert [e[0] for e in eventos] == ["built", "evaluated", "approved"]
    ((n_runs, costo),) = await _sql(
        tid, "select count(*), sum(cost_usd) from eval_runs where release_id = :r and origen = 'gate'", r=candidata
    )
    assert n_runs == 1 and abs(float(costo) - 0.01 * INTENTOS) < 1e-6
    ((gasto, creditos),) = await _sql(
        tid, "select sum(cost_usd), sum(credits) from usage_ledger where kind = 'eval' and release_id = :r", r=candidata
    )
    assert abs(float(gasto) - 0.01 * INTENTOS) < 1e-6 and float(creditos) == 0.0
    resultados = await _sql(
        tid,
        "select procedure_id, count(*) from eval_results er join eval_runs e on e.id = er.run_id "
        "where e.release_id = :r group by 1 order by 1",
        r=candidata,
    )
    assert dict(resultados) == {"pack_calendar.agendar_cita": 1, "pack_commerce.contratar_plan": 2, "pack_commerce.tomar_pedido": 1}
    # El negocio efímero ya no existe y no quedó nada del cliente en la base de eval.
    assert await _tenants_en_eval(bases_de_eval[1]) == 0
    # Idempotente: otra medición contra la misma base no gasta.
    assert (await _medir(tid, cid, candidata)).estado == "ya_medida"
    # El gate lee el veredicto y deja promover.
    async with tenant_scope(tid):
        assert (await promover(tid, cid, candidata, actor="test")).gate == "aprobado"


async def test_un_escenario_inestable_rechaza_y_bloquea_la_promocion(entorno_de_eval: Path) -> None:
    from core.releases.promocion import ConflictoDeRelease, promover
    from core.shared.tenant import tenant_scope

    _guion(entorno_de_eval, {"02-cambia-de-plan-antes-de-confirmar": [True, True, False, True]})
    tid, cid, _activa, candidata = await _negocio_y_candidata()
    r = await _medir(tid, cid, candidata)
    assert r.estado == "rechazada"
    assert r.por_procedimiento["pack_commerce.contratar_plan"]["pass_k"] == 0.5
    assert r.por_procedimiento["pack_commerce.contratar_plan"]["ok"] is False
    async with tenant_scope(tid):
        with pytest.raises(ConflictoDeRelease) as exc:
            await promover(tid, cid, candidata, actor="test")
    assert exc.value.codigo == "gate_rechazado"


async def test_una_falla_del_proceso_no_rechaza_y_el_gasto_parcial_cuenta(
    entorno_de_eval: Path, bases_de_eval: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    monkeypatch.setenv("EVAL_GATE_EXPLOTA", "03-pedido-a-domicilio")
    tid, cid, _activa, candidata = await _negocio_y_candidata()
    r = await _medir(tid, cid, candidata)
    assert r.estado == "fallida" and r.detalle == "error_inesperado"
    assert r.costo_usd > 0  # los escenarios 01 y 02 se corrieron antes
    tipos = await _sql(tid, "select type, data->>'estado' from release_events where release_id = :r and type = 'evaluated' order by created_at", r=candidata)
    assert [t[1] for t in tipos] == ["en_curso", "fallida"]
    ((veredictos,),) = await _sql(tid, "select count(*) from release_events where release_id = :r and type in ('approved','rejected')", r=candidata)
    assert veredictos == 0
    ((gasto,),) = await _sql(tid, "select sum(cost_usd) from usage_ledger where kind = 'eval' and release_id = :r", r=candidata)
    assert abs(float(gasto) - r.costo_usd) < 1e-6
    assert await _tenants_en_eval(bases_de_eval[1]) == 0
    # Sin veredicto, la transición sigue: pasa con `sin_evaluar`.
    async with tenant_scope(tid):
        assert (await promover(tid, cid, candidata, actor="test")).gate == "sin_evaluar"


async def test_el_tope_mensual_corta_antes_de_gastar(entorno_de_eval: Path) -> None:
    tid, cid, _activa, candidata = await _negocio_y_candidata()
    await _sql(
        tid,
        "update tenants set settings = jsonb_set(settings, '{evals}', '{\"tope_usd_mensual\": 0}') where id = :t",
        t=tid,
    )
    r = await _medir(tid, cid, candidata)
    assert r.estado == "tope_alcanzado"
    ((n,),) = await _sql(tid, "select count(*) from usage_ledger where kind = 'eval'")
    assert n == 0


async def test_un_reclamo_vivo_evita_la_segunda_medicion(entorno_de_eval: Path) -> None:
    from core.db.session import get_session
    from core.releases.build import registrar_evento
    from core.shared.tenant import tenant_scope

    tid, cid, activa, candidata = await _negocio_y_candidata()
    async with tenant_scope(tid):
        async with get_session() as session:
            await registrar_evento(session, tid, cid, candidata, "evaluated", "test", {"estado": "en_curso", "base_release_id": str(activa)})
    assert (await _medir(tid, cid, candidata)).estado == "en_curso"


async def test_si_la_activa_cambia_durante_la_medicion_no_hay_veredicto(entorno_de_eval: Path) -> None:
    from core.releases.build import construir
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    tid, cid, _activa, candidata = await _negocio_y_candidata()
    await _sql(tid, "update app_settings set assistant_name = 'Tercera' where tenant_id = :t", t=tid)
    async with tenant_scope(tid):
        otra = (await construir(tid, cid, actor="test", modules=_modules())).release_id

    async def ejecutor_que_promueve(entrada: dict[str, Any]) -> dict[str, Any]:
        async with tenant_scope(tid):
            await promover(tid, cid, otra, actor="test")
        return {"ok": True, "resultados": [], "costo_usd": 0, "input_tokens": 0, "output_tokens": 0, "modelo": "x"}

    r = await _medir(tid, cid, candidata, ejecutor=ejecutor_que_promueve)
    assert r.estado == "activa_cambio"
    ((veredictos,),) = await _sql(tid, "select count(*) from release_events where release_id = :r and type in ('approved','rejected')", r=candidata)
    assert veredictos == 0


async def test_sin_cambios_de_procedimientos_no_mide(entorno_de_eval: Path) -> None:
    tid, cid, activa, _candidata = await _negocio_y_candidata()
    assert (await _medir(tid, cid, activa)).estado == "sin_cambios"


async def test_el_camino_real_monta_la_release_y_se_detiene_sin_grabacion(
    entorno_de_eval: Path, bases_de_eval: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin el ejecutor guionado: `journeys.preparar_negocio`, la foto y las banderas corren de verdad y el
    primer escenario pide su grabación (que no existe para estos journeys): ni red ni LLM, sin veredicto."""
    monkeypatch.delenv("EVAL_GATE_EJECUTOR")
    tid, cid, _activa, candidata = await _negocio_y_candidata()
    r = await _medir(tid, cid, candidata, grabacion="replay")
    assert r.estado == "fallida" and r.costo_usd == 0
    assert await _tenants_en_eval(bases_de_eval[1]) == 0
    ((veredictos,),) = await _sql(tid, "select count(*) from release_events where release_id = :r and type in ('approved','rejected')", r=candidata)
    assert veredictos == 0
