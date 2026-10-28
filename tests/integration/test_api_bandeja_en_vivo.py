"""HU-UX-15 · Bandeja de conversaciones en vivo.

Con la BD real: la lista trae el último mensaje y la conexión y se ordena por actividad;
`?connection_id=` filtra; `GET …/messages?after=` da lo nuevo (con solape) y nada de otro
negocio; el detalle trae la escalación activa, las notas (del agente y de personas), el
costo y las herramientas del turno; tomar pausa el agente y devolver lo reactiva.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

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

URL = "/api/v1/admin/conversations"


async def _negocio() -> dict:
    """Negocio con dos conexiones y tres chats:

    - `vieja`: por «Ventas», último mensaje hace 1 h.
    - `nueva`: por «Soporte», escalada, con mensajes recientes, un turno con herramientas,
      una nota del agente y costo.
    - `sin_mensajes`: sin conexión ni mensajes, iniciada hace 2 h.
    """
    from sqlalchemy import text

    from core.alta_negocio import onboard_tenant
    from core.db.models import (
        ChannelConnection,
        Conversation,
        Customer,
        Escalation,
        Message,
        UsageLedger,
    )
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("bandeja")
    tid, _ = await onboard_tenant(
        name=f"Bandeja {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local"
    )
    ahora = datetime.now(UTC)
    turno = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as session:
            ventas = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="meta", type_id="whatsapp_cloud",
                label="Ventas", external_id=uniq("pn"),
            )
            soporte = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="waweb", type_id="whatsapp_waweb",
                label="Soporte", external_id=uniq("pn"),
            )
            session.add_all([ventas, soporte])
            await session.flush()

            def cliente(nombre: str) -> Customer:
                return Customer(
                    tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre
                )

            ana, beto, caro = cliente("Ana"), cliente("Beto"), cliente("Caro")
            session.add_all([ana, beto, caro])
            await session.flush()
            vieja = Conversation(
                tenant_id=tid, customer_id=ana.id, connection_id=ventas.id,
                started_at=ahora - timedelta(hours=3),
            )
            nueva = Conversation(
                tenant_id=tid, customer_id=beto.id, connection_id=soporte.id, status="escalated",
                bot_paused=True, started_at=ahora - timedelta(hours=4),
                state={"notes": ["Cliente pide factura a otro NIT"]},
            )
            sin_mensajes = Conversation(
                tenant_id=tid, customer_id=caro.id, started_at=ahora - timedelta(hours=2)
            )
            session.add_all([vieja, nueva, sin_mensajes])
            await session.flush()
            # Cabecera del turno (FK de `messages.turn_id`).
            await session.execute(
                text(
                    "insert into core_decisions (id, tenant_id, conversation_id, outcome, latency_ms) "
                    "values (:id, :t, :c, 'routed', 1)"
                ),
                {"id": turno, "t": tid, "c": nueva.id},
            )
            session.add_all(
                [
                    Message(
                        tenant_id=tid, conversation_id=vieja.id, direction="inbound",
                        sender_type="customer", content="Hola", created_at=ahora - timedelta(hours=1),
                    ),
                    Message(
                        tenant_id=tid, conversation_id=nueva.id, direction="inbound",
                        sender_type="customer", content="¿Tienen envío?",
                        created_at=ahora - timedelta(minutes=10),
                    ),
                    Message(
                        tenant_id=tid, conversation_id=nueva.id, direction="outbound",
                        sender_type="agent", content="Sí, a todo el país.", turn_id=turno,
                        created_at=ahora - timedelta(minutes=9),
                    ),
                    Message(
                        tenant_id=tid, conversation_id=nueva.id, direction="outbound",
                        sender_type="agent", content="¿Te ayudo con algo más?", turn_id=turno,
                        created_at=ahora - timedelta(minutes=9) + timedelta(seconds=1),
                    ),
                    Message(
                        tenant_id=tid, conversation_id=nueva.id, direction="inbound",
                        sender_type="customer", content="Quiero hablar con alguien",
                        created_at=ahora - timedelta(minutes=1),
                    ),
                    Escalation(
                        tenant_id=tid, conversation_id=nueva.id, reason="tool:queja",
                        summary="Llegó tarde",
                    ),
                    UsageLedger(
                        tenant_id=tid, conversation_id=nueva.id, model="m", input_tokens=1200,
                        output_tokens=300, cost_usd=0.0125, turn_id=turno,
                        tool_calls=[{"tool": "buscar_producto"}, {"tool": "consultar_envio"}],
                    ),
                ]
            )
    return {
        "tid": tid,
        "ventas": ventas.id,
        "soporte": soporte.id,
        "vieja": vieja.id,
        "nueva": nueva.id,
        "sin_mensajes": sin_mensajes.id,
    }


async def test_lista_con_ultimo_mensaje_conexion_y_orden_por_actividad(app_client) -> None:
    n = await _negocio()
    headers = await api_headers(app_client, n["tid"])

    r = await app_client.get(URL, headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [str(n["nueva"]), str(n["vieja"]), str(n["sin_mensajes"])]
    primero = items[0]
    assert primero["last_message"] == "Quiero hablar con alguien"
    assert primero["last_message_sender"] == "customer"
    assert primero["pending_count"] == 1
    assert (primero["connection_label"], primero["connection_provider"], primero["connection_type"]) == (
        "Soporte", "waweb", "whatsapp_waweb",
    )
    assert items[1]["pending_count"] == 1
    assert items[2]["last_message"] is None and items[2]["connection_id"] is None
    assert items[2]["pending_count"] == 0

    r = await app_client.get(URL, params={"connection_id": str(n["ventas"])}, headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [str(n["vieja"])]
    assert r.json()["total"] == 1

    # Opciones del filtro: las conexiones del negocio, con el permiso de Conversaciones.
    r = await app_client.get(f"{URL}/connections", headers=headers)
    assert r.status_code == 200, r.text
    opciones = {c["id"]: (c["label"], c["provider"], c["type_id"]) for c in r.json()}
    assert opciones[str(n["ventas"])] == ("Ventas", "meta", "whatsapp_cloud")
    assert opciones[str(n["soporte"])] == ("Soporte", "waweb", "whatsapp_waweb")


async def test_pendientes_se_limpian_solo_con_una_respuesta_entregada(app_client) -> None:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    headers = await api_headers(app_client, n["tid"])
    ahora = datetime.now(UTC)

    async def agregar(direction: str, status: str, content: str, segundos: int) -> None:
        async with tenant_scope(n["tid"]):
            async with get_session() as session:
                session.add(
                    Message(
                        tenant_id=n["tid"],
                        conversation_id=n["nueva"],
                        direction=direction,
                        sender_type="agent" if direction == "outbound" else "customer",
                        content=content,
                        status=status,
                        created_at=ahora + timedelta(seconds=segundos),
                    )
                )

    # Un envío fallido o aún encolado no es una respuesta: el cliente sigue esperando.
    await agregar("outbound", "failed", "No salió", 1)
    await agregar("outbound", "queued", "En cola", 2)
    r = await app_client.get(URL, headers=headers)
    nueva = next(i for i in r.json()["items"] if i["id"] == str(n["nueva"]))
    assert nueva["pending_count"] == 1

    # Una respuesta entregada sí limpia la cuenta.
    await agregar("outbound", "sent", "Ya te ayudo", 3)
    r = await app_client.get(URL, headers=headers)
    nueva = next(i for i in r.json()["items"] if i["id"] == str(n["nueva"]))
    assert nueva["pending_count"] == 0

    # Dos mensajes nuevos del cliente vuelven a contar.
    await agregar("inbound", "sent", "¿Hola?", 4)
    await agregar("inbound", "sent", "¿Sigues ahí?", 5)
    r = await app_client.get(URL, headers=headers)
    items = r.json()["items"]
    assert items[0]["id"] == str(n["nueva"])
    assert items[0]["pending_count"] == 2


async def test_mensajes_despues_del_cursor(app_client) -> None:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    headers = await api_headers(app_client, n["tid"])
    url = f"{URL}/{n['nueva']}/messages"

    r = await app_client.get(url, headers=headers)
    assert r.status_code == 200, r.text
    todos = r.json()["items"]
    assert len(todos) == 4 and r.json()["has_more"] is False
    cursor = todos[-1]["created_at"]

    # Llega un mensaje nuevo: con el cursor vuelve ése (y el solape de 2 s, que el panel
    # deduplica por id), no el hilo completo.
    async with tenant_scope(n["tid"]):
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=n["tid"], conversation_id=n["nueva"], direction="inbound",
                    sender_type="customer", content="¿Hola?",
                )
            )
    r = await app_client.get(url, params={"after": cursor}, headers=headers)
    assert r.status_code == 200, r.text
    contenidos = [m["content"] for m in r.json()["items"]]
    assert contenidos == ["Quiero hablar con alguien", "¿Hola?"]

    r = await app_client.get(url, params={"after": "no-es-fecha"}, headers=headers)
    assert r.status_code == 422


async def test_detalle_con_escalacion_notas_costo_y_herramientas(app_client) -> None:
    n = await _negocio()
    headers = await api_headers(app_client, n["tid"])

    r = await app_client.get(f"{URL}/{n['nueva']}", headers=headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["escalation"]["reason_code"] == "queja"
    assert d["escalation"]["reason_label"] == "Queja"
    assert d["escalation"]["summary"] == "Llegó tarde"
    assert d["escalation"]["assigned_admin_id"] is None
    assert d["notes"] == [
        {"text": "Cliente pide factura a otro NIT", "source": "agent", "author_email": None, "created_at": None}
    ]
    assert d["usage"] == {"cost_usd": 0.0125, "input_tokens": 1200, "output_tokens": 300, "turns": 1}
    # Las herramientas del turno van en el PRIMER mensaje del agente de ese turno.
    agente = [m for m in d["messages"] if m["sender_type"] == "agent"]
    assert agente[0]["tool_calls"] == ["buscar_producto", "consultar_envio"]
    assert agente[1]["tool_calls"] == []
    assert d["connection_label"] == "Soporte"
    assert d["last_message"] == "Quiero hablar con alguien"
    assert d["pending_count"] == 1

    # El polling con cursor adjunta las herramientas al mismo mensaje que la carga completa.
    r = await app_client.get(
        f"{URL}/{n['nueva']}/messages", params={"after": agente[1]["created_at"]}, headers=headers
    )
    por_id = {m["id"]: m for m in r.json()["items"]}
    assert por_id[agente[0]["id"]]["tool_calls"] == ["buscar_producto", "consultar_envio"]
    assert por_id[agente[1]["id"]]["tool_calls"] == []


async def test_tomar_y_devolver_al_agente(app_client) -> None:
    from sqlalchemy import select

    from core.db.models import Admin, Escalation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    headers = await api_headers(app_client, n["tid"])
    base = f"{URL}/{n['nueva']}"

    # Sin escalación no hay nada que tomar.
    r = await app_client.post(f"{URL}/{n['vieja']}/take", headers=headers)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "no_escalation")

    r = await app_client.post(f"{base}/take", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "asignada"
    assert r.json()["assigned_admin_email"] == "admin@local"
    # Idempotente para quien ya la tiene.
    assert (await app_client.post(f"{base}/take", headers=headers)).status_code == 200
    d = (await app_client.get(base, headers=headers)).json()
    assert d["bot_paused"] is True and d["status"] == "escalated"
    assert d["escalation"]["assigned_admin_email"] == "admin@local"

    # Si la tiene otra persona del equipo: 409 con su correo.
    async with tenant_scope(n["tid"]):
        async with get_session() as session:
            otro = Admin(email=f"{uniq('otro')}@local", password_hash="x")
            session.add(otro)
            await session.flush()
            esc = (
                await session.execute(select(Escalation).where(Escalation.conversation_id == n["nueva"]))
            ).scalar_one()
            esc.assigned_admin_id = otro.id
            correo = otro.email
    r = await app_client.post(f"{base}/take", headers=headers)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "already_taken")
    assert correo in r.json()["error"]["message"]

    r = await app_client.post(f"{base}/return-to-bot", headers=headers)
    assert r.status_code == 204, r.text
    d = (await app_client.get(base, headers=headers)).json()
    assert (d["bot_paused"], d["status"], d["escalation"]) == (False, "open", None)


async def test_nota_interna_de_persona(app_client) -> None:
    from sqlalchemy import func, select

    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    headers = await api_headers(app_client, n["tid"])
    base = f"{URL}/{n['nueva']}"

    r = await app_client.post(f"{base}/notes", json={"text": "  Llamar mañana  "}, headers=headers)
    assert r.status_code == 201, r.text
    assert (r.json()["text"], r.json()["source"]) == ("Llamar mañana", "human")
    assert (await app_client.post(f"{base}/notes", json={"text": "   "}, headers=headers)).status_code == 400

    notas = (await app_client.get(base, headers=headers)).json()["notes"]
    assert [(x["text"], x["source"], x["author_email"]) for x in notas] == [
        ("Cliente pide factura a otro NIT", "agent", None),
        ("Llamar mañana", "human", "admin@local"),
    ]
    # Nunca llega al cliente: no se crea ningún mensaje.
    async with tenant_scope(n["tid"]):
        async with get_session() as session:
            mensajes = (
                await session.execute(
                    select(func.count()).select_from(Message).where(Message.conversation_id == n["nueva"])
                )
            ).scalar_one()
    assert mensajes == 4


async def test_aislamiento_entre_negocios(app_client) -> None:
    a = await _negocio()
    b = await _negocio()
    headers_b = await api_headers(app_client, b["tid"])

    ids = {i["id"] for i in (await app_client.get(URL, headers=headers_b)).json()["items"]}
    conexiones_b = {c["id"] for c in (await app_client.get(f"{URL}/connections", headers=headers_b)).json()}
    assert str(a["soporte"]) not in conexiones_b and str(b["soporte"]) in conexiones_b
    assert str(a["nueva"]) not in ids and str(b["nueva"]) in ids
    # Filtrar por una conexión de otro negocio no trae nada.
    r = await app_client.get(URL, params={"connection_id": str(a["soporte"])}, headers=headers_b)
    assert r.json()["items"] == []

    ajena = f"{URL}/{a['nueva']}"
    assert (await app_client.get(ajena, headers=headers_b)).status_code == 404
    assert (await app_client.get(f"{ajena}/messages", headers=headers_b)).status_code == 404
    for accion in ("take", "return-to-bot"):
        assert (await app_client.post(f"{ajena}/{accion}", headers=headers_b)).status_code == 404
    r = await app_client.post(f"{ajena}/notes", json={"text": "intruso"}, headers=headers_b)
    assert r.status_code == 404

    # Y la conversación del otro negocio quedó intacta.
    headers_a = await api_headers(app_client, a["tid"])
    d = (await app_client.get(ajena, headers=headers_a)).json()
    assert d["bot_paused"] is True and d["escalation"]["assigned_admin_id"] is None
    assert [x["text"] for x in d["notes"]] == ["Cliente pide factura a otro NIT"]
