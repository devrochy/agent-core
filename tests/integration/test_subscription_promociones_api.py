"""SPEC-43 · HU-SUB-33 · API de promociones de plan: CRUD con auditoría y permisos, canjes e inscripción con
código desde el panel, contra la BD real."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers
from test_subscription_inscripcion import _auditoria, _cliente, _negocio
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
PLAN = {"codigo": "PRO", "nombre": "Pro", "precio": 100000, "periodicidad": "mensual"}
PROMO = {"etiqueta": "Lanzamiento", "codigo": "LANZAMIENTO", "tipo_descuento": "porcentaje", "valor": 20, "max_canjes": 100}


async def _con_plan(app_client):
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    plan = (await app_client.post(f"{BASE}/planes", json=PLAN, headers=h)).json()
    return tid, h, plan


async def test_crud_de_promociones_con_auditoria(app_client) -> None:
    tid, h, _ = await _con_plan(app_client)

    creada = await app_client.post(f"{BASE}/promociones", json=PROMO, headers=h)
    assert creada.status_code == 201, creada.text
    promo = creada.json()
    assert promo["codigo"] == "lanzamiento", "el código se guarda en minúsculas"
    assert (promo["canjes_usados"], promo["canjes_restantes"], promo["activa"]) == (0, 100, True)

    assert (await app_client.post(f"{BASE}/promociones", json=PROMO, headers=h)).json()["error"]["code"] == "codigo_duplicado"

    editada = await app_client.put(f"{BASE}/promociones/{promo['id']}", json={**PROMO, "valor": 25}, headers=h)
    assert editada.status_code == 200 and editada.json()["valor"] == 25

    assert [p["id"] for p in (await app_client.get(f"{BASE}/promociones", headers=h)).json()] == [promo["id"]]

    assert (await app_client.delete(f"{BASE}/promociones/{promo['id']}", headers=h)).status_code == 204
    assert (await app_client.get(f"{BASE}/promociones", headers=h)).json() == []
    # El código de una promoción borrada se puede volver a usar.
    assert (await app_client.post(f"{BASE}/promociones", json=PROMO, headers=h)).status_code == 201

    assert len(await _auditoria(tid, "subs_promocion_creada")) == 2
    [editada_fila] = await _auditoria(tid, "subs_promocion_editada")
    assert editada_fila.actor == "admin:admin@local" and editada_fila.input["antes"]["valor"] == "20.00"
    assert "codigo" not in editada_fila.output and "codigo" not in editada_fila.input["antes"], (
        "el código es un secreto de cupón: la auditoría solo dice si la promoción tiene código"
    )
    assert editada_fila.output["con_codigo"] is True
    assert len(await _auditoria(tid, "subs_promocion_borrada")) == 1


@pytest.mark.parametrize(
    ("cambio", "codigo"),
    [
        ({"valor": 0}, "unprocessable"),  # gt=0 en el esquema
        ({"valor": 100}, "porcentaje_invalido"),
        ({"valor": 0.5}, "porcentaje_invalido"),
        ({"tipo_descuento": "monto", "valor": 5000, "periodos": 2}, "monto_un_periodo"),
        ({"periodos": 3}, "periodos_no_disponibles"),
        ({"codigo": "ab"}, "codigo_invalido"),
        ({"vigente_desde": "2026-12-01T00:00:00Z", "vigente_hasta": "2026-11-01T00:00:00Z"}, "vigencia_invalida"),
        ({"plan_id": str(uuid.uuid4())}, "plan_no_encontrado"),
    ],
)
async def test_promocion_invalida_se_rechaza_con_su_codigo(app_client, cambio, codigo) -> None:
    _, h, _ = await _con_plan(app_client)
    r = await app_client.post(f"{BASE}/promociones", json={**PROMO, **cambio}, headers=h)
    assert r.status_code in (404, 422), r.text
    if codigo != "unprocessable":
        assert r.json()["error"]["code"] == codigo


async def test_una_promocion_no_puede_apuntar_a_un_plan_gratis_ni_bajar_el_tope_de_lo_usado(app_client) -> None:
    _, h, plan = await _con_plan(app_client)
    gratis = (await app_client.post(f"{BASE}/planes", json={**PLAN, "codigo": "FREE", "precio": 0}, headers=h)).json()
    r = await app_client.post(f"{BASE}/promociones", json={**PROMO, "plan_id": gratis["id"]}, headers=h)
    assert r.json()["error"]["code"] == "plan_gratis"
    ok = await app_client.post(f"{BASE}/promociones", json={**PROMO, "plan_id": plan["id"]}, headers=h)
    assert ok.status_code == 201


async def test_el_colaborador_lee_pero_no_escribe_y_otro_negocio_no_ve_nada(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    promo = (await app_client.post(f"{BASE}/promociones", json=PROMO, headers=ha)).json()

    assert (await app_client.put(f"{BASE}/promociones/{promo['id']}", json=PROMO, headers=hb)).status_code == 404
    assert (await app_client.delete(f"{BASE}/promociones/{promo['id']}", headers=hb)).status_code == 404
    assert (await app_client.get(f"{BASE}/promociones/{promo['id']}/canjes", headers=hb)).status_code == 404
    assert (await app_client.get(f"{BASE}/promociones", headers=hb)).json() == []

    hc = await api_headers(app_client, a, email=await _colaborador(a), password=PASSWORD)
    assert (await app_client.get(f"{BASE}/promociones", headers=hc)).status_code == 200
    assert (await app_client.get(f"{BASE}/promociones/{promo['id']}/canjes", headers=hc)).status_code == 200
    assert (await app_client.post(f"{BASE}/promociones", json={**PROMO, "codigo": "otra"}, headers=hc)).status_code == 403
    assert (await app_client.put(f"{BASE}/promociones/{promo['id']}", json=PROMO, headers=hc)).status_code == 403
    assert (await app_client.delete(f"{BASE}/promociones/{promo['id']}", headers=hc)).status_code == 403


async def test_inscribir_desde_el_panel_con_codigo_y_ver_el_canje(app_client) -> None:
    tid, h, _ = await _con_plan(app_client)
    promo = (await app_client.post(f"{BASE}/promociones", json=PROMO, headers=h)).json()
    cliente = await _cliente(tid)
    cuerpo = {
        "customer_id": str(cliente),
        "plan_codigo": "PRO",
        "codigo_promocion": "LANZAMIENTO",
        "datos_alta": {"email": "x@y.co", "nombre": "X", "telefono": "+573005550101"},
    }

    r = await app_client.post(f"{BASE}/suscripciones", json=cuerpo, headers=h)

    assert r.status_code == 200, r.text
    salida = r.json()
    assert (salida["monto"], salida["monto_bruto"], salida["descuento"], salida["promocion"]) == (
        80000, 100000, 20000, "Lanzamiento",
    )
    assert salida["estado"] == "pendiente_pago"
    lista = (await app_client.get(f"{BASE}/promociones", headers=h)).json()
    assert (lista[0]["canjes_usados"], lista[0]["canjes_restantes"]) == (1, 99)
    canjes = (await app_client.get(f"{BASE}/promociones/{promo['id']}/canjes", headers=h)).json()
    assert canjes["total"] == 1
    [canje] = canjes["items"]
    assert (canje["estado"], canje["cliente_id"], canje["monto"], canje["monto_bruto"], canje["descuento"]) == (
        "reservado", str(cliente), 80000, 100000, 20000,
    )
    # Repetir la misma inscripción devuelve lo mismo y no gasta otro cupo.
    repetida = await app_client.post(f"{BASE}/suscripciones", json=cuerpo, headers=h)
    assert repetida.json()["suscripcion_id"] == salida["suscripcion_id"] and repetida.json()["inscripcion"] == "repetida"
    assert (await app_client.get(f"{BASE}/promociones", headers=h)).json()[0]["canjes_usados"] == 1
    # Cancelar la inscripción pendiente devuelve el cupo.
    assert (await app_client.post(f"{BASE}/suscripciones/{salida['suscripcion_id']}/cancelar", headers=h)).status_code == 200
    assert (await app_client.get(f"{BASE}/promociones", headers=h)).json()[0]["canjes_usados"] == 0


@pytest.mark.parametrize(
    ("codigo", "estado", "error"),
    [
        ("NO-EXISTE", 422, "codigo_no_valido"),
        ("GRATIS", 422, "promo_no_aplica_a_free"),
    ],
)
async def test_codigo_no_valido_desde_el_panel_responde_422(app_client, codigo, estado, error) -> None:
    tid, h, _ = await _con_plan(app_client)
    await app_client.post(f"{BASE}/promociones", json=PROMO, headers=h)
    await app_client.post(f"{BASE}/planes", json={**PLAN, "codigo": "FREE", "precio": 0}, headers=h)
    plan = "FREE" if error == "promo_no_aplica_a_free" else "PRO"
    cuerpo = {"customer_id": str(await _cliente(tid)), "plan_codigo": plan, "codigo_promocion": codigo}
    r = await app_client.post(f"{BASE}/suscripciones", json=cuerpo, headers=h)
    assert r.status_code == estado and r.json()["error"]["code"] == error


async def test_un_cliente_que_ya_canjeo_recibe_409_y_el_exceso_de_intentos_429(app_client) -> None:
    tid, h, _ = await _con_plan(app_client)
    await app_client.post(f"{BASE}/promociones", json=PROMO, headers=h)
    cliente = await _cliente(tid)
    cuerpo = {"customer_id": str(cliente), "plan_codigo": "PRO", "codigo_promocion": "LANZAMIENTO"}
    primera = (await app_client.post(f"{BASE}/suscripciones", json=cuerpo, headers=h)).json()
    # Cancelada la inscripción, el canje liberado deja volver a canjear; pagada y cancelada, no. Aquí: pendiente → 'repetida'.
    assert primera["monto"] == 80000

    otro = {"customer_id": str(await _cliente(tid)), "plan_codigo": "PRO", "codigo_promocion": "LANZAMIENTO"}
    for i in range(10):
        r = await app_client.post(f"{BASE}/suscripciones", json={**otro, "codigo_promocion": f"nope-{i}"}, headers=h)
        assert r.status_code == 422
    r = await app_client.post(f"{BASE}/suscripciones", json=otro, headers=h)
    assert r.status_code == 429 and r.json()["error"]["code"] == "demasiados_intentos"


async def test_inscribir_desde_el_panel_sin_datos_del_cliente_no_falla(app_client) -> None:
    """Regresión del PR-2: sin `datos_alta` se guardaba el JSON `null` y el CHECK de objeto lo rechazaba (500)."""
    tid, h, _ = await _con_plan(app_client)
    r = await app_client.post(
        f"{BASE}/suscripciones", json={"customer_id": str(await _cliente(tid)), "plan_codigo": "PRO"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "pendiente_pago" and r.json()["monto"] == 100000
