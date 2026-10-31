"""SPEC-35 · administración de suscripciones (HU-SUB-08/19/20/16) contra la BD real.

Lista, detalle, pagos (cargos), clientes y métricas; acciones pausar / reanudar / cancelar /
reenviar link con auditoría del usuario; aislamiento entre negocios y permisos de solo lectura;
y la contribución del pack a la vista de Clientes (`LeadProvider`).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import api_headers, uniq
from _subs_helpers import nueva_sub, plan_por_defecto
from sqlalchemy import select

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


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("subsui")
    tid, _ = await onboard_tenant(
        name=f"Subs {slug}", slug=slug, admin_email="admin@local", packs=["pack_subscriptions"], sembrar_ejemplos=False
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str, *, con_whatsapp: bool = False) -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            c = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('c')}", name=nombre,
                contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
            )
            session.add(c)
            await session.flush()
            if con_whatsapp:
                session.add(Conversation(tenant_id=tid, customer_id=c.id, channel="whatsapp"))
            return c.id


async def _sub(tid: uuid.UUID, cliente: uuid.UUID, *, concepto="Plan Pro", estado="activa", valor="200000",
               periodicidad="mensual", moneda="COP", **campos) -> uuid.UUID:

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    campos.setdefault("fecha_alta", datetime.now(UTC) - timedelta(days=60))
    campos.setdefault("proximo_cargo", datetime.now(UTC) + timedelta(days=5))
    async with tenant_scope(tid):
        async with get_session() as session:
            sub = await nueva_sub(
                session, tid, customer_id=cliente, concepto=concepto, estado=estado, valor_periodo=Decimal(valor),
                moneda=moneda, periodicidad=periodicidad, dia_ancla=15, **campos,
            )
            await session.flush()
            return sub.id


async def _cargo(tid: uuid.UUID, sid: uuid.UUID, *, vence: datetime | None, estado="pendiente", monto="200000",
                 tipo="periodo", link="https://pago.test/abc", periodo: str | None = None) -> uuid.UUID:
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cargo = SubscriptionCharge(
                tenant_id=tid, subscription_id=sid, periodo=periodo or (vence or datetime.now(UTC)).strftime("%Y-%m"),
                monto=Decimal(monto), estado=estado, tipo=tipo, fecha_vencimiento=vence, link_url=link,
                referencia=f"sub-{uuid.uuid4()}" if link else None,
            )
            session.add(cargo)
            await session.flush()
            return cargo.id


async def _colaborador(tid: uuid.UUID) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
    return email


async def _auditoria(tid: uuid.UUID, accion: str) -> list:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list(
                (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == accion)))
                .scalars()
            )


async def _eventos(tid: uuid.UUID, topic: str) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list(
                (await session.execute(select(DomainEvent).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic)))
                .scalars()
            )


async def _escenario(tid: uuid.UUID) -> dict[str, uuid.UUID]:
    """Ana (activa, con mora), Beto (pausada) y Carla (cancelada)."""
    ahora = datetime.now(UTC)
    ana, beto, carla = await _cliente(tid, "Ana Gómez"), await _cliente(tid, "Beto Ruiz"), await _cliente(tid, "Carla Díaz")
    s_ana = await _sub(tid, ana, concepto="Plan Pro")
    c_viejo = await _cargo(tid, s_ana, vence=ahora - timedelta(days=10))
    c_futuro = await _cargo(tid, s_ana, vence=ahora + timedelta(days=3), periodo="2099-01")
    s_beto = await _sub(tid, beto, concepto="Plan Basic", estado="pausada", valor="50000", motivo_pausa="cliente",
                        proximo_cargo=None)
    s_carla = await _sub(tid, carla, concepto="Plan Pro", estado="cancelada", fecha_cancelacion=ahora - timedelta(days=2),
                         proximo_cargo=None)
    return {"ana": ana, "beto": beto, "carla": carla, "s_ana": s_ana, "s_beto": s_beto, "s_carla": s_carla,
            "c_viejo": c_viejo, "c_futuro": c_futuro}


# ----------------------------------------------------------------------------- lectura
async def test_lista_filtra_pagina_y_calcula_deuda_y_mora(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    h = await api_headers(app_client, tid)

    r = await app_client.get(f"{BASE}/suscripciones", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 3
    ana = next(i for i in data["items"] if i["id"] == str(e["s_ana"]))
    assert ana["cliente"]["nombre"] == "Ana Gómez"
    assert ana["plan"] == "Plan Pro" and ana["estado"] == "activa"
    assert ana["deuda"] == 400000 and ana["en_mora"] == 200000 and ana["cargos_pendientes"] == 2
    assert ana["dias_mora"] == 10

    por_estado = (await app_client.get(f"{BASE}/suscripciones?estado=pausada", headers=h)).json()
    assert [i["id"] for i in por_estado["items"]] == [str(e["s_beto"])]
    assert por_estado["items"][0]["motivo_pausa"] == "cliente"

    con_mora = (await app_client.get(f"{BASE}/suscripciones?con_mora=true", headers=h)).json()
    assert [i["id"] for i in con_mora["items"]] == [str(e["s_ana"])]

    por_nombre = (await app_client.get(f"{BASE}/suscripciones?q=beto", headers=h)).json()
    assert [i["id"] for i in por_nombre["items"]] == [str(e["s_beto"])]
    # `%` y `_` son texto literal, no comodines.
    assert (await app_client.get(f"{BASE}/suscripciones?q=%25", headers=h)).json()["total"] == 0

    de_un_cliente = (await app_client.get(f"{BASE}/suscripciones?cliente_id={e['carla']}", headers=h)).json()
    assert [i["id"] for i in de_un_cliente["items"]] == [str(e["s_carla"])]

    pag = (await app_client.get(f"{BASE}/suscripciones?page_size=2", headers=h)).json()
    assert len(pag["items"]) == 2 and pag["has_more"] is True and pag["total"] == 3

    invalido = await app_client.get(f"{BASE}/suscripciones?estado=inventado", headers=h)
    assert invalido.status_code == 422


async def test_detalle_trae_cargos_historial_y_acciones_validas(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    h = await api_headers(app_client, tid)

    r = await app_client.get(f"{BASE}/suscripciones/{e['s_ana']}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["cargos"]) == 2
    vencido = next(c for c in d["cargos"] if c["id"] == str(e["c_viejo"]))
    assert vencido["estado"] == "pendiente" and vencido["dias_mora"] == 10 and vencido["link_url"]
    assert d["acciones"] == ["pausar", "cancelar"]
    assert (await app_client.get(f"{BASE}/suscripciones/{e['s_beto']}", headers=h)).json()["acciones"] == ["reanudar", "cancelar"]
    assert (await app_client.get(f"{BASE}/suscripciones/{e['s_carla']}", headers=h)).json()["acciones"] == []
    assert (await app_client.get(f"{BASE}/suscripciones/{uuid.uuid4()}", headers=h)).status_code == 404


async def test_pagos_son_los_cargos_con_filtros_y_mora(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    ahora = datetime.now(UTC)
    pagado = await _cargo(tid, e["s_ana"], vence=ahora - timedelta(days=40), estado="pagado", periodo="2020-01")
    anulado = await _cargo(tid, e["s_ana"], vence=ahora - timedelta(days=1), estado="anulado", tipo="cambio_plan",
                           periodo="cambio-20201231000000")
    h = await api_headers(app_client, tid)

    todos = (await app_client.get(f"{BASE}/cargos", headers=h)).json()
    assert todos["total"] == 4
    vencidos = (await app_client.get(f"{BASE}/cargos?vencidos=true", headers=h)).json()
    assert [c["id"] for c in vencidos["items"]] == [str(e["c_viejo"])]
    assert vencidos["items"][0]["dias_mora"] == 10
    assert vencidos["items"][0]["cliente"]["nombre"] == "Ana Gómez"
    pagados = (await app_client.get(f"{BASE}/cargos?estado=pagado", headers=h)).json()
    assert [c["id"] for c in pagados["items"]] == [str(pagado)]
    assert pagados["items"][0]["dias_mora"] is None and pagados["items"][0]["link_url"] is None
    cambios = (await app_client.get(f"{BASE}/cargos?tipo=cambio_plan", headers=h)).json()
    assert [c["id"] for c in cambios["items"]] == [str(anulado)]
    rango = (
        await app_client.get(
            f"{BASE}/cargos?desde={(ahora - timedelta(days=12)).date()}&hasta={(ahora - timedelta(days=8)).date()}", headers=h
        )
    ).json()
    assert [c["id"] for c in rango["items"]] == [str(e["c_viejo"])]
    de_beto = (await app_client.get(f"{BASE}/cargos?suscripcion_id={e['s_beto']}", headers=h)).json()
    assert de_beto["total"] == 0


async def test_clientes_agrupa_planes_deuda_y_ultimo_periodo_pagado(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    ahora = datetime.now(UTC)
    otra = await _sub(
        tid, e["ana"], concepto="Plan Extra", valor="30000", plan_id=await plan_por_defecto(tid, codigo="PLAN-EXTRA")
    )
    await _cargo(tid, otra, vence=ahora - timedelta(days=40), estado="pagado", periodo="2026-09")
    h = await api_headers(app_client, tid)

    data = (await app_client.get(f"{BASE}/clientes", headers=h)).json()
    nombres = [c["cliente"]["nombre"] for c in data["items"]]
    assert "Carla Díaz" not in nombres  # solo canceladas: fuera por defecto
    ana = next(c for c in data["items"] if c["cliente"]["nombre"] == "Ana Gómez")
    assert ana["suscripciones"] == 2 and ana["vivas"] == 2
    assert sorted(ana["planes"]) == ["Plan Extra", "Plan Pro"]
    assert ana["deuda"] == 400000 and ana["en_mora"] == 200000 and ana["dias_mora"] == 10
    assert ana["ultimo_periodo_pagado"] == "2026-09"
    assert nombres[0] == "Ana Gómez"  # la mora primero

    con_canceladas = (await app_client.get(f"{BASE}/clientes?incluir_canceladas=true", headers=h)).json()
    assert con_canceladas["total"] == 3
    solo_mora = (await app_client.get(f"{BASE}/clientes?con_mora=true", headers=h)).json()
    assert [c["cliente"]["nombre"] for c in solo_mora["items"]] == ["Ana Gómez"]
    assert (await app_client.get(f"{BASE}/clientes?q=beto", headers=h)).json()["total"] == 1


async def test_metricas_con_datos_conocidos() -> None:
    """MRR normalizado, altas/bajas del mes, churn, mora por tramos y cohortes (reloj fijo)."""
    from pack_subscriptions.consultas import metricas

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    ahora = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
    tid = await _negocio()
    c = [await _cliente(tid, f"Cliente {i}") for i in range(7)]
    oct_ = datetime(2026, 10, 3, tzinfo=UTC)
    s_mensual = await _sub(tid, c[0], valor="100000", fecha_alta=oct_)
    await _sub(tid, c[1], concepto="Plan Anual", valor="1200000", periodicidad="anual", fecha_alta=oct_)
    await _sub(tid, c[2], concepto="Plan Trim", valor="300000", periodicidad="trimestral", fecha_alta=oct_)
    await _sub(tid, c[3], concepto="Free", estado="trialing", valor="0", fecha_alta=oct_)
    await _sub(tid, c[4], concepto="Plan Viejo", estado="cancelada", valor="50000", fecha_alta=oct_,
               fecha_cancelacion=datetime(2026, 11, 10, tzinfo=UTC))
    await _sub(tid, c[5], concepto="Plan Pro", valor="100000", fecha_alta=datetime(2026, 11, 5, tzinfo=UTC))
    await _sub(tid, c[6], concepto="Plan USD", valor="50", moneda="USD", fecha_alta=oct_)
    for dias in (3, 20, 45):
        await _cargo(tid, s_mensual, vence=ahora - timedelta(days=dias), periodo=f"2026-{dias:02d}")

    async with tenant_scope(tid):
        async with get_session() as session:
            m = await metricas(session, tid, ahora)

    assert m.moneda == "COP" and m.otras_monedas == 1
    assert m.mrr == 400000  # 100k + 1.2M/12 + 300k/3 + 100k
    assert m.mrr_mes_anterior == 350000  # vivas al 1.º de noviembre, sin la prueba (incluye la cancelada el 10)
    assert (m.activas, m.en_prueba, m.canceladas) == (5, 1, 1)
    assert (m.altas_mes, m.altas_mes_anterior, m.bajas_mes, m.bajas_mes_anterior) == (1, 6, 1, 0)
    assert m.churn_mes == pytest.approx(1 / 6, abs=1e-4)
    assert m.mora.cargos == 3 and m.mora.monto == 600000
    assert [t.cargos for t in m.mora.tramos] == [1, 1, 1]
    nov = [f for f in m.cohortes if f.cohorte == "2026-11"]
    assert [(f.plan, f.altas, f.vigentes) for f in nov] == [("Plan Pro", 1, 1)]
    viejo = next(f for f in m.cohortes if f.plan == "Plan Viejo")
    assert (viejo.altas, viejo.vigentes) == (1, 0)


async def test_metricas_por_http(app_client) -> None:
    tid = await _negocio()
    await _escenario(tid)
    r = await app_client.get(f"{BASE}/metricas", headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    assert r.json()["moneda"] == "COP" and r.json()["activas"] == 1 and r.json()["canceladas"] == 1


# ----------------------------------------------------------------------------- acciones
async def test_pausar_reanudar_cancelar_dejan_auditoria_del_usuario(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    h = await api_headers(app_client, tid)
    url = f"{BASE}/suscripciones/{e['s_ana']}"

    r = await app_client.post(f"{url}/pausar", json={"motivo": "pidió esperar"}, headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["estado"], r.json()["motivo_pausa"], r.json()["proximo_cargo"]) == ("pausada", "cliente", None)
    assert (await app_client.post(f"{url}/pausar", headers=h)).status_code == 409  # ya pausada

    r = await app_client.post(f"{url}/reanudar", headers=h)
    assert r.status_code == 200 and r.json()["estado"] == "activa" and r.json()["proximo_cargo"]

    r = await app_client.post(f"{url}/cancelar", json={"motivo": "se fue"}, headers=h)
    assert r.status_code == 200 and r.json()["estado"] == "cancelada"
    for accion in ("pausar", "reanudar", "cancelar"):
        malo = await app_client.post(f"{url}/{accion}", headers=h)
        assert malo.status_code == 409 and malo.json()["error"]["code"] == "transicion_invalida"

    for accion in ("subs_pausada", "subs_reanudada", "subs_cancelada"):
        [fila] = await _auditoria(tid, accion)
        assert fila.actor == "admin:admin@local"
        assert fila.input["subscription_id"] == str(e["s_ana"])
    [pausa] = await _auditoria(tid, "subs_pausada")
    assert pausa.input["motivo"] == "pidió esperar" and pausa.output["estado_antes"] == "activa"

    detalle = (await app_client.get(url, headers=h)).json()
    assert [x["accion"] for x in detalle["historial"]] == ["subs_cancelada", "subs_reanudada", "subs_pausada"]
    assert detalle["historial"][2]["actor"] == "admin:admin@local"
    assert "Motivo: pidió esperar" in detalle["historial"][2]["detalle"]


async def test_acciones_del_llm_no_duplican_la_auditoria() -> None:
    """Sin `actor` (las tools del LLM) la capability no escribe audit_log: ya lo hace el host de tools."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    sid = await _sub(tid, await _cliente(tid, "Dora"))
    async with tenant_scope(tid):
        await SubscriptionCapabilityImpl().pause(sid)
    assert await _auditoria(tid, "subs_pausada") == []


async def test_aislamiento_entre_negocios(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    e = await _escenario(a)
    hb = await api_headers(app_client, b)

    for ruta in ("suscripciones", "cargos", "clientes"):
        assert (await app_client.get(f"{BASE}/{ruta}", headers=hb)).json()["total"] == 0
    assert (await app_client.get(f"{BASE}/suscripciones/{e['s_ana']}", headers=hb)).status_code == 404
    for accion in ("pausar", "reanudar", "cancelar"):
        assert (await app_client.post(f"{BASE}/suscripciones/{e['s_ana']}/{accion}", headers=hb)).status_code == 404
    assert (await app_client.post(f"{BASE}/cargos/{e['c_viejo']}/reenviar-link", headers=hb)).status_code == 404
    # y nada cambió en el negocio A
    ha = await api_headers(app_client, a)
    assert (await app_client.get(f"{BASE}/suscripciones/{e['s_ana']}", headers=ha)).json()["estado"] == "activa"
    assert await _auditoria(a, "subs_pausada") == []


async def test_colaborador_de_solo_lectura_mira_pero_no_actua(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    h = await api_headers(app_client, tid, email=await _colaborador(tid), password=PASSWORD)
    assert (await app_client.get(f"{BASE}/suscripciones", headers=h)).status_code == 200
    assert (await app_client.get(f"{BASE}/metricas", headers=h)).status_code == 200
    for ruta in (f"suscripciones/{e['s_ana']}/pausar", f"suscripciones/{e['s_ana']}/cancelar",
                 f"cargos/{e['c_viejo']}/reenviar-link"):
        assert (await app_client.post(f"{BASE}/{ruta}", headers=h)).status_code == 403


# ----------------------------------------------------------------------------- reenviar link
async def test_reenviar_link_sin_whatsapp_devuelve_el_link_y_no_envia(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)  # Ana no tiene conversación de WhatsApp
    h = await api_headers(app_client, tid)
    r = await app_client.post(f"{BASE}/cargos/{e['c_viejo']}/reenviar-link", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"enviado": False, "link": "https://pago.test/abc", "motivo": "sin_conversacion_whatsapp"}
    assert await _eventos(tid, "subscription.charge_reminder") == []
    [fila] = await _auditoria(tid, "subs_link_reenviado")
    assert fila.output == {"enviado": False} and fila.actor == "admin:admin@local"


async def test_reenviar_link_con_whatsapp_encola_el_aviso_una_vez(app_client, monkeypatch) -> None:
    import pack_subscriptions.horario_legal as horario

    monkeypatch.setattr(horario, "en_horario_de_cobro", lambda *a, **k: True)  # el CI corre a cualquier hora
    tid = await _negocio()
    ana = await _cliente(tid, "Ana", con_whatsapp=True)
    sid = await _sub(tid, ana)
    cargo = await _cargo(tid, sid, vence=datetime.now(UTC) - timedelta(days=2))
    h = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/cargos/{cargo}/reenviar-link", headers=h)
    assert r.status_code == 200 and r.json()["enviado"] is True and r.json()["motivo"] is None
    [evento] = await _eventos(tid, "subscription.charge_reminder")
    assert evento.payload["charge_id"] == str(cargo) and evento.payload["link"] == "https://pago.test/abc"
    assert evento.payload["reenvio"] and evento.payload["dias"] == 2
    assert evento.dedupe_key == f"sub_resend:{cargo}:{evento.payload['reenvio']}"

    # Aparece en los avisos del cargo (trazabilidad de HU-SUB-05).
    avisos = (await app_client.get(f"{BASE}/cargos/{cargo}/avisos", headers=h)).json()
    assert [a["evento"] for a in avisos] == ["subscription.charge_reminder"]


async def test_reenviar_link_fuera_de_horario_o_con_aviso_del_dia_responde_409(app_client, monkeypatch) -> None:
    """HU-SUB-18: el reenvío del panel es un contacto de cobro (Ley 2300)."""
    import pack_subscriptions.horario_legal as horario

    tid = await _negocio()
    ana = await _cliente(tid, "Ana", con_whatsapp=True)
    cargo = await _cargo(tid, await _sub(tid, ana), vence=datetime.now(UTC) - timedelta(days=2))
    h = await api_headers(app_client, tid)

    monkeypatch.setattr(horario, "en_horario_de_cobro", lambda *a, **k: False)
    r = await app_client.post(f"{BASE}/cargos/{cargo}/reenviar-link", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "fuera_de_horario_de_cobro"
    assert await _eventos(tid, "subscription.charge_reminder") == []

    monkeypatch.setattr(horario, "en_horario_de_cobro", lambda *a, **k: True)
    assert (await app_client.post(f"{BASE}/cargos/{cargo}/reenviar-link", headers=h)).status_code == 200
    r = await app_client.post(f"{BASE}/cargos/{cargo}/reenviar-link", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "aviso_de_cobro_del_dia_enviado"
    assert len(await _eventos(tid, "subscription.charge_reminder")) == 1


async def test_reenviar_link_rechaza_lo_que_no_se_puede_cobrar(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    h = await api_headers(app_client, tid)
    pagado = await _cargo(tid, e["s_ana"], vence=datetime.now(UTC) - timedelta(days=30), estado="pagado", periodo="2020-02")
    r = await app_client.post(f"{BASE}/cargos/{pagado}/reenviar-link", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "cargo_no_pendiente"
    de_cancelada = await _cargo(tid, e["s_carla"], vence=datetime.now(UTC) - timedelta(days=1), periodo="2020-03")
    r = await app_client.post(f"{BASE}/cargos/{de_cancelada}/reenviar-link", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "suscripcion_cancelada"


async def test_el_reenvio_manual_tiene_su_propia_clave_de_aviso() -> None:
    """El aviso del panel no se funde con el recordatorio del cron del mismo día (ADR-0108)."""
    from core.events.core_handlers import notice_key_for
    from core.shared.events import ON_SUBSCRIPTION_CHARGE_REMINDER

    cron = {"charge_id": "c1", "dias": 2}
    manual = {"charge_id": "c1", "dias": 2, "reenvio": "202611181400"}
    assert notice_key_for(ON_SUBSCRIPTION_CHARGE_REMINDER, cron) == "sub_reminder:c1:2"
    assert notice_key_for(ON_SUBSCRIPTION_CHARGE_REMINDER, manual) == "sub_resend:c1:202611181400"


# ----------------------------------------------------------------------------- Clientes (LeadProvider)
async def test_la_vista_de_clientes_muestra_y_filtra_por_suscripcion(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    sin = await _cliente(tid, "Sin Plan")
    h = await api_headers(app_client, tid)

    data = (await app_client.get("/api/v1/admin/leads?page_size=50", headers=h)).json()
    assert {f["key"] for f in data["filters"]} >= {"suscripcion"}
    por_nombre = {i["cliente"]: i for i in data["items"]}
    ana = por_nombre["Ana Gómez"]["extras"]
    assert ana["sub_plan"] == "Plan Pro" and ana["sub_estado"] == "Con mora" and ana["sub_deuda"] == "$400.000"
    assert por_nombre["Beto Ruiz"]["extras"]["sub_estado"] == "Pausada"
    assert por_nombre["Carla Díaz"]["extras"]["sub_estado"] == "Cancelada"
    assert "suscripciones" in por_nombre["Ana Gómez"]["agentes"]

    async def ids(filtro: str) -> set[str]:
        r = await app_client.get(f"/api/v1/admin/leads?page_size=50&filtro=suscripcion:{filtro}", headers=h)
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()["items"]}

    assert await ids("mora") == {str(e["ana"])}
    assert await ids("pausada") == {str(e["beto"])}
    assert await ids("cancelada") == {str(e["carla"])}
    assert await ids("ninguna") == {str(sin)}


async def test_la_linea_de_tiempo_del_cliente_trae_alta_cobros_y_baja(app_client) -> None:
    tid = await _negocio()
    e = await _escenario(tid)
    h = await api_headers(app_client, tid)
    r = await app_client.get(f"/api/v1/admin/leads/{e['ana']}/timeline", headers=h)
    assert r.status_code == 200, r.text
    titulos = [ev["title"] for ev in r.json()["items"]]
    assert any(t.startswith("Suscripción a «Plan Pro»") for t in titulos)
    assert sum(t.startswith("Cobro ") for t in titulos) == 2
    carla = (await app_client.get(f"/api/v1/admin/leads/{e['carla']}/timeline", headers=h)).json()["items"]
    assert any(ev["title"].startswith("Suscripción cancelada") for ev in carla)


async def test_ids_de_otro_negocio_en_los_filtros_no_traen_nada(app_client) -> None:
    """`plan_id`, `cliente_id` y `suscripcion_id` ajenos no abren filas ni cuelan métricas."""
    a, b = await _negocio(), await _negocio()
    e = await _escenario(a)
    plan = await plan_por_defecto(a, codigo="PLAN-X")
    await _sub(a, await _cliente(a, "Con plan"), concepto="Plan X", plan_id=plan)
    # Negocio B con su propia data: la de A no se mezcla en lo suyo.
    await _sub(b, await _cliente(b, "Zoe"), concepto="Plan Z", valor="1000")
    hb = await api_headers(app_client, b)

    assert (await app_client.get(f"{BASE}/suscripciones?plan_id={plan}", headers=hb)).json()["total"] == 0
    assert (await app_client.get(f"{BASE}/suscripciones?cliente_id={e['ana']}", headers=hb)).json()["total"] == 0
    assert (await app_client.get(f"{BASE}/cargos?suscripcion_id={e['s_ana']}", headers=hb)).json()["total"] == 0
    assert (await app_client.get(f"{BASE}/suscripciones?q=Ana", headers=hb)).json()["total"] == 0
    assert (await app_client.get(f"{BASE}/cargos/{e['c_viejo']}/avisos", headers=hb)).status_code == 404
    m = (await app_client.get(f"{BASE}/metricas", headers=hb)).json()
    assert m["mrr"] == 1000 and m["mora"]["cargos"] == 0 and (m["activas"], m["canceladas"]) == (1, 0)


async def test_la_auditoria_y_la_vista_de_clientes_no_cruzan_negocios(app_client) -> None:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    e = await _escenario(a)
    await _escenario(b)
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    await app_client.post(f"{BASE}/suscripciones/{e['s_ana']}/pausar", headers=ha)
    await app_client.post(f"{BASE}/cargos/{e['c_viejo']}/reenviar-link", headers=ha)

    async with tenant_scope(a):
        async with get_session() as session:
            filas = (await session.execute(select(AuditLog).where(AuditLog.action.like("subs\\_%")))).scalars().all()
    assert {f.tenant_id for f in filas} == {a} and len(filas) == 2
    assert await _auditoria(b, "subs_pausada") == [] and await _auditoria(b, "subs_link_reenviado") == []

    # Los filtros y la línea de tiempo de Clientes solo ven al negocio propio.
    ids_b = {i["id"] for i in (await app_client.get("/api/v1/admin/leads?page_size=50&filtro=suscripcion:mora", headers=hb)).json()["items"]}
    assert str(e["ana"]) not in ids_b and len(ids_b) == 1
    assert (await app_client.get(f"/api/v1/admin/leads/{e['ana']}/timeline", headers=hb)).status_code == 404
