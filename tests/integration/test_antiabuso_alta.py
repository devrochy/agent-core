"""HU-SH-07 · Antiabuso y cumplimiento del alta automática (ADR-0125), contra la BD real.

Gherkin de la HU:
- Un Free por persona: un teléfono o documento que ya tuvo un Free en los últimos 12 meses
  no saca otro; la tool pide ofrecer un plan pago.
- Términos aceptados: al confirmar la compra se aceptan términos, privacidad y DPA con
  enlace a la versión vigente, y queda quién, cuándo y qué versión.
- Revisión humana: dominio desechable, muchos intentos o datos inconsistentes → el alta queda
  en revisión y se avisa al equipo.
- Interruptor: con el alta pausada por el master, las altas nuevas quedan en revisión.
- Límites de tasa: más de N pedidos de alta por hora desde un mismo origen se frenan.

Además: aislamiento entre negocios de las tablas nuevas, la API de Plataforma solo para el
master y solo sobre vendedores, y la aprobación de un alta en revisión.
"""

from __future__ import annotations

import itertools
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _alta_helpers import aceptar_terminos, publicar_documentos
from _api_helpers import api_headers, uniq
from sqlalchemy import select, update

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_SEQ = itertools.count(1)


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def vendedores(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    from core.config import get_settings

    def fijar(*valores: Any) -> None:
        monkeypatch.setenv("PROVISIONING_SELLER_TENANTS", ",".join(str(v) for v in valores))
        get_settings.cache_clear()

    yield fijar
    monkeypatch.delenv("PROVISIONING_SELLER_TENANTS", raising=False)
    monkeypatch.delenv("ALTA_PEDIDOS_POR_HORA", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def silencio(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Sin correo, WhatsApp ni push reales. Devuelve los push al equipo (título, cuerpo)."""
    from core.gateway.event_notices import NoticeResult

    async def correo(to: str, subject: str, body: str) -> str:
        return "msg-test"

    async def aviso(*_a: Any, **_k: Any) -> NoticeResult:
        return NoticeResult("texto")

    pushes: list[tuple[str, str]] = []

    class _Push:
        @staticmethod
        def delay(_tenant: str, titulo: str, cuerpo: str, _datos: Any) -> None:
            pushes.append((titulo, cuerpo))

    monkeypatch.setattr("core.gateway.system_email.send_system_email", correo)
    monkeypatch.setattr("core.gateway.event_notices.send_event_notice", aviso)
    monkeypatch.setattr("core.tasks.push_tasks.notify_owners_push", _Push)
    return pushes


# ------------------------------------------------------------------ helpers
async def _vendedor() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("vendedor-sh07")
    tid, _ = await onboard_tenant(name=f"Vendedor {slug}", slug=slug, packs=["pack_commerce", "pack_subscriptions"])
    return tid


async def _cliente(tid: uuid.UUID, telefono: str | None = None) -> uuid.UUID:
    """Cliente de WhatsApp del vendedor, con su conversación y un mensaje suyo."""
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    numero = telefono or f"+57{3000000000 + uuid.uuid4().int % 10**9}"
    async with tenant_scope(tid):
        async with get_session() as s:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{numero}", name="Ana")
            s.add(cliente)
            await s.flush()
            conversacion = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
            s.add(conversacion)
            await s.flush()
            s.add(
                Message(
                    tenant_id=tid, conversation_id=conversacion.id, direction="inbound",
                    sender_type="customer", content="Sí, acepto",
                )
            )
            return cliente.id


async def _conversacion(tid: uuid.UUID, cliente: uuid.UUID) -> uuid.UUID:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return (
                await s.execute(select(Conversation.id).where(Conversation.customer_id == cliente))
            ).scalar_one()


async def _cotizacion(
    tid: uuid.UUID, cliente: uuid.UUID, *, plan: str = "free", precio: int = 0, extra: int = 0
) -> str:
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            producto = Product(tenant_id=tid, name=f"Plan {plan} {uuid.uuid4().hex[:4]}")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(
                tenant_id=tid, product_id=producto.id, sku=f"PLAN-{uuid.uuid4().hex[:8]}", price=precio,
                billing_period="mensual", trial_days=30 if precio == 0 else 0,
                attributes={"periodicidad": "mensual", "precio_de_plan": plan},
            )
            s.add(variante)
            await s.flush()
            cotizacion = Quote(
                tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=cliente,
                subtotal=precio + extra, discount=0, total=precio + extra, status="aceptada",
            )
            s.add(cotizacion)
            await s.flush()
            s.add(QuoteItem(tenant_id=tid, quote_id=cotizacion.id, variant_id=variante.id, quantity=1, unit_price=precio))
            if extra:
                # Un producto pago en la misma cotización: el Free sigue siendo un Free.
                otro = Product(tenant_id=tid, name=f"Extra {uuid.uuid4().hex[:4]}")
                s.add(otro)
                await s.flush()
                variante_extra = ProductVariant(
                    tenant_id=tid, product_id=otro.id, sku=f"X-{uuid.uuid4().hex[:8]}", price=extra
                )
                s.add(variante_extra)
                await s.flush()
                s.add(QuoteItem(tenant_id=tid, quote_id=cotizacion.id, variant_id=variante_extra.id, quantity=1, unit_price=extra))
            return str(cotizacion.id)


def _datos(**extra: Any) -> dict[str, Any]:
    datos = {
        "full_name": "Ana Pérez",
        "id_document": f"{10**9 + uuid.uuid4().int % 10**9}",
        "phone": f"+57 31{uuid.uuid4().int % 10**8:08d}",
        "email": f"{uniq('owner')}@correo-prueba.co",
        "business_name": "Pizzería Roma",
    }
    datos.update(extra)
    return datos


async def _pedir(
    tid: uuid.UUID, cliente: uuid.UUID, datos: dict[str, Any], *, plan: str = "free", precio: int = 0, extra: int = 0
) -> dict[str, Any]:
    """`crear_pedido` de un plan, como lo llama el asistente."""
    from pack_commerce.skills import OrderSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    quote = await _cotizacion(tid, cliente, plan=plan, precio=precio, extra=extra)
    ctx = ToolContext(
        conversation_id=await _conversacion(tid, cliente), customer_id=cliente,
        idempotency_key=f"t:{uuid.uuid4().hex[:8]}", channel="whatsapp",
    )
    async with tenant_scope(tid):
        return await OrderSkill().crear_pedido(
            {"quote_id": quote, "payment_method": "transferencia", "customer_data": datos}, ctx
        )


async def _mensaje_del_cliente(tid: uuid.UUID, cliente: uuid.UUID, texto: str = "Sí, acepto") -> uuid.UUID:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    conversacion = await _conversacion(tid, cliente)
    async with tenant_scope(tid):
        async with get_session() as s:
            mensaje = Message(
                tenant_id=tid, conversation_id=conversacion, direction="inbound", sender_type="customer", content=texto
            )
            s.add(mensaje)
            await s.flush()
            return mensaje.id


async def _pedir_aceptando(tid: uuid.UUID, cliente: uuid.UUID, datos: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """Primero los términos pendientes; el cliente responde que acepta (un mensaje suyo
    posterior) y se vuelve a pedir con el token."""
    primero = await _pedir(tid, cliente, datos, **kw)
    if primero.get("status") != "terminos_pendientes":
        return primero
    await _mensaje_del_cliente(tid, cliente)
    return await _pedir(tid, cliente, {**datos, "acepta_terminos": primero["acepta_terminos"]}, **kw)


async def _entregar_aprobacion(tid: uuid.UUID, referencia: str) -> Any:
    """Entrega `provisioning.alta_aprobada` al consumidor, como el despachador del outbox."""
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.events.outbox import payload_para_handlers
    from core.provisioning.consumer import on_alta_aprobada
    from core.provisioning.revision import por_referencia
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            eventos = (
                await s.execute(
                    select(DomainEvent).where(
                        DomainEvent.tenant_id == tid, DomainEvent.topic == "provisioning.alta_aprobada"
                    )
                )
            ).scalars().all()
        [evento] = [e for e in eventos if e.payload.get("referencia") == referencia][-1:]
        await on_alta_aprobada(**payload_para_handlers(evento.payload))
    return await por_referencia(tid, referencia)


async def _pedido_de_alta(tid: uuid.UUID, referencia: str) -> Any:
    from core.provisioning.antiabuso import pedido_de_alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await pedido_de_alta(tid, referencia)


async def _entregar(tid: uuid.UUID, referencia: str) -> Any:
    """Entrega `subscription.activated` del pedido al consumidor, como el despachador."""
    from core.db.models import DomainEvent, ProvisioningRequest
    from core.db.session import get_session
    from core.events.outbox import payload_para_handlers
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            eventos = (
                await s.execute(
                    select(DomainEvent).where(
                        DomainEvent.tenant_id == tid, DomainEvent.topic == "subscription.activated"
                    )
                )
            ).scalars().all()
        [evento] = [e for e in eventos if e.payload.get("referencia") == referencia]
        await on_subscription_activated(**payload_para_handlers(evento.payload))
        async with get_session() as s:
            return (
                await s.execute(select(ProvisioningRequest).where(ProvisioningRequest.referencia == referencia))
            ).scalar_one()


async def _pedido_activado(
    tid: uuid.UUID,
    *,
    email: str | None = None,
    documento: str | None = None,
    telefono: str | None = None,
    plan: str = "premium",
    precio: int = 600_000,
    aceptar: bool = True,
    senales: list[str] | None = None,
    cliente: uuid.UUID | None = None,
) -> str:
    """Pedido pagado armado a mano (sin la tool) + su registro de alta; emite el evento."""
    from pack_commerce.models import Order, OrderItem, Product, ProductVariant
    from pack_commerce.planes import activar_planes_del_pedido

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    seq = next(_SEQ) + 700
    cliente = cliente or uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as s:
            producto = Product(tenant_id=tid, name=f"Plan {plan}")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(
                tenant_id=tid, product_id=producto.id, sku=f"PLAN-{seq}-{uuid.uuid4().hex[:4]}", price=precio,
                billing_period="mensual", trial_days=30 if precio == 0 else 0,
                attributes={"periodicidad": "mensual", "precio_de_plan": plan},
            )
            s.add(variante)
            await s.flush()
            order = Order(
                tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=cliente, status="abierto",
                payment_status="pagado", payment_method="pago_en_linea", delivery_method="recoger_en_tienda",
                total=precio, idempotency_key=str(uuid.uuid4()), order_seq=seq,
                public_order_number=f"SH-{seq:04d}", delivery_full_name="Ana Pérez",
                datos_alta={"email": email or f"{uniq('owner')}@correo-prueba.co", "negocio": "Acme"},
            )
            s.add(order)
            await s.flush()
            s.add(OrderItem(tenant_id=tid, order_id=order.id, variant_id=variante.id, quantity=1, unit_price=precio))
            await s.flush()
            assert await activar_planes_del_pedido(s, order) == 1
            referencia = order.public_order_number
    await aceptar_terminos(
        tid, referencia, customer_id=cliente, gratis=precio == 0, telefono=telefono,
        documento=documento, senales=senales, con_aceptacion=aceptar,
    )
    return referencia


# ------------------------------------------------------------------ términos aceptados
async def test_sin_aceptar_los_terminos_no_se_crea_el_pedido_y_se_muestran_las_versiones(vendedores, silencio) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    await publicar_documentos(vendedor, version="2026-10")
    cliente = await _cliente(vendedor)

    resultado = await _pedir(vendedor, cliente, _datos())

    assert resultado["status"] == "terminos_pendientes"
    assert [d["tipo"] for d in resultado["documentos"]] == ["terminos", "privacidad", "dpa"]
    assert all(d["version"] == "2026-10" and d["url"].startswith("https://") for d in resultado["documentos"])
    assert "order_id" not in resultado
    # Un valor inventado no sirve: solo el que mostró la tool.
    otra = await _pedir(vendedor, cliente, _datos(acepta_terminos="si"))
    assert otra["status"] == "terminos_pendientes"


async def test_la_aceptacion_queda_registrada_y_llega_al_negocio_nuevo(vendedores, silencio) -> None:
    """Quién, cuándo y qué versión: en el pedido de alta del vendedor y en el negocio creado."""
    from core.db.models import AuditLog, Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    await publicar_documentos(vendedor, version="v7")
    cliente = await _cliente(vendedor)
    antes = datetime.now(UTC)

    resultado = await _pedir_aceptando(vendedor, cliente, _datos())

    assert resultado["status"] == "confirmed", resultado
    referencia = resultado["public_order_number"]
    registro = await _pedido_de_alta(vendedor, referencia)
    assert registro.customer_id == cliente
    assert registro.aceptado_en >= antes
    assert {d["tipo"]: d["version"] for d in registro.aceptacion["documentos"]} == {
        "terminos": "v7", "privacidad": "v7", "dpa": "v7",
    }
    assert registro.aceptacion["mensaje_id"]  # el mensaje del cliente que aceptó
    assert registro.gratis is True
    assert len(registro.huellas) == 3  # WhatsApp, teléfono del checkout y documento
    assert all(h.startswith(("tel:", "doc:")) and len(h) == 68 for h in registro.huellas)

    alta = await _entregar(vendedor, referencia)
    assert alta.estado == "completado", alta.motivo
    async with get_session() as s:
        negocio = await s.get(Tenant, alta.negocio_tenant_id)
    aceptacion = negocio.settings["alta"]["aceptacion"]
    assert [d["version"] for d in aceptacion["documentos"]] == ["v7", "v7", "v7"]
    async with tenant_scope(negocio.id):
        async with get_session() as s:
            [creado] = (
                await s.execute(select(AuditLog).where(AuditLog.action == "negocio_creado"))
            ).scalars().all()
    assert creado.input["aceptacion"]["aceptado_en"] == registro.aceptado_en.isoformat()


async def test_una_version_nueva_pide_aceptar_otra_vez(vendedores, silencio) -> None:
    from core.provisioning import legal

    vendedor = await _vendedor()
    vendedores(vendedor)
    await publicar_documentos(vendedor, version="v1")
    cliente = await _cliente(vendedor)
    primero = await _pedir(vendedor, cliente, _datos())

    # Llega el texto del abogado: se publica otra versión, sin tocar código.
    await legal.publicar(vendedor, tipo="terminos", version="v2", url="https://solvershub.test/terminos/v2", por="test")
    segundo = await _pedir(vendedor, cliente, _datos(acepta_terminos=primero["acepta_terminos"]))

    assert segundo["status"] == "terminos_pendientes"
    assert segundo["acepta_terminos"] != primero["acepta_terminos"]
    assert {d["tipo"]: d["version"] for d in segundo["documentos"]}["terminos"] == "v2"
    with pytest.raises(legal.VersionRepetida):
        await legal.publicar(vendedor, tipo="terminos", version="v2", url="https://solvershub.test/x", por="test")


async def test_el_token_solo_vale_con_un_mensaje_posterior_del_cliente_y_en_su_conversacion(vendedores, silencio) -> None:
    """El modelo no puede aceptar por el cliente: repetir el token en el mismo turno (sin un
    mensaje del cliente después de mostrar los enlaces) o traerlo de otra conversación no
    crea el pedido."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    await publicar_documentos(vendedor)
    cliente = await _cliente(vendedor)
    datos = _datos()

    primero = await _pedir(vendedor, cliente, datos)
    mismo_turno = await _pedir(vendedor, cliente, {**datos, "acepta_terminos": primero["acepta_terminos"]})
    assert mismo_turno["status"] == "terminos_pendientes"

    otro = await _cliente(vendedor)
    await _pedir(vendedor, otro, _datos())
    await _mensaje_del_cliente(vendedor, otro)
    ajeno = await _pedir(vendedor, otro, {**_datos(), "acepta_terminos": primero["acepta_terminos"]})
    assert ajeno["status"] == "terminos_pendientes"

    mensaje = await _mensaje_del_cliente(vendedor, cliente)
    aceptado = await _pedir(vendedor, cliente, {**datos, "acepta_terminos": mismo_turno["acepta_terminos"]})
    assert aceptado["status"] == "confirmed", aceptado
    registro = await _pedido_de_alta(vendedor, aceptado["public_order_number"])
    assert registro.aceptacion["mensaje_id"] == str(mensaje)


async def test_un_free_con_un_producto_pago_al_lado_sigue_siendo_un_free(vendedores, silencio) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    await publicar_documentos(vendedor)
    datos = _datos()
    mixto = await _pedir_aceptando(vendedor, await _cliente(vendedor), datos, extra=50_000)
    assert mixto["status"] == "confirmed", mixto
    assert (await _pedido_de_alta(vendedor, mixto["public_order_number"])).gratis is True
    otra_vez = await _pedir_aceptando(vendedor, await _cliente(vendedor), _datos(id_document=datos["id_document"]))
    assert otra_vez["status"] == "free_no_disponible"


async def test_sin_documentos_publicados_el_alta_va_a_revision(vendedores, silencio) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia = await _pedido_activado(vendedor, aceptar=False)

    alta = await _entregar(vendedor, referencia)

    assert alta.estado == "revision"
    assert "aceptación" in alta.motivo
    assert alta.negocio_tenant_id is None
    assert any(t == "Alta automática en revisión" for t, _ in silencio)


# ------------------------------------------------------------------ un Free por persona
async def test_un_documento_o_telefono_con_free_en_12_meses_no_saca_otro(vendedores, silencio) -> None:
    from core.db.models import PedidoDeAlta
    from core.db.session import get_session
    from core.provisioning.antiabuso import olvidar_huellas_vencidas
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    await publicar_documentos(vendedor)
    datos = _datos()
    primero = await _pedir_aceptando(vendedor, await _cliente(vendedor), datos)
    assert primero["status"] == "confirmed", primero

    # Otro número de WhatsApp, mismo documento (con otro formato): Free no.
    otro = await _cliente(vendedor)
    mismo_doc = _datos(id_document=f"{int(datos['id_document']):,}".replace(",", "."))
    rechazo = await _pedir_aceptando(vendedor, otro, mismo_doc)
    assert rechazo["status"] == "free_no_disponible"
    assert "plan pago" in rechazo["hint"]
    # Mismo teléfono del checkout, otro documento: tampoco.
    rechazo_tel = await _pedir_aceptando(vendedor, await _cliente(vendedor), _datos(phone=datos["phone"]))
    assert rechazo_tel["status"] == "free_no_disponible"
    # Un plan pago sí.
    pago = await _pedir_aceptando(vendedor, otro, mismo_doc, plan="basic", precio=150_000)
    assert pago["status"] == "confirmed", pago

    # Pasados 12 meses, el cron vacía las huellas (la aceptación queda) y vuelve a poder.
    assert await olvidar_huellas_vencidas(ahora=datetime.now(UTC) + timedelta(days=400)) >= 3
    async with tenant_scope(vendedor):
        async with get_session() as s:
            filas = (
                await s.execute(select(PedidoDeAlta).where(PedidoDeAlta.tenant_id == vendedor))
            ).scalars().all()
    assert all(f.huellas == [] for f in filas)
    assert all(f.aceptacion is not None for f in filas)
    despues = await _pedir_aceptando(vendedor, await _cliente(vendedor), mismo_doc)
    assert despues["status"] == "confirmed", despues


async def test_un_free_repetido_que_llega_al_alta_va_a_revision(vendedores, silencio) -> None:
    """El respaldo del consumidor: un pedido Free que no pasó por la tool (p. ej. creado a
    mano) con un documento que ya tuvo Free no crea otro negocio."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    primero = await _pedido_activado(vendedor, plan="free", precio=0, documento="1.020.304.050")
    assert (await _entregar(vendedor, primero)).estado == "completado"

    segundo = await _pedido_activado(vendedor, plan="free", precio=0, documento="1020304050")
    alta = await _entregar(vendedor, segundo)

    assert alta.estado == "revision"
    assert "un Free por persona" in alta.motivo


# ------------------------------------------------------------------ revisión humana
async def test_un_correo_desechable_va_a_revision_y_avisa_al_equipo(vendedores, silencio) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia = await _pedido_activado(vendedor, email=f"{uniq('x')}@mailinator.com")

    alta = await _entregar(vendedor, referencia)

    assert alta.estado == "revision"
    assert "dominio desechable" in alta.motivo
    assert alta.negocio_tenant_id is None
    [(_titulo, cuerpo)] = [p for p in silencio if p[0] == "Alta automática en revisión"]
    assert referencia in cuerpo


async def test_muchos_intentos_de_la_misma_persona_van_a_revision(vendedores, silencio) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    documento = f"{10**9 + uuid.uuid4().int % 10**9}"
    estados = []
    for _ in range(3):
        referencia = await _pedido_activado(vendedor, documento=documento)
        estados.append(await _entregar(vendedor, referencia))

    assert [a.estado for a in estados] == ["completado", "completado", "revision"]
    assert "3 pedidos de alta de la misma persona en 24 h" in estados[2].motivo


async def test_datos_inconsistentes_van_a_revision_y_el_master_la_aprueba(vendedores, silencio) -> None:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.provisioning.antiabuso import SENAL_DOCUMENTO
    from core.provisioning.consumer import reintentar_alta
    from core.provisioning.revision import aprobar_alta
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia = await _pedido_activado(vendedor, senales=[SENAL_DOCUMENTO])

    alta = await _entregar(vendedor, referencia)
    assert alta.estado == "revision"
    assert "datos inconsistentes (documento_invalido)" in alta.motivo

    # Reintentar vuelve a aplicar todos los controles y borra una aprobación anterior.
    marcada = await aprobar_alta(vendedor, referencia, por="admin:master@test")
    assert marcada.estado == "en_curso"
    vuelta = await reintentar_alta(vendedor, referencia)
    assert vuelta.estado == "revision"
    assert "aprobacion" not in vuelta.detalle

    # Aprobar deja el alta en curso y la corre el worker (evento del outbox).
    marcada = await aprobar_alta(vendedor, referencia, por="admin:master@test")
    assert marcada.estado == "en_curso"
    aprobada = await _entregar_aprobacion(vendedor, referencia)
    assert aprobada.estado == "completado", aprobada.motivo
    with pytest.raises(ValueError, match="solo se aprueba una en revisión"):
        await aprobar_alta(vendedor, referencia, por="admin:master@test")
    assert aprobada.detalle["aprobacion"]["por"] == "admin:master@test"
    async with tenant_scope(vendedor):
        async with get_session() as s:
            auditoria = (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "aprovisionamiento_aprobado")
                    .order_by(AuditLog.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
    assert auditoria.actor == "admin:master@test"
    assert "datos inconsistentes" in auditoria.input["motivo_revision"]


# ------------------------------------------------------------------ interruptor
async def test_con_el_alta_pausada_las_altas_nuevas_quedan_en_revision(vendedores, silencio) -> None:
    from core.provisioning import interruptor
    from core.provisioning.consumer import reintentar_alta

    vendedor = await _vendedor()
    vendedores(vendedor)
    await interruptor.fijar(vendedor, pausar=True, motivo="ataque de altas", por="admin:master@test")

    referencia = await _pedido_activado(vendedor)
    alta = await _entregar(vendedor, referencia)

    assert alta.estado == "revision"
    assert "pausada por el master" in alta.motivo
    assert alta.negocio_tenant_id is None
    assert any(t == "Alta automática en revisión" for t, _ in silencio)
    # Pausada, reintentar no la saca de la cola; al reanudar, sí.
    assert (await reintentar_alta(vendedor, referencia)).estado == "revision"
    await interruptor.fijar(vendedor, pausar=False, motivo=None, por="admin:master@test")
    assert (await reintentar_alta(vendedor, referencia)).estado == "completado"


async def test_la_revision_muestra_todos_los_motivos(vendedores, silencio) -> None:
    """Un alta frenada por el interruptor que además es un Free repetido (ADR-0121) muestra
    los dos motivos: quien aprueba la cola después del ataque ve todo lo que se salta."""
    from core.provisioning import interruptor

    vendedor = await _vendedor()
    vendedores(vendedor)
    correo = f"{uniq('owner')}@correo-prueba.co"
    primera = await _pedido_activado(vendedor, email=correo, plan="free", precio=0)
    assert (await _entregar(vendedor, primera)).estado == "completado"
    await interruptor.fijar(vendedor, pausar=True, motivo="ataque", por="admin:master@test")

    segunda = await _pedido_activado(vendedor, email=correo, plan="free", precio=0)
    alta = await _entregar(vendedor, segunda)

    assert alta.estado == "revision"
    assert "pausada por el master" in alta.motivo
    assert "ya hubo un alta para este cliente o este correo" in alta.motivo


# ------------------------------------------------------------------ límites de tasa
async def test_mas_de_n_pedidos_de_alta_por_hora_desde_un_origen_se_frenan(vendedores, silencio, monkeypatch) -> None:
    from core.config import get_settings

    vendedor = await _vendedor()
    vendedores(vendedor)
    monkeypatch.setenv("ALTA_PEDIDOS_POR_HORA", "2")
    get_settings.cache_clear()
    await publicar_documentos(vendedor)
    cliente = await _cliente(vendedor)

    resultados = [
        await _pedir_aceptando(vendedor, cliente, _datos(), plan="basic", precio=150_000) for _ in range(3)
    ]

    assert [r["status"] for r in resultados] == ["confirmed", "confirmed", "limite_de_altas"]
    # Otro origen sigue pudiendo.
    otro = await _pedir_aceptando(vendedor, await _cliente(vendedor), _datos(), plan="basic", precio=150_000)
    assert otro["status"] == "confirmed"


# ------------------------------------------------------------------ aislamiento
@pytest.mark.aislamiento
async def test_las_tablas_del_antiabuso_no_se_ven_desde_otro_negocio(vendedores, silencio) -> None:
    from core.db.models import DocumentoLegal, PedidoDeAlta
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    otro = await _vendedor()
    vendedores(vendedor)
    await _pedido_activado(vendedor, documento="99887766")

    async with tenant_scope(otro):
        async with get_session() as s:
            assert (await s.execute(select(PedidoDeAlta).where(PedidoDeAlta.tenant_id == vendedor))).all() == []
            assert (await s.execute(select(DocumentoLegal).where(DocumentoLegal.tenant_id == vendedor))).all() == []
        # Publicar a nombre de otro negocio: la RLS lo rechaza.
        with pytest.raises(Exception, match="row-level security"):
            async with get_session() as s:
                s.add(
                    DocumentoLegal(
                        tenant_id=vendedor, tipo="dpa", version="intruso", url="https://x.test/dpa", publicado_por="x"
                    )
                )
                await s.flush()


@pytest.mark.aislamiento
async def test_el_token_de_terminos_de_un_negocio_no_vale_en_otro(vendedores, silencio) -> None:
    """`acepta_terminos` está en el schema de `crear_pedido` (ADR-0129): un token que el
    modelo trae de la conversación de otro vendedor no acepta nada ni crea el pedido."""
    from pack_commerce.models import Order

    from core.db.models import PedidoDeAlta
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    otro = await _vendedor()
    vendedores(vendedor, otro)
    await publicar_documentos(vendedor)
    await publicar_documentos(otro)
    cliente = await _cliente(vendedor)
    ajeno = await _cliente(otro)
    token = (await _pedir(vendedor, cliente, _datos()))["acepta_terminos"]
    await _pedir(otro, ajeno, _datos())
    await _mensaje_del_cliente(otro, ajeno)

    resultado = await _pedir(otro, ajeno, _datos(acepta_terminos=token))

    assert resultado["status"] == "terminos_pendientes"
    assert resultado["acepta_terminos"] != token
    async with tenant_scope(otro):
        async with get_session() as s:
            assert (await s.execute(select(Order).where(Order.customer_id == ajeno))).all() == []
            assert (await s.execute(select(PedidoDeAlta).where(PedidoDeAlta.customer_id == ajeno))).all() == []
    # Control: con su propio token y un mensaje suyo después, el mismo cliente sí compra.
    await _mensaje_del_cliente(otro, ajeno)
    propio = await _pedir(otro, ajeno, _datos(acepta_terminos=resultado["acepta_terminos"]))
    assert propio["status"] == "confirmed", propio


@pytest.mark.aislamiento
async def test_la_api_de_plataforma_es_solo_del_master_y_solo_de_vendedores(app_client, vendedores, silencio) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    vendedor = await _vendedor()
    no_vendedor = await _vendedor()
    vendedores(vendedor)
    base = "/api/v1/admin/platform/altas-automaticas"
    master = await api_headers(app_client, vendedor)

    # El dueño del vendedor no es el master: 403 en todo.
    email = f"{uniq('duenio')}@example.test"
    async with get_session() as s:
        duenio = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        s.add(duenio)
        await s.flush()
        s.add(TenantUser(tenant_id=vendedor, admin_id=duenio.id, role="owner"))
    propio = await api_headers(app_client, vendedor, email=email, password="clave-segura-123")
    assert (await app_client.get(base, headers=propio)).status_code == 403
    r = await app_client.put(f"{base}/{vendedor}/interruptor", json={"pausado": True, "motivo": "x"}, headers=propio)
    assert r.status_code == 403

    # Un negocio que no está en la allowlist no existe para esta API.
    r = await app_client.put(f"{base}/{no_vendedor}/interruptor", json={"pausado": True, "motivo": "x"}, headers=master)
    assert r.status_code == 404

    # El master pausa, publica documentos y ve la cola.
    r = await app_client.put(f"{base}/{vendedor}/interruptor", json={"pausado": True}, headers=master)
    assert r.status_code == 400  # sin motivo
    r = await app_client.put(
        f"{base}/{vendedor}/interruptor", json={"pausado": True, "motivo": "ataque"}, headers=master
    )
    assert r.status_code == 200, r.text
    assert r.json()["interruptor"]["pausado"] is True
    assert r.json()["documentos_faltantes"] == ["terminos", "privacidad", "dpa"]
    for tipo in ("terminos", "privacidad", "dpa"):
        r = await app_client.post(
            f"{base}/{vendedor}/documentos",
            json={"tipo": tipo, "version": "2026-10-09", "url": f"https://solvershub.test/{tipo}"},
            headers=master,
        )
        assert r.status_code == 201, r.text
    r = await app_client.post(
        f"{base}/{vendedor}/documentos",
        json={"tipo": "dpa", "version": "otra", "url": "http://inseguro.test/dpa"},
        headers=master,
    )
    assert r.status_code == 400
    [resumen] = [v for v in (await app_client.get(base, headers=master)).json() if v["id"] == str(vendedor)]
    assert resumen["documentos_faltantes"] == []
    assert resumen["interruptor"]["por"].startswith("admin:")

    referencia = await _pedido_activado(vendedor)
    assert (await _entregar(vendedor, referencia)).estado == "revision"
    cola = (await app_client.get(f"{base}/{vendedor}/altas", headers=master)).json()
    assert [a["referencia"] for a in cola] == [referencia]
    assert "email" not in cola[0]
    r = await app_client.post(f"{base}/{vendedor}/altas/{referencia}/aprobar", headers=master)
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "en_curso"  # la corre el worker
    assert r.json()["aprobada"] is True
    assert (await _entregar_aprobacion(vendedor, referencia)).estado == "completado"
    r = await app_client.post(f"{base}/{vendedor}/altas/{referencia}/aprobar", headers=master)
    assert r.status_code == 409


async def test_la_aceptacion_y_las_versiones_publicadas_no_se_editan_ni_se_borran(vendedores, silencio) -> None:
    """Son la prueba del contrato: la base lo impide, no solo el código."""
    from sqlalchemy import delete

    from core.db.models import DocumentoLegal, PedidoDeAlta
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia = await _pedido_activado(vendedor, documento="55667788")

    cambios = [
        update(PedidoDeAlta).where(PedidoDeAlta.referencia == referencia).values(aceptacion={"documentos": []}),
        update(PedidoDeAlta).where(PedidoDeAlta.referencia == referencia).values(aceptado_en=datetime.now(UTC)),
        delete(PedidoDeAlta).where(PedidoDeAlta.referencia == referencia),
        update(DocumentoLegal).where(DocumentoLegal.tenant_id == vendedor).values(url="https://otro.test/x"),
        delete(DocumentoLegal).where(DocumentoLegal.tenant_id == vendedor),
    ]
    for cambio in cambios:
        with pytest.raises(Exception, match=r"no se edita|sin borrado"):
            async with tenant_scope(vendedor):
                async with get_session() as s:
                    await s.execute(cambio)
    # Lo que sí puede cambiar: las huellas (retención) y el borrado lógico.
    async with tenant_scope(vendedor):
        async with get_session() as s:
            await s.execute(update(PedidoDeAlta).where(PedidoDeAlta.referencia == referencia).values(huellas=[]))
            await s.execute(
                update(DocumentoLegal)
                .where(DocumentoLegal.tenant_id == vendedor, DocumentoLegal.tipo == "dpa")
                .values(deleted_at=datetime.now(UTC))
            )


async def test_un_alta_en_revision_por_un_dato_no_se_aprueba(vendedores, silencio) -> None:
    """Aprobar salta los controles de riesgo; una revisión por un dato (sin correo) nunca los
    evaluó, así que se corrige y se reintenta."""
    from core.provisioning.revision import aprobar_alta

    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia = await _pedido_activado(vendedor, email="no-es-un-correo")

    alta = await _entregar(vendedor, referencia)
    assert alta.estado == "revision"
    assert "correo" in alta.motivo
    with pytest.raises(ValueError, match="por un dato"):
        await aprobar_alta(vendedor, referencia, por="admin:master@test")
