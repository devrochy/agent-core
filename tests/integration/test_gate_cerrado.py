# ruff: noqa: F811  (los fixtures importados de test_gate_corredor se usan como parámetros)
"""HU-AOS-21 parte 4 · cerrar el gate: bandera por negocio, 409 `gate_pendiente`, encolado y tarea.

Sin LLM: la medición la resuelve el ejecutor guionado de la parte 3b contra `agentcore_eval_test`.

- Bandera apagada: una candidata sin veredicto pasa con `sin_evaluar` (como antes).
- Bandera encendida: 409 `gate_pendiente` y se encola UNA medición; la tarea mide (aprueba) y la
  promoción pasa con `aprobado`. Si la medición no encontró escenarios, pasa con `sin_medir`.
- «Evaluar» encola (202) o responde 200 sin medir; `evaluation` devuelve el veredicto.
- La bandera solo la cambia el master, no pisa el resto de `settings` y exige la base de eval.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers, banderas_como_dueno, uniq
from test_gate_corredor import (  # noqa: F401
    _guion,
    _negocio_y_candidata,
    _sql,
    bases_de_eval,
    entorno_de_eval,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

TODOS = {
    "01-contratar-premium-mensual": [True] * 4,
    "02-cambia-de-plan-antes-de-confirmar": [True] * 4,
    "03-pedido-a-domicilio": [True] * 4,
    "04-agendar-una-demo": [True] * 4,
}


class _Cola:
    def __init__(self) -> None:
        self.pedidos: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, task_name: str, **kwargs: Any) -> None:
        self.pedidos.append((task_name, kwargs))


@pytest.fixture
def cola(monkeypatch: pytest.MonkeyPatch) -> _Cola:
    import core.tasks.queue as q

    falsa = _Cola()
    monkeypatch.setattr(q, "task_queue", falsa)
    return falsa


async def _cerrar(tid: uuid.UUID, cerrado: bool = True, tope: float | None = None) -> Any:
    from core.releases import gate
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await gate.fijar(tid, cerrado=cerrado, tope_usd_mensual=tope, actor="test")


async def _promover(tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID) -> Any:
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await promover(tid, cid, rid, actor="test")


async def test_con_la_bandera_apagada_sigue_pasando_sin_evaluar(entorno_de_eval: Path) -> None:
    tid, cid, _activa, candidata = await _negocio_y_candidata("Abierto")
    assert (await _promover(tid, cid, candidata)).gate == "sin_evaluar"


async def test_cerrado_pide_medir_la_tarea_aprueba_y_la_promocion_pasa(entorno_de_eval: Path, cola: _Cola, app_client) -> None:
    from core.tasks.gate_tasks import evaluar

    tid, cid, _activa, candidata = await _negocio_y_candidata("Cerrado")
    await banderas_como_dueno(tid, aos_runtime="v3")
    await _cerrar(tid)
    h = await api_headers(app_client, tid)
    url = f"/api/v1/admin/connections/{cid}/releases/{candidata}"

    r = await app_client.post(f"{url}/promote", headers=h, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "gate_pendiente", r.text
    detalles = r.json()["error"]["details"]
    assert detalles["estado"] == "sin_medicion" and detalles["encolado"] == "encolada"
    assert [n for n, _ in cola.pedidos] == ["core.releases.evaluar_candidata"]
    assert cola.pedidos[0][1]["release_id"] == str(candidata)

    # El worker mide (ejecutor guionado: todo estable) y deja `approved`.
    _guion(entorno_de_eval, TODOS)
    assert await evaluar(**cola.pedidos[0][1]) == "aprobada"
    r = await app_client.get(f"{url}/evaluation", headers=h)
    assert r.status_code == 200 and r.json()["veredicto"]["aprobado"] is True, r.text
    assert r.json()["gate_cerrado"] is True

    r = await app_client.post(f"{url}/promote", headers=h, json={})
    assert r.status_code == 200, r.text
    ((gate,),) = await _sql(tid, "select data->>'gate' from release_events where release_id = :r and type = 'promoted'", r=candidata)
    assert gate == "aprobado"


async def test_una_medicion_en_curso_no_se_vuelve_a_encolar(entorno_de_eval: Path, cola: _Cola) -> None:
    from core.db.session import get_session
    from core.releases import gate
    from core.releases.build import registrar_evento
    from core.shared.tenant import tenant_scope

    tid, cid, activa, candidata = await _negocio_y_candidata("En curso")
    await _cerrar(tid)
    async with tenant_scope(tid):
        async with get_session() as s:
            await registrar_evento(s, tid, cid, candidata, "evaluated", "test", {"estado": "en_curso", "base_release_id": str(activa)})
        assert await gate.solicitar_medicion(tid, cid, candidata, actor="test") == "en_curso"
    assert cola.pedidos == []


async def test_sin_escenarios_pasa_anotado_sin_medir(entorno_de_eval: Path) -> None:
    from core.db.session import get_session
    from core.releases.build import registrar_evento
    from core.shared.tenant import tenant_scope

    tid, cid, activa, candidata = await _negocio_y_candidata("Sin escenarios")
    await _cerrar(tid)
    async with tenant_scope(tid):
        async with get_session() as s:
            await registrar_evento(
                s, tid, cid, candidata, "evaluated", "test",
                {"estado": "sin_escenarios", "base_release_id": str(activa), "sin_escenarios": ["pack_x.y"]},
            )
    assert (await _promover(tid, cid, candidata)).gate == "sin_medir"


async def test_evaluar_encola_o_responde_sin_medir(entorno_de_eval: Path, cola: _Cola, app_client) -> None:
    tid, cid, activa, candidata = await _negocio_y_candidata("Evaluar")
    await banderas_como_dueno(tid, aos_runtime="v3")
    h = await api_headers(app_client, tid)
    url = f"/api/v1/admin/connections/{cid}/releases"
    r = await app_client.post(f"{url}/{candidata}/evaluate", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "gate_abierto"
    await _cerrar(tid)
    r = await app_client.post(f"{url}/{candidata}/evaluate", headers=h)
    assert r.status_code == 202 and r.json()["estado"] == "encolada", r.text
    # Un segundo pedido no encola otra tarea (una sola pendiente por candidata y base).
    r = await app_client.post(f"{url}/{candidata}/evaluate", headers=h)
    assert r.status_code == 202 and r.json()["estado"] == "encolada"
    r = await app_client.post(f"{url}/{activa}/evaluate", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ya_activa"
    assert len(cola.pedidos) == 1


async def test_la_bandera_es_del_master_no_pisa_settings_y_exige_la_base(entorno_de_eval: Path, monkeypatch: pytest.MonkeyPatch, app_client) -> None:
    from core.admin.auth import hash_password
    from core.config import get_settings
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid, _cid, _activa, _candidata = await _negocio_y_candidata("Bandera")
    await _sql(
        tid,
        "update tenants set settings = coalesce(settings,'{}'::jsonb) || '{\"alta\": {\"origen\": \"x\"}}' where id = :t",
        t=tid,
    )
    master = await api_headers(app_client, tid)
    url = f"/api/v1/admin/platform/tenants/{tid}/gate"
    r = await app_client.put(url, headers=master, json={"cerrado": True, "tope_usd_mensual": 3})
    assert r.status_code == 200 and r.json() == {"cerrado": True, "tope_usd_mensual": 3.0}, r.text
    ((evals, alta),) = await _sql(tid, "select settings->'evals', settings->'alta' from tenants where id = :t", t=tid)
    assert evals == {"gate": "cerrado", "tope_usd_mensual": 3} and alta == {"origen": "x"}
    r = await app_client.put(url, headers=master, json={"cerrado": False, "tope_usd_mensual": None})
    assert r.json() == {"cerrado": False, "tope_usd_mensual": None}
    ((evals,),) = await _sql(tid, "select settings->'evals' from tenants where id = :t", t=tid)
    assert evals == {}
    assert (await app_client.put(url, headers=master, json={"cerrado": False, "tope_usd_mensual": 1000})).status_code == 422
    auditoria = await _sql(tid, "select count(*) from audit_log where action = 'release_gate_bandera'")
    assert auditoria[0][0] == 2

    # Sin base de eval no se puede cerrar.
    monkeypatch.setenv("EVAL_DATABASE_URL", "")
    get_settings.cache_clear()
    r = await app_client.put(url, headers=master, json={"cerrado": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sin_base_de_eval"

    # Un dueño no la cambia.
    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    dueno = await api_headers(app_client, tid, email=email, password="clave-segura-123")
    assert (await app_client.put(url, headers=dueno, json={"cerrado": False})).status_code == 403


async def test_al_agotar_la_espera_queda_corredor_ocupado(entorno_de_eval: Path) -> None:
    from core.tasks.gate_tasks import agotada

    tid, cid, _activa, candidata = await _negocio_y_candidata("Ocupado")
    await agotada(str(tid), str(cid), str(candidata), "test")
    ((codigo,),) = await _sql(
        tid, "select data->>'codigo' from release_events where release_id = :r and type = 'evaluated'", r=candidata
    )
    assert codigo == "corredor_ocupado"


async def test_sin_base_de_eval_la_tarea_no_mide(entorno_de_eval: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings
    from core.tasks.gate_tasks import evaluar

    tid, cid, _activa, candidata = await _negocio_y_candidata("Sin base")
    monkeypatch.setenv("EVAL_DATABASE_URL", "")
    get_settings.cache_clear()
    assert await evaluar(str(tid), str(cid), str(candidata), "test") == "sin_base_de_eval"
    ((estado, codigo),) = await _sql(
        tid, "select data->>'estado', data->>'codigo' from release_events where release_id = :r and type = 'evaluated'", r=candidata
    )
    assert (estado, codigo) == ("fallida", "sin_base_de_eval")
