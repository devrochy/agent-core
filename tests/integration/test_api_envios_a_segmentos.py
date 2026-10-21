"""HU-CRM-06 · envío de una plantilla a un segmento de Clientes y consentimiento (ADR-0150).

Con la BD real: la vista previa cuenta y excluye por motivo y guarda la instantánea; confirmar es
idempotente y emite un solo lote al outbox; un envío en curso bloquea otro por la misma conexión;
los lotes no duplican mensajes aunque se reprocesen, respetan una baja posterior a la vista previa
y dejan la plantilla en la línea de tiempo; nada cruza negocios.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/leads"
ACC = f"{URL}/acciones/plantilla"
PASSWORD = "Colab-Envios-2026!"


@pytest.fixture(autouse=True)
def _sin_guarda_de_test(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.admin import envios_segmento

    monkeypatch.setattr(envios_segmento, "lista_permitida", lambda: None)


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("envios")
    tid, _ = await onboard_tenant(
        name=f"Envios {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    return tid


async def _escenario(tid: uuid.UUID) -> dict[str, uuid.UUID]:
    """Conexión de WhatsApp Cloud con una plantilla de marketing y otra de utilidad, y clientes:
    ana y eva (con consentimiento), beto (sin), caro (se dio de baja), dani (con consentimiento y
    una escalación abierta) y web (otro canal)."""
    from core.db.models import (
        ChannelConnection,
        Conversation,
        Customer,
        CustomerConsent,
        Escalation,
        MessageTemplate,
        MetaConnection,
        WabaAccount,
    )
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    ids: dict[str, uuid.UUID] = {}
    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok",
                meta_app_id=uniq("app"), status="connected",
            )
            session.add(cuenta)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=cuenta.id, waba_id=uniq("waba"), subscribed=True)
            session.add(waba)
            await session.flush()
            cuerpo = [{"type": "BODY", "text": "Hola {{1}}, esta semana {{2}}."}]
            promo = MessageTemplate(
                tenant_id=tid, waba_account_id=waba.id, name="promo_semana", language="es",
                status="APPROVED", category="MARKETING", components=cuerpo,
            )
            aviso = MessageTemplate(
                tenant_id=tid, waba_account_id=waba.id, name="aviso_pedido", language="es",
                status="APPROVED", category="UTILITY", components=cuerpo,
            )
            session.add_all([promo, aviso])
            conexion = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="meta", external_id=uniq("pnid"),
                label="Ventas", meta_connection_id=cuenta.id,
            )
            session.add(conexion)
            await session.flush()
            ids.update(conexion=conexion.id, promo=promo.id, aviso=aviso.id)
            for nombre, canal in (("ana", "whatsapp"), ("eva", "whatsapp"), ("beto", "whatsapp"),
                                  ("caro", "whatsapp"), ("dani", "whatsapp"), ("web", "webchat")):
                c = Customer(tenant_id=tid, channel=canal, external_id=f"{canal}:{uniq(nombre)}", name=nombre.title())
                session.add(c)
                await session.flush()
                conv = Conversation(tenant_id=tid, customer_id=c.id, channel=canal, connection_id=conexion.id)
                session.add(conv)
                await session.flush()
                ids[nombre], ids[f"conv_{nombre}"] = c.id, conv.id
            for nombre, estado in (("ana", "otorgado"), ("eva", "otorgado"), ("dani", "otorgado"),
                                   ("caro", "otorgado"), ("caro", "revocado")):
                session.add(CustomerConsent(tenant_id=tid, customer_id=ids[nombre], estado=estado, origen="ficha"))
                await session.flush()
            session.add(Escalation(tenant_id=tid, conversation_id=ids["conv_dani"], reason="cliente_pide_humano"))
    return ids


def _cuerpo(ids: dict[str, uuid.UUID]) -> dict:
    return {
        "connection_id": str(ids["conexion"]),
        "template_id": str(ids["promo"]),
        "variables": {"1": {"fuente": "cliente.nombre", "valor": "amigo"}, "2": {"fuente": "fijo", "valor": "20% en todo"}},
    }


async def _colaborador(tid: uuid.UUID) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        session.add(AdminPermission(admin_id=admin.id, section="leads", access="write", tenant_id=tid))
    return email


async def _enviar_falso(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Reemplaza el sender: crea el `Message` como lo haría (con el id estable) sin Meta ni Celery."""
    from core.db.models import Message
    from core.db.session import get_session
    from core.gateway import sender
    from core.shared.tenant import get_current_tenant_id

    llamadas: list[str] = []

    async def falso(conversation_id, *, template_name, language, components, preview_text, sender_type="agent", client_message_id=None):
        llamadas.append(str(client_message_id))
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=get_current_tenant_id(), conversation_id=conversation_id, direction="outbound",
                    sender_type=sender_type, content=preview_text, status="queued",
                    client_message_id=client_message_id, template_name=template_name,
                    template_language=language, template_components=components,
                )
            )
        return True

    monkeypatch.setattr(sender, "send_template_to_conversation", falso)
    return llamadas


async def _lotes(tid: uuid.UUID, send_id: str) -> int:
    """Entrega los lotes del outbox de ese envío como lo haría el despachador. Devuelve cuántos."""
    from sqlalchemy import select

    from core.admin.envios_segmento import procesar_lote
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    entregados = 0
    async with tenant_scope(tid):
        while True:
            async with get_session() as session:
                evento = (
                    await session.execute(
                        select(DomainEvent).where(
                            DomainEvent.tenant_id == tid,
                            DomainEvent.status == "pendiente",
                            DomainEvent.dedupe_key.like(f"segmento:{send_id}:lote:%"),
                        )
                    )
                ).scalars().first()
                if evento is None:
                    return entregados
                await procesar_lote(**{k: v for k, v in evento.payload.items() if k in ("send_id", "lote")})
                evento.status = "entregado"
                entregados += 1


async def test_vista_previa_cuenta_excluye_y_guarda_la_instantanea(app_client) -> None:
    tid = await _negocio()
    ids = await _escenario(tid)
    master = await api_headers(app_client, tid)

    conexiones = (await app_client.get(f"{ACC}/conexiones", headers=master)).json()
    assert [(c["label"], [p["name"] for p in c["plantillas"]]) for c in conexiones] == [("Ventas", ["promo_semana"])]

    r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)
    assert r.status_code == 200, r.text
    vista = r.json()
    assert vista["candidatos"] == 6
    assert vista["elegibles"] == 2 and sorted(vista["muestra"]) == ["Ana", "Eva"]
    assert {e["motivo"]: e["total"] for e in vista["excluidos"]} == {
        "sin_consentimiento": 1, "baja": 1, "escalacion_abierta": 1, "no_whatsapp": 1,
    }
    assert vista["bloqueo"] is None and vista["envio_id"]
    assert vista["costo_creditos"] == "0.6"
    assert vista["texto"] == "Hola amigo, esta semana 20% en todo."

    # Una plantilla de utilidad no se manda a un segmento.
    r = await app_client.post(f"{ACC}/vista-previa", json={**_cuerpo(ids), "template_id": str(ids["aviso"])}, headers=master)
    assert r.status_code == 404
    # Variables incompletas.
    r = await app_client.post(f"{ACC}/vista-previa", json={**_cuerpo(ids), "variables": {}}, headers=master)
    assert r.status_code == 400 and r.json()["error"]["code"] == "template_variables"


async def test_solo_el_dueno_envia(app_client) -> None:
    tid = await _negocio()
    ids = await _escenario(tid)
    colab = await api_headers(app_client, tid, email=await _colaborador(tid), password=PASSWORD)
    assert (await app_client.get(f"{ACC}/conexiones", headers=colab)).status_code == 403
    assert (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=colab)).status_code == 403


async def test_tope_y_un_envio_en_curso_por_conexion(app_client, monkeypatch) -> None:
    from core.admin import envios_segmento

    tid = await _negocio()
    ids = await _escenario(tid)
    master = await api_headers(app_client, tid)

    monkeypatch.setattr(envios_segmento, "TOPE_POR_ENVIO", 1)
    vista = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()
    assert vista["bloqueo"] == "tope" and vista["envio_id"] is None
    monkeypatch.setattr(envios_segmento, "TOPE_POR_ENVIO", 200)

    primera = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()
    segunda = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()
    r = await app_client.post(f"{ACC}/{primera['envio_id']}/confirmar", headers={**master, "Idempotency-Key": uniq("clave-a")})
    assert r.status_code == 202, r.text
    # La segunda vista previa se armó antes: al confirmarla choca con el envío en curso.
    r = await app_client.post(f"{ACC}/{segunda['envio_id']}/confirmar", headers={**master, "Idempotency-Key": uniq("clave-b")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "envio_en_curso"
    vista = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()
    assert vista["bloqueo"] == "envio_en_curso"


async def test_confirmar_es_idempotente_y_vence(app_client) -> None:
    from sqlalchemy import select, update

    from core.db.models import DomainEvent, SegmentSend, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ids = await _escenario(tid)
    master = await api_headers(app_client, tid)
    envio = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()["envio_id"]
    clave = uniq("clave-envio")

    r1 = await app_client.post(f"{ACC}/{envio}/confirmar", headers={**master, "Idempotency-Key": clave})
    r2 = await app_client.post(f"{ACC}/{envio}/confirmar", headers={**master, "Idempotency-Key": clave})
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["estado"] == "confirmado" and r1.json()["total"] == 2
    # Otra clave sobre el mismo envío: ya está confirmado.
    r = await app_client.post(f"{ACC}/{envio}/confirmar", headers={**master, "Idempotency-Key": uniq("otra")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ya_confirmado"
    async with tenant_scope(tid):
        async with get_session() as session:
            eventos = (
                await session.execute(select(DomainEvent.dedupe_key).where(DomainEvent.tenant_id == tid))
            ).scalars().all()
    assert [e for e in eventos if e.startswith("segmento:")] == [f"segmento:{envio}:lote:1"]

    # La misma clave con otro envío → conflicto. Una vista previa vencida → 410.
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(update(SegmentSend).where(SegmentSend.id == envio).values(estado="terminado"))
    vencida = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()["envio_id"]
    r = await app_client.post(f"{ACC}/{vencida}/confirmar", headers={**master, "Idempotency-Key": clave})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(SegmentSend).where(SegmentSend.id == vencida).values(expires_at=utcnow() - timedelta(minutes=1))
            )
    r = await app_client.post(f"{ACC}/{vencida}/confirmar", headers={**master, "Idempotency-Key": uniq("nueva")})
    assert r.status_code == 410


async def test_lotes_sin_duplicados_con_baja_posterior_y_en_la_linea_de_tiempo(app_client, monkeypatch) -> None:
    from core.admin import envios_segmento
    from core.admin.envios_segmento import registrar_consentimiento
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ids = await _escenario(tid)
    master = await api_headers(app_client, tid)
    llamadas = await _enviar_falso(monkeypatch)
    monkeypatch.setattr(envios_segmento, "LOTE", 1)  # un destinatario por lote: dos lotes

    envio = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()["envio_id"]
    # Eva se da de baja entre la vista previa y el envío.
    async with tenant_scope(tid):
        await registrar_consentimiento(tid, ids["eva"], estado="revocado", origen="palabra_de_baja", evidencia="baja")
    r = await app_client.post(f"{ACC}/{envio}/confirmar", headers={**master, "Idempotency-Key": uniq("k")})
    assert r.status_code == 202

    assert await _lotes(tid, envio) == 2
    assert llamadas == [f"segmento:{envio}:{ids['ana']}"]
    detalle = (await app_client.get(f"{ACC}/{envio}", headers=master)).json()
    assert detalle["estado"] == "terminado"
    assert detalle["conteo"] == {"queued": 1, "omitido": 1}
    assert detalle["fallas"] == [{"cliente": "Eva", "motivo": "Se dio de baja"}]

    # Reprocesar un lote ya entregado no vuelve a enviar.
    async with tenant_scope(tid):
        await envios_segmento.procesar_lote(send_id=envio, lote=1)
    assert len(llamadas) == 1

    linea = (await app_client.get(f"{URL}/{ids['ana']}/timeline", headers=master)).json()["items"]
    plantilla = next(e for e in linea if e["kind"] == "plantilla")
    assert plantilla["title"] == "Plantilla enviada: promo_semana"
    assert "Envío a un segmento" in plantilla["detail"] and plantilla["link_id"] == str(ids["conv_ana"])

    # Ana ya recibió una promoción: queda fuera por 7 días.
    vista = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()
    assert {e["motivo"]: e["total"] for e in vista["excluidos"]}.get("promocion_reciente") == 1
    assert vista["bloqueo"] == "sin_destinatarios"


async def test_consentimiento_desde_la_ficha_y_baja_por_palabra(app_client) -> None:
    from core.admin.envios_segmento import registrar_baja_si_corresponde
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ids = await _escenario(tid)
    master = await api_headers(app_client, tid)
    beto = ids["beto"]

    assert (await app_client.get(f"{URL}/{beto}/consentimiento", headers=master)).json()["estado"] == "sin_registro"
    r = await app_client.post(f"{URL}/{beto}/consentimiento", json={"estado": "otorgado", "como": "otro"}, headers=master)
    assert r.status_code == 400  # «otro» pide contar cómo
    r = await app_client.post(
        f"{URL}/{beto}/consentimiento",
        json={"estado": "otorgado", "como": "en_el_local", "evidencia": "firmó la planilla"},
        headers=master,
    )
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "otorgado"
    assert cuerpo["historial"][0]["evidencia"] == "En el local: firmó la planilla"
    assert cuerpo["historial"][0]["quien"] == "Soporte de la plataforma"

    async with tenant_scope(tid):
        assert not await registrar_baja_si_corresponde(beto, "whatsapp", "baja el precio")
        assert await registrar_baja_si_corresponde(beto, "whatsapp", "No más promociones")
        assert not await registrar_baja_si_corresponde(beto, "whatsapp", "baja")  # ya estaba de baja
    cuerpo = (await app_client.get(f"{URL}/{beto}/consentimiento", headers=master)).json()
    assert cuerpo["estado"] == "revocado"
    assert cuerpo["historial"][0]["origen"] == "palabra_de_baja" and cuerpo["historial"][0]["quien"] == "el cliente"


async def test_aislamiento_entre_negocios(app_client) -> None:
    tid = await _negocio()
    otro = await _negocio()
    ids = await _escenario(tid)
    ajenos = await _escenario(otro)
    master = await api_headers(app_client, tid)
    master_otro = await api_headers(app_client, otro)

    envio = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master)).json()["envio_id"]
    # El otro negocio no ve, no confirma y no usa la conexión ni la plantilla ajenas.
    assert (await app_client.get(f"{ACC}/{envio}", headers=master_otro)).status_code == 404
    r = await app_client.post(f"{ACC}/{envio}/confirmar", headers={**master_otro, "Idempotency-Key": uniq("k")})
    assert r.status_code == 404
    assert (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=master_otro)).status_code == 404
    assert (await app_client.get(f"{URL}/{ids['ana']}/consentimiento", headers=master_otro)).status_code == 404
    vista = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ajenos), headers=master_otro)).json()
    assert vista["candidatos"] == 6  # solo sus clientes
