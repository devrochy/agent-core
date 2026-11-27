"""HU-AOS-102 · estimado de créditos y aprobación de una corrida de pruebas del agente (ADR-0212 §4), con la API real.

- Estimado: `por_k` de 1 al tope, k = 2 es el doble de k = 1, la cuota gratis se gasta primero y es la misma del
  playground; el costo en USD solo lo ve el master.
- Aprobación: `confirmo_creditos` tiene que ser el estimado vigente (409 `estimado_cambio`); sin saldo, 409
  `plan_limit_creditos`; una corrida activa por negocio y 5 por día; máx. 20 escenarios aprobados.
- `k` con tope editable por el master para UN negocio (`k_fuera_de_rango`).
- Aislamiento: agente y escenarios de otro negocio son 404 sin eco.

Sin LLM ni red: crear la corrida no ejecuta nada (el corredor es HU-AOS-103).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from _ayudas_sql import _sql

pytestmark = [
    pytest.mark.aislamiento,  # dinero y escenarios por negocio (ADR-0212)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

V2 = {"key_mode": "platform", "tarifario_version": "v2", "trm_cop": Decimal("4000")}
PASSWORD = "clave-segura-123"


@pytest.fixture(autouse=True)
def pruebas_encendidas(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "pruebas_agente_enabled", True)
    monkeypatch.setattr(get_settings(), "pruebas_k_maximo", 4)


async def _negocio(plan: str = "basic") -> tuple[uuid.UUID, uuid.UUID]:
    from _habilidades import onboard_tenant

    from core.db.models import AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("aos102")
    tid, _ = await onboard_tenant(name=f"Pruebas {slug}", slug=slug, sembrar_ejemplos=False, plan=plan)
    agente = AgentRecord(tenant_id=tid, key=f"ventas_{uuid.uuid4().hex[:6]}", kind="specialist", name="Ventas")
    async with tenant_scope(tid), get_session() as session:
        session.add(agente)
        await session.flush()
        return tid, agente.id


async def _escenarios(tid: uuid.UUID, agente: uuid.UUID, n: int, *, estado: str = "aprobado") -> list[str]:
    from core.db.models import AgentTestCase
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    casos = [
        AgentTestCase(
            tenant_id=tid,
            agent_id=agente,
            titulo=f"Escenario {i} {uuid.uuid4().hex[:4]}",
            objetivo_cliente="Quiere pedir una pizza",
            criterios=["Confirma la dirección"],
            origen="manual",
            estado=estado,
        )
        for i in range(n)
    ]
    async with tenant_scope(tid), get_session() as session:
        session.add_all(casos)
        await session.flush()
        return [str(c.id) for c in casos]


async def _consumo(tid: uuid.UUID, creditos: str, *, kind: str = "prueba_agente") -> None:
    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        session.add(
            UsageLedger(
                tenant_id=tid, kind=kind, model="m", input_tokens=1, output_tokens=1, cost_usd=Decimal(0),
                credits=Decimal(creditos), **V2,
            )
        )


async def _dueno(tid: uuid.UUID, denegadas: tuple[str, ...] = ()) -> str:
    """Un gestor del negocio (no master) con escritura en `agents` salvo que se la deniegue."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion in dict.fromkeys(("agents", *denegadas)):
            acceso = "none" if seccion in denegadas else "write"
            session.add(AdminPermission(admin_id=admin.id, tenant_id=tid, section=seccion, access=acceso))
    return email


def _url(agente: uuid.UUID, ruta: str) -> str:
    return f"/api/v1/admin/agents/{agente}/pruebas/{ruta}"


async def _estimar(app_client, h, agente, ids) -> Any:
    return await app_client.post(_url(agente, "estimado"), headers=h, json={"case_ids": ids})


async def _lanzar(app_client, h, agente, ids, *, k: int, confirmo: int) -> Any:
    return await app_client.post(
        _url(agente, "corridas"), headers=h, json={"case_ids": ids, "k": k, "confirmo_creditos": confirmo}
    )


async def _corridas(tid: uuid.UUID) -> list[Any]:
    return await _sql(tid, "select estado, k, creditos_estimados, creditos_de_cuota, tope_creditos from agent_test_runs where tenant_id = :t", t=tid)


async def test_estimado_por_k_con_k2_el_doble_y_costo_usd_solo_master(app_client) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 10)
    master = await api_headers(app_client, tid)
    r = await _estimar(app_client, master, agente, ids)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    por_k = cuerpo["por_k"]
    assert [e["k"] for e in por_k] == [1, 2, 3, 4] and cuerpo["k_maximo"] == 4 and cuerpo["escenarios"] == 10
    assert por_k[1]["estimado"] == 2 * por_k[0]["estimado"] > 0
    for e in por_k:
        assert e["de_la_cuota"] + e["del_saldo"] == e["estimado"]
        assert e["costo_usd"] is not None and e["costo_usd"] > 0  # el master lo ve
    assert por_k[1]["costo_usd"] == pytest.approx(2 * por_k[0]["costo_usd"], rel=1e-3)

    dueno = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    r2 = await _estimar(app_client, dueno, agente, ids)
    assert r2.status_code == 200, r2.text
    assert all(e["costo_usd"] is None for e in r2.json()["por_k"])  # el dueño no
    assert [e["estimado"] for e in r2.json()["por_k"]] == [e["estimado"] for e in por_k]
    assert await _corridas(tid) == []  # estimar no escribe


async def test_cuota_gratis_primero_y_compartida_con_el_playground(app_client) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 20)
    h = await api_headers(app_client, tid)
    # 100 de pruebas ya corridas + 100 del playground vivo: quedan 100 de la cuota de 300.
    await _consumo(tid, "100.0")
    from core.db.models import Conversation, Customer, UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        cliente = Customer(tenant_id=tid, channel="test", external_id=f"a102:{uuid.uuid4().hex[:6]}")
        session.add(cliente)
        await session.flush()
        conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="test", state={})
        session.add(conv)
        await session.flush()
        session.add(
            UsageLedger(
                tenant_id=tid, conversation_id=conv.id, kind="turn", model="m", input_tokens=1, output_tokens=1,
                cost_usd=Decimal(0), credits=Decimal("100.0"), **V2,
            )
        )
    cuerpo = (await _estimar(app_client, h, agente, ids)).json()
    assert cuerpo["cuota_gratis_restante"] == 100
    k1 = cuerpo["por_k"][0]
    assert k1["estimado"] > 100
    assert k1["de_la_cuota"] == 100 and k1["del_saldo"] == k1["estimado"] - 100


async def test_cuota_de_200_con_estimado_mayor_sale_200_de_cuota_y_el_resto_del_saldo(app_client) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 20)
    h = await api_headers(app_client, tid)
    await _consumo(tid, "100.0")  # quedan 200
    k1 = (await _estimar(app_client, h, agente, ids)).json()["por_k"][0]
    assert k1["estimado"] > 200
    assert (k1["de_la_cuota"], k1["del_saldo"]) == (200, k1["estimado"] - 200)
    # El estimado de una cuota que alcanza sale entero de la cuota.
    pocos = await _escenarios(tid, agente, 1)
    uno = (await _estimar(app_client, h, agente, pocos)).json()["por_k"][0]
    assert uno["estimado"] <= 200 and uno["de_la_cuota"] == uno["estimado"] and uno["del_saldo"] == 0


async def test_confirmacion_distinta_es_409_y_no_crea_la_corrida(app_client) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 10)
    h = await api_headers(app_client, tid)
    estimado = (await _estimar(app_client, h, agente, ids)).json()["por_k"][0]["estimado"]
    malo = await _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado - 50)
    assert malo.status_code == 409, malo.text
    error = malo.json()["error"]
    assert error["code"] == "estimado_cambio" and error["details"]["estimado"] == estimado
    assert await _corridas(tid) == []

    bueno = await _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado)
    assert bueno.status_code == 202, bueno.text
    cuerpo = bueno.json()
    assert cuerpo["estado"] == "en_cola" and cuerpo["k"] == 1 and cuerpo["escenarios"] == 10
    filas = await _corridas(tid)
    assert len(filas) == 1 and filas[0][0] == "en_cola"
    assert filas[0][2] == Decimal(estimado) and filas[0][4] == Decimal(estimado) * Decimal("1.5")


async def test_el_estimado_de_otro_k_no_vale_para_este(app_client) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 5)
    h = await api_headers(app_client, tid)
    por_k = (await _estimar(app_client, h, agente, ids)).json()["por_k"]
    r = await _lanzar(app_client, h, agente, ids, k=3, confirmo=por_k[0]["estimado"])  # el de k = 1
    assert r.status_code == 409 and r.json()["error"]["code"] == "estimado_cambio"
    assert await _corridas(tid) == []


async def test_k_fuera_del_tope_y_tope_editable_por_el_master_solo_para_su_negocio(app_client) -> None:
    tid, agente = await _negocio()
    otro, agente_otro = await _negocio()
    ids = await _escenarios(tid, agente, 3)
    ids_otro = await _escenarios(otro, agente_otro, 3)
    h = await api_headers(app_client, tid)
    r = await _lanzar(app_client, h, agente, ids, k=5, confirmo=1)
    assert r.status_code == 422 and r.json()["error"]["code"] == "k_fuera_de_rango", r.text
    assert await _corridas(tid) == []

    # El dueño no cambia el tope; el master sí, y solo el de ese negocio.
    dueno = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    ruta = f"/api/v1/admin/platform/tenants/{tid}/pruebas"
    assert (await app_client.put(ruta, headers=dueno, json={"k_maximo": 9})).status_code == 403
    assert (await app_client.put(ruta, headers=h, json={"k_maximo": 11})).status_code == 422
    assert (await app_client.put(ruta, headers=h, json={"k_maximo": 0})).status_code == 422
    ok = await app_client.put(ruta, headers=h, json={"k_maximo": 2})
    assert ok.status_code == 200 and ok.json()["k_maximo"] == 2 and ok.json()["k_maximo_propio"] == 2

    assert [e["k"] for e in (await _estimar(app_client, h, agente, ids)).json()["por_k"]] == [1, 2]
    h_otro = await api_headers(app_client, otro)
    assert len((await _estimar(app_client, h_otro, agente_otro, ids_otro)).json()["por_k"]) == 4
    assert (await _lanzar(app_client, h, agente, ids, k=3, confirmo=1)).json()["error"]["code"] == "k_fuera_de_rango"

    # Subirlo por encima del de plataforma también vale (hasta el techo) y `null` vuelve al de plataforma.
    assert (await app_client.put(ruta, headers=h, json={"k_maximo": 6})).json()["k_maximo"] == 6
    assert len((await _estimar(app_client, h, agente, ids)).json()["por_k"]) == 6
    vuelta = await app_client.put(ruta, headers=h, json={"k_maximo": None})
    assert vuelta.json()["k_maximo"] == 4 and vuelta.json()["k_maximo_propio"] is None
    assert (await app_client.get(ruta, headers=h)).json()["k_maximo"] == 4


async def test_el_tope_de_k_no_pisa_el_resto_de_settings(app_client) -> None:
    tid, _ = await _negocio()
    h = await api_headers(app_client, tid)
    await _sql(tid, "update tenants set settings = settings || '{\"pruebas\": {\"k_por_defecto\": 2}, \"otra\": 1}'::jsonb where id = :t", t=tid)
    ruta = f"/api/v1/admin/platform/tenants/{tid}/pruebas"
    assert (await app_client.put(ruta, headers=h, json={"k_maximo": 3})).status_code == 200
    assert (await app_client.put(ruta, headers=h, json={"k_maximo": None})).status_code == 200
    (fila,) = await _sql(tid, "select settings->'pruebas', settings->'otra' from tenants where id = :t", t=tid)
    assert fila[0] == {"k_por_defecto": 2} and fila[1] == 1


async def test_sin_saldo_ni_cuota_es_409_plan_limit_creditos_y_no_crea(app_client) -> None:
    tid, agente = await _negocio("free")
    ids = await _escenarios(tid, agente, 10)
    h = await api_headers(app_client, tid)
    await _consumo(tid, "2499.5", kind="turn")
    await _consumo(tid, "300.0")  # la cuota gratis también está gastada
    cuerpo = (await _estimar(app_client, h, agente, ids)).json()
    assert cuerpo["cuota_gratis_restante"] == 0 and cuerpo["por_k"][0]["alcanza"] is False
    r = await _lanzar(app_client, h, agente, ids, k=1, confirmo=cuerpo["por_k"][0]["estimado"])
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_limit_creditos" and error["details"]["metric"] == "creditos"
    assert await _corridas(tid) == []


async def test_una_corrida_activa_por_negocio_tambien_con_dos_peticiones_a_la_vez(app_client) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 2)
    h = await api_headers(app_client, tid)
    estimado = (await _estimar(app_client, h, agente, ids)).json()["por_k"][0]["estimado"]
    a, b = await asyncio.gather(
        _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado), _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado)
    )
    assert sorted([a.status_code, b.status_code]) == [202, 409], (a.text, b.text)
    assert [r.json()["error"]["code"] for r in (a, b) if r.status_code == 409] == ["corrida_en_curso"]
    assert len(await _corridas(tid)) == 1
    c = await _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado)
    assert c.status_code == 409 and c.json()["error"]["code"] == "corrida_en_curso"
    # Terminada la anterior, el cupo se libera.
    await _sql(tid, "update agent_test_runs set estado = 'terminada', iniciada_en = now(), terminada_en = now() where tenant_id = :t", t=tid)
    assert (await _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado)).status_code == 202


async def test_cinco_corridas_por_dia(app_client) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 1)
    h = await api_headers(app_client, tid)
    estimado = (await _estimar(app_client, h, agente, ids)).json()["por_k"][0]["estimado"]
    for _ in range(5):
        r = await _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado)
        assert r.status_code == 202, r.text
        await _sql(tid, "update agent_test_runs set estado = 'terminada', iniciada_en = now(), terminada_en = now() where tenant_id = :t", t=tid)
    sexta = await _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado)
    assert sexta.status_code == 429 and sexta.json()["error"]["code"] == "corridas_por_dia"
    # Las de hace más de un día ya no cuentan.
    await _sql(tid, "update agent_test_runs set created_at = :antes where tenant_id = :t", t=tid, antes=datetime.now(UTC) - timedelta(days=2))
    assert (await _lanzar(app_client, h, agente, ids, k=1, confirmo=estimado)).status_code == 202


async def test_escenarios_validos_maximo_20_aprobados_y_del_agente(app_client) -> None:
    tid, agente = await _negocio()
    h = await api_headers(app_client, tid)
    veintiuno = await _escenarios(tid, agente, 21)
    r = await _estimar(app_client, h, agente, veintiuno)
    assert r.status_code == 422 and r.json()["error"]["code"] == "demasiados_escenarios"
    assert (await _estimar(app_client, h, agente, veintiuno[:20])).status_code == 200
    # Repetidos cuentan una vez.
    assert (await _estimar(app_client, h, agente, [veintiuno[0]] * 3)).json()["escenarios"] == 1
    borrador = await _escenarios(tid, agente, 1, estado="borrador")
    r = await _estimar(app_client, h, agente, [veintiuno[0], *borrador])
    assert r.status_code == 422 and r.json()["error"]["code"] == "escenario_no_aprobado"
    assert (await _estimar(app_client, h, agente, [])).status_code == 422
    # Un escenario borrado ya no existe para la corrida.
    await _sql(tid, "update agent_test_cases set deleted_at = now() where id = :i", i=uuid.UUID(veintiuno[1]))
    r = await _estimar(app_client, h, agente, [veintiuno[1]])
    assert r.status_code == 404 and r.json()["error"]["code"] == "escenario_no_encontrado"


async def test_aislamiento_entre_negocios_agente_y_escenarios_ajenos_son_404(app_client) -> None:
    a, agente_a = await _negocio()
    b, agente_b = await _negocio()
    ids_a = await _escenarios(a, agente_a, 2)
    ids_b = await _escenarios(b, agente_b, 2)
    h_a = await api_headers(app_client, a)
    h_b = await api_headers(app_client, b)
    # B pide con el agente y los escenarios de A.
    assert (await _estimar(app_client, h_b, agente_a, ids_a)).status_code == 404
    assert (await _lanzar(app_client, h_b, agente_a, ids_a, k=1, confirmo=1)).status_code == 404
    # B pide con su agente pero con escenarios de A: no existen para B.
    r = await _estimar(app_client, h_b, agente_b, ids_a)
    assert r.status_code == 404 and r.json()["error"]["code"] == "escenario_no_encontrado"
    r = await _lanzar(app_client, h_b, agente_b, [*ids_b, *ids_a], k=1, confirmo=1)
    assert r.status_code == 404
    # Escenarios del otro agente del mismo negocio tampoco.
    _, otro_agente = a, None
    from core.db.models import AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(a), get_session() as session:
        segundo = AgentRecord(tenant_id=a, key=f"agenda_{uuid.uuid4().hex[:6]}", kind="specialist", name="Agenda")
        session.add(segundo)
        await session.flush()
        otro_agente = segundo.id
    assert (await _estimar(app_client, h_a, otro_agente, ids_a)).status_code == 404
    assert await _corridas(a) == [] and await _corridas(b) == []
    # La cuota de uno no es la del otro.
    await _consumo(a, "250.0")
    assert (await _estimar(app_client, h_b, agente_b, ids_b)).json()["cuota_gratis_restante"] == 300
    assert (await _estimar(app_client, h_a, agente_a, ids_a)).json()["cuota_gratis_restante"] == 50


async def test_sin_permiso_de_agentes_es_403_y_con_la_bandera_apagada_404(app_client, monkeypatch) -> None:
    tid, agente = await _negocio()
    ids = await _escenarios(tid, agente, 1)
    sin = await api_headers(app_client, tid, email=await _dueno(tid, ("agents",)), password=PASSWORD)
    assert (await _estimar(app_client, sin, agente, ids)).status_code == 403
    assert (await _lanzar(app_client, sin, agente, ids, k=1, confirmo=1)).status_code == 403
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "pruebas_agente_enabled", False)
    h = await api_headers(app_client, tid)
    assert (await _estimar(app_client, h, agente, ids)).status_code == 404
    assert (await _lanzar(app_client, h, agente, ids, k=1, confirmo=1)).status_code == 404
    assert await _corridas(tid) == []


async def test_el_negocio_con_llave_propia_paga_menos_el_agente_pero_no_el_simulador_ni_el_juez(app_client, monkeypatch) -> None:
    from core.billing import pruebas

    base_plataforma = pruebas.estimado_de_k1(10, "estandar", "platform")
    base_byok = pruebas.estimado_de_k1(10, "estandar", "byok")
    assert 0 < base_byok < base_plataforma  # el simulador y el juez siguen costando con la llave de la plataforma
    assert pruebas.estimado_de_k1(10, "avanzado", "platform") > base_plataforma
    assert pruebas.tope_de_creditos(100) == Decimal("150.0")
