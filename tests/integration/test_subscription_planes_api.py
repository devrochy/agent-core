"""SPEC-43 · API del catálogo de planes, inscripción desde el panel, siembra y fuente de altas.

Cubre HU-SUB-27 (CRUD con auditoría y permisos), HU-SUB-28 (endpoints de inscripción y confirmación
manual), HU-SUB-30 (la fuente de altas que lee la inscripción) y HU-SUB-32 (siembra todo o nada y
sincronización de precios), contra la BD real.
"""

from __future__ import annotations

import os
import types
import uuid
from decimal import Decimal

import pytest
from _api_helpers import api_headers
from test_subscription_inscripcion import (
    _auditoria,
    _cargos,
    _cliente,
    _inscribir,
    _negocio,
    _pago,
    _pendiente,
    _plan,
    _sub,
)
from test_subscriptions_admin_ui import _colaborador

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/pack/pack_subscriptions"
PASSWORD = "Colab-Subs-2026!"
PLAN = {"codigo": "PRO", "nombre": "Pro", "precio": 150000, "periodicidad": "mensual", "entitlements": {"clientes": 500}}


# ============================================================================= CRUD de planes
async def test_crud_de_planes_con_auditoria(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)

    creado = await app_client.post(f"{BASE}/planes", json=PLAN, headers=h)
    assert creado.status_code == 201, creado.text
    plan = creado.json()
    assert plan["codigo"] == "PRO" and plan["entitlements"] == {"clientes": 500} and plan["activo"] is True

    assert (await app_client.post(f"{BASE}/planes", json=PLAN, headers=h)).json()["error"]["code"] == "codigo_duplicado"

    editado = await app_client.put(f"{BASE}/planes/{plan['id']}", json={**PLAN, "precio": 170000}, headers=h)
    assert editado.status_code == 200 and editado.json()["precio"] == 170000

    lista = (await app_client.get(f"{BASE}/planes", headers=h)).json()
    assert [p["codigo"] for p in lista] == ["PRO"]

    assert (await app_client.delete(f"{BASE}/planes/{plan['id']}", headers=h)).status_code == 204
    assert (await app_client.get(f"{BASE}/planes", headers=h)).json() == []
    # El código de un plan borrado se puede volver a usar (único parcial por vigentes).
    assert (await app_client.post(f"{BASE}/planes", json=PLAN, headers=h)).status_code == 201

    assert len(await _auditoria(tid, "subs_plan_creado")) == 2
    [editada] = await _auditoria(tid, "subs_plan_editado")
    assert editada.actor == "admin:admin@local" and editada.input["antes"]["precio"] == "150000.00"
    assert len(await _auditoria(tid, "subs_plan_borrado")) == 1


@pytest.mark.parametrize(
    ("cambio", "codigo"),
    [
        ({"trial_dias": 14}, "prueba_en_plan_de_pago"),  # D-16: solo $0 tiene prueba
        ({"codigo": "con espacio"}, "codigo_invalido"),
        ({"plan_plataforma": "no-existe"}, "plan_plataforma_desconocido"),
    ],
)
async def test_el_catalogo_rechaza_planes_invalidos_con_su_codigo(app_client, cambio, codigo) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    r = await app_client.post(f"{BASE}/planes", json={**PLAN, **cambio}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == codigo, r.text


async def test_el_precio_negativo_o_no_finito_se_rechaza(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    assert (await app_client.post(f"{BASE}/planes", json={**PLAN, "precio": -1}, headers=h)).status_code == 422
    assert (await app_client.post(f"{BASE}/planes", json={**PLAN, "entitlements": []}, headers=h)).status_code == 422


async def test_editar_un_plan_no_toca_las_suscripciones_ya_inscritas(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    plan = (await app_client.post(f"{BASE}/planes", json=PLAN, headers=h)).json()
    cliente = await _cliente(tid)
    vista = await _inscribir(tid, cliente, "PRO")
    await app_client.put(f"{BASE}/planes/{plan['id']}", json={**PLAN, "nombre": "Pro Plus", "precio": 999000}, headers=h)
    sub = await _sub(tid, uuid.UUID(vista["subscription_id"]))
    assert sub.concepto == "Pro" and Decimal(sub.valor_periodo) == Decimal("150000")
    cargo = (await _cargos(tid, sub.id))[0]
    assert Decimal(cargo.monto) == Decimal("150000")


async def test_planes_de_otro_negocio_responden_404_y_el_colaborador_solo_lee(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    plan = (await app_client.post(f"{BASE}/planes", json=PLAN, headers=ha)).json()
    assert (await app_client.put(f"{BASE}/planes/{plan['id']}", json=PLAN, headers=hb)).status_code == 404
    assert (await app_client.delete(f"{BASE}/planes/{plan['id']}", headers=hb)).status_code == 404
    assert (await app_client.get(f"{BASE}/planes", headers=hb)).json() == []

    hc = await api_headers(app_client, a, email=await _colaborador(a), password=PASSWORD)
    assert (await app_client.get(f"{BASE}/planes", headers=hc)).status_code == 200
    assert (await app_client.post(f"{BASE}/planes", json={**PLAN, "codigo": "OTRO"}, headers=hc)).status_code == 403
    assert (await app_client.delete(f"{BASE}/planes/{plan['id']}", headers=hc)).status_code == 403


# ============================================================================= inscripción desde el panel
async def test_inscribir_desde_el_panel_y_repetir_devuelve_lo_mismo(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    await app_client.post(f"{BASE}/planes", json=PLAN, headers=h)
    cliente = await _cliente(tid)
    cuerpo = {"customer_id": str(cliente), "plan_codigo": "PRO", "datos_alta": {"email": "x@y.co", "nombre": "X"}}

    r1 = await app_client.post(f"{BASE}/suscripciones", json=cuerpo, headers=h)
    r2 = await app_client.post(f"{BASE}/suscripciones", json=cuerpo, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    a, b = r1.json(), r2.json()
    assert a["estado"] == "pendiente_pago" and a["creada"] is False and a["monto"] == 150000
    assert a["inscripcion"] == "nueva" and b["inscripcion"] == "repetida"
    assert a["suscripcion_id"] == b["suscripcion_id"] and a["numero_publico"] == b["numero_publico"] == "SUB-0001"
    # Sin pasarela configurada: no hay link, pero la inscripción existe y reintenta con `charge_link`.
    assert a["link_pago"] is None

    estados = (await app_client.get(f"{BASE}/suscripciones", params={"estado": "pendiente_pago"}, headers=h)).json()
    assert [s["id"] for s in estados["items"]] == [a["suscripcion_id"]]
    assert (await app_client.get(f"{BASE}/metricas", headers=h)).json()["pendientes_de_pago"] == 1


async def test_inscribir_con_cliente_o_plan_inexistentes_responde_404(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    ha = await api_headers(app_client, a)
    await app_client.post(f"{BASE}/planes", json=PLAN, headers=ha)
    cliente_b = await _cliente(b)
    # Cliente de otro negocio: no existe para este.
    r = await app_client.post(f"{BASE}/suscripciones", json={"customer_id": str(cliente_b), "plan_codigo": "PRO"}, headers=ha)
    assert r.status_code == 404
    cliente_a = await _cliente(a)
    r = await app_client.post(f"{BASE}/suscripciones", json={"customer_id": str(cliente_a), "plan_codigo": "NOPE"}, headers=ha)
    assert r.status_code == 404 and r.json()["error"]["code"] == "plan_no_encontrado"
    r = await app_client.post(f"{BASE}/suscripciones", json={"customer_id": str(cliente_a)}, headers=ha)
    assert r.status_code == 422  # el plan es obligatorio


async def test_el_plan_libre_del_contrato_v1_ya_no_existe(app_client) -> None:
    """HU-COM-17: el plan libre (nombre, precio, periodicidad) se retiró; sin `plan_codigo` es un 422."""
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    cliente = await _cliente(tid)
    r = await app_client.post(
        f"{BASE}/suscripciones",
        json={"customer_id": str(cliente), "nombre": "Plan libre", "precio": 50000, "periodicidad": "mensual"},
        headers=h,
    )
    assert r.status_code == 422, r.text


async def test_confirmar_pago_por_la_api(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    _, _, sub, _ = await _pendiente(a)
    ruta = f"{BASE}/suscripciones/{sub.id}/confirmar-pago"

    assert (await app_client.post(ruta, json={"referencia": "COMP-1"}, headers=hb)).status_code == 404
    assert (await app_client.post(ruta, json={"referencia": "x"}, headers=ha)).status_code == 422
    r1 = await app_client.post(ruta, json={"referencia": "COMP-1"}, headers=ha)
    r2 = await app_client.post(ruta, json={"referencia": "COMP-1"}, headers=ha)
    assert r1.status_code == 200 and r1.json()["estado"] == "confirmado" and r1.json()["suscripcion_estado"] == "activa"
    assert r2.json()["estado"] == "ya_confirmado"
    assert (await app_client.post(ruta, json={"referencia": "OTRA-2"}, headers=ha)).json()["error"]["code"] == "cargo_ya_pagado"
    [fila] = await _auditoria(a, "subs_pago_inscripcion_confirmado")
    assert fila.actor == "admin:admin@local" and fila.input["referencia"] == "COMP-1"

    hc = await api_headers(app_client, a, email=await _colaborador(a), password=PASSWORD)
    assert (await app_client.post(ruta, json={"referencia": "COMP-9"}, headers=hc)).status_code == 403


async def test_cancelar_una_pendiente_anula_su_cargo_y_no_es_baja(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    _, _, sub, _ = await _pendiente(tid)
    r = await app_client.post(f"{BASE}/suscripciones/{sub.id}/cancelar", headers=h)
    assert r.status_code == 200 and r.json()["estado"] == "cancelada"
    assert (await _cargos(tid, sub.id))[0].estado == "anulado"
    m = (await app_client.get(f"{BASE}/metricas", headers=h)).json()
    assert m["bajas_mes"] == 0 and m["pendientes_de_pago"] == 0
    # Pausar una pendiente no aplica.
    _, _, otra, _ = await _pendiente(tid)
    assert (await app_client.post(f"{BASE}/suscripciones/{otra.id}/pausar", headers=h)).status_code == 409


# ============================================================================= fuente de altas (HU-SUB-30)
async def test_la_fuente_de_altas_lee_la_inscripcion_por_su_numero() -> None:
    from pack_subscriptions.altas import DatosDeAltaDeLaInscripcion

    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    await _plan(a, "BASIC", "99000", plan_plataforma="basic")
    _, vista, sub, cargo = await _pendiente(a)
    fuente = DatosDeAltaDeLaInscripcion()
    async with tenant_scope(a):
        pendiente = await fuente.datos_de_alta(vista["numero_publico"])
        assert pendiente is not None and pendiente["pagado"] is False and pendiente["origen"] == "panel"
        assert pendiente["plan"] == "basic" and pendiente["email"] == "ana@ejemplo.co"
        assert pendiente["customer_id"] == str(sub.customer_id)
        assert await fuente.plan_de_inscripcion(str(sub.plan_id)) == "basic"
        assert await fuente.datos_de_alta("SUB-9999") is None
    await _pago(a, cargo.id, 99000.0)
    async with tenant_scope(a):
        assert (await fuente.datos_de_alta(vista["numero_publico"]))["pagado"] is True
    async with tenant_scope(b):  # otro negocio no ve la referencia
        assert await fuente.datos_de_alta(vista["numero_publico"]) is None
        assert await fuente.plan_de_inscripcion(str(sub.plan_id)) is None


async def test_una_inscripcion_del_panel_espera_aprobacion_antes_del_alta(monkeypatch) -> None:
    from pack_subscriptions.altas import DatosDeAltaDeLaInscripcion

    from core.provisioning import AltaEnRevision, consumer
    from core.provisioning.fuentes import fuentes_de_alta
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await _plan(tid, "BASIC", "99000", plan_plataforma="basic")
    _, vista, sub, cargo = await _pendiente(tid)
    await _pago(tid, cargo.id, 99000.0)

    async def _fuentes(_tid):
        return [DatosDeAltaDeLaInscripcion()]

    monkeypatch.setattr(fuentes_de_alta, "del_negocio", _fuentes)
    registro = types.SimpleNamespace(referencia=vista["numero_publico"], detalle={}, customer_id=sub.customer_id)
    async with tenant_scope(tid):
        with pytest.raises(AltaEnRevision):
            await consumer._datos_del_pedido(tid, registro)
        # Con la aprobación de una persona (`detalle.aprobacion`) pasa y trae los datos del comprador.
        registro.detalle = {"aprobacion": {"por": "admin:a@x.co"}}
        datos = await consumer._datos_del_pedido(tid, registro)
    assert datos["plan"] == "basic" and datos["email"] == "ana@ejemplo.co"


async def test_un_pedido_de_agente_no_pasa_por_revision_y_un_impago_espera(monkeypatch) -> None:
    from pack_subscriptions.altas import DatosDeAltaDeLaInscripcion

    from core.provisioning import AltaTransitoria, consumer
    from core.provisioning.fuentes import fuentes_de_alta
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await _plan(tid, "BASIC", "99000", plan_plataforma="basic")
    cliente = await _cliente(tid)
    vista = await _inscribir(tid, cliente, "BASIC", origen="panel")

    async def _fuentes(_tid):
        return [DatosDeAltaDeLaInscripcion()]

    monkeypatch.setattr(fuentes_de_alta, "del_negocio", _fuentes)
    registro = types.SimpleNamespace(
        referencia=vista["numero_publico"], detalle={"aprobacion": {"por": "x"}}, customer_id=cliente
    )
    async with tenant_scope(tid):
        with pytest.raises(AltaTransitoria):  # sigue pendiente_pago: el pedido todavía no está pagado
            await consumer._datos_del_pedido(tid, registro)


async def test_plan_plataforma_de_pregunta_a_cada_fuente_hasta_que_una_responde() -> None:
    from core.provisioning.fuentes import plan_plataforma_de

    class SinOperacion:
        provider_id = "otro_proveedor"

    class Subs:
        provider_id = "pack_subscriptions"

        async def plan_de_inscripcion(self, plan_id):
            return "basic" if plan_id == "p1" else None

    fuentes = [SinOperacion(), Subs()]
    assert await plan_plataforma_de(fuentes, plan_id="p1") == "basic"
    assert await plan_plataforma_de(fuentes, plan_id="otro") is None
    assert await plan_plataforma_de([SinOperacion()], plan_id="p1") is None  # una fuente sin la operación se salta


# ============================================================================= siembra y precios (HU-SUB-32)
SEMBRADO = {
    "planes": [
        {"codigo": "FREE", "nombre": "Free", "plan_plataforma": "free", "trial_dias": 30, "orden": 0},
        {"codigo": "BASIC", "nombre": "Basic", "plan_plataforma": "basic", "precio": 1, "orden": 1},
    ]
}


async def test_la_siembra_es_idempotente_y_el_precio_sigue_a_la_plataforma() -> None:
    from pack_subscriptions import planes
    from pack_subscriptions.seed import seed_planes

    from core.billing.plans import get_plan
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await seed_planes(tid, SEMBRADO)
    pasos = await seed_planes(tid, SEMBRADO)
    assert pasos == ["plan FREE actualizado", "plan BASIC actualizado"]
    async with tenant_scope(tid):
        lista = await planes.listar(incluir_inactivos=True)
    assert [p.codigo for p in lista] == ["FREE", "BASIC"]
    assert Decimal(lista[1].precio) == Decimal(get_plan("basic").price_cop)  # el `precio: 1` del archivo se ignora
    assert lista[0].trial_dias == 30 and Decimal(lista[0].precio) == 0


async def test_la_siembra_es_todo_o_nada() -> None:
    from pack_subscriptions import planes
    from pack_subscriptions.seed import SiembraInvalida, seed_planes

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    malo = {"planes": [*SEMBRADO["planes"], {"codigo": "X", "nombre": "X", "precio": 10, "trial_dias": 5}]}
    with pytest.raises(SiembraInvalida):
        await seed_planes(tid, malo)
    async with tenant_scope(tid):
        assert await planes.listar(incluir_inactivos=True) == []
    for roto in (
        {"planes": [{"codigo": "A", "nombre": "A", "plan_plataforma": "inexistente"}]},
        {"planes": [{"codigo": "A", "nombre": "A", "precio": 1}, {"codigo": "a", "nombre": "B", "precio": 2}]},
        {"planes": []},
        {},
    ):
        with pytest.raises(SiembraInvalida):
            await seed_planes(tid, roto)


async def test_el_cron_actualiza_el_precio_y_respeta_la_prueba(monkeypatch) -> None:
    import dataclasses

    from pack_subscriptions import planes
    from pack_subscriptions.jobs import _sincronizar_precios_de_plan
    from pack_subscriptions.seed import seed_planes

    from core.billing import plans as plataforma
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await seed_planes(tid, SEMBRADO)
    # Un plan «manual» (sin plan de plataforma) no se toca nunca.
    await _plan(tid, "MANUAL", "12345")
    original = plataforma.get_plan
    basic = original("basic")
    mas_caro = dataclasses.replace(basic, price_cop=basic.price_cop + 5000)
    monkeypatch.setattr(plataforma, "get_plan", lambda pid: mas_caro if pid == "basic" else original(pid))
    await _sincronizar_precios_de_plan()
    async with tenant_scope(tid):
        por_codigo = {p.codigo: p for p in await planes.listar(incluir_inactivos=True)}
    assert Decimal(por_codigo["BASIC"].precio) == Decimal(basic.price_cop + 5000)
    assert Decimal(por_codigo["MANUAL"].precio) == Decimal("12345")

    # Free con prueba: si la plataforma le pusiera precio, el cron NO lo cambia (D-16) y no revienta.
    free_caro = dataclasses.replace(original("free"), price_cop=1000)
    monkeypatch.setattr(plataforma, "get_plan", lambda pid: free_caro if pid == "free" else original(pid))
    await _sincronizar_precios_de_plan()
    async with tenant_scope(tid):
        free = await planes.por_codigo("FREE")
    assert Decimal(free.precio) == 0 and free.trial_dias == 30


async def test_el_seed_generico_del_core_encuentra_planes_json(tmp_path, monkeypatch) -> None:
    """`tenant seed` también lee `planes.json` (además de `<pack>.json`)."""
    import json

    from pack_subscriptions import planes

    from core import onboarding
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    slug = f"s-{uuid.uuid4().hex[:6]}"
    carpeta = tmp_path / slug
    carpeta.mkdir()
    (carpeta / "planes.json").write_text(json.dumps(SEMBRADO), encoding="utf-8")
    monkeypatch.setattr(onboarding, "tenant_dir", lambda s: tmp_path / s)
    monkeypatch.setattr(onboarding, "_validate_slug", lambda s: None)
    pasos = await onboarding.sembrar_packs(tid, slug)
    assert "planes.json importado" in pasos
    async with tenant_scope(tid):
        assert [p.codigo for p in await planes.listar(incluir_inactivos=True)] == ["FREE", "BASIC"]


# ============================================================================= consumer: alta y cambio con la misma referencia
async def test_el_consumer_trata_cambio_de_plan_aunque_la_referencia_sea_la_misma(monkeypatch) -> None:
    """El `numero_publico` es el mismo en el alta y en el cambio: solo la bandera `cambio_de_plan` los distingue."""
    from core.db.models import ProvisioningRequest
    from core.db.session import get_session
    from core.provisioning import consumer
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    sub_id, cliente, plan_id = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    llamadas: list[tuple] = []

    async def procesar(vendedor, registro):
        return registro

    async def sincronizar(vendedor, registro, payload, referencia):
        llamadas.append((referencia, payload.get("plan_id")))
        return {}

    monkeypatch.setattr(consumer, "_procesar", procesar)
    monkeypatch.setattr(consumer, "sincronizar_cambio_de_plan", sincronizar)
    base = {
        "subscription_id": str(sub_id), "customer_id": str(cliente), "referencia": "SUB-0042",
        "plan_id": plan_id, "estado": "activa",
    }
    async with tenant_scope(tid):
        async with get_session() as session:  # `subscription_id` no tiene FK: basta la fila del alta
            from sqlalchemy import select

            assert (await session.execute(select(ProvisioningRequest).where(ProvisioningRequest.tenant_id == tid))).first() is None
        await consumer.aprovisionar(tid, {**base, "cambio_de_plan": False})
        assert llamadas == []  # el alta no es un cambio
        await consumer.aprovisionar(tid, {**base, "cambio_de_plan": False})  # reentrega del outbox
        assert llamadas == []
        await consumer.aprovisionar(tid, {**base, "cambio_de_plan": True})
    assert llamadas == [("SUB-0042:cambio", plan_id)]
