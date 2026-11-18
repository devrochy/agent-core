"""Acción `comprobar_comprobante` (SPEC-48, HU-COM-24, ADR-0203).

Lee el comprobante de transferencia que el cliente mandó y decide con la regla D4:

1. El pedido tiene que ser del cliente de la conversación y por transferencia.
2. La imagen es la última imagen ENTRANTE de esta conversación, posterior al pedido.
3. Idempotencia: el mismo archivo (`image_sha256`) para el mismo pedido devuelve el resultado
   guardado, sin volver a llamar a la visión ni cobrar. El mismo archivo en otro pedido es
   `duplicado` (va a revisión).
4. Con la lectura activada, la visión copia los datos (`leer_comprobante`, cuota antes de llamar)
   y `evaluar_comprobante` decide; sin ella, el comprobante queda en revisión para una persona.
5. Se guarda la evidencia (`pack_commerce_payment_proofs`, la cuenta solo con 4 dígitos) y se
   actualiza `payment_status`; `pagado` encola `order.paid` en la misma transacción.

Al modelo le vuelve solo el resultado y el total del servidor: nunca el texto que leyó la visión
ni las cuentas. Sin imagen, sin cuota o sin visión es un error de negocio (el agente pide el
comprobante o escala); nunca una excepción.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

MAX_BYTES = 8 * 1024 * 1024
_RESUMEN = {
    "pagado": "El comprobante coincide con el pedido: el pago quedó confirmado.",
    "en_revision": "El comprobante quedó en revisión: una persona del negocio lo confirma.",
    "rechazado": "El comprobante no coincide con el pedido: el pago no quedó confirmado.",
}


def _salida(order: Any, status: str, *, repetido: bool = False) -> dict[str, Any]:
    salida: dict[str, Any] = {
        "order_id": str(order.id),
        "public_order_number": order.public_order_number,
        "resultado": status,
        "total": float(Decimal(str(order.total))),
        "detalle": _RESUMEN[status],
        # El dato que cierra la tarea solo existe si el servidor lo confirmó.
        "comprobante": "recibido",
    }
    if status == "pagado":
        salida["pago_confirmado"] = "confirmado"
    if status != "pagado":
        salida["hint"] = (
            "Dile al cliente que una persona revisa su comprobante y le confirma."
            if status == "en_revision"
            else "Dile que el comprobante no coincide con el pedido y ofrécele hablar con una persona."
        )
    if repetido:
        salida["repetido"] = True
    return salida


async def _ultima_imagen(session: Any, tenant_id: uuid.UUID, conversation_id: Any, desde: Any) -> Any:
    from core.db.models import Message

    return (
        await session.execute(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversation_id,
                Message.direction == "inbound",
                Message.media_type.like("image/%"),
                Message.media_url.is_not(None),
                Message.created_at >= desde,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _leer_archivo(key: str) -> bytes | None:
    """El archivo, solo si está en la carpeta de medios de ESTE negocio (una key ajena o hostil,
    de un dato viejo o de otro canal, no se abre)."""
    from core import almacenamiento
    from core.shared.tenant import current_tenant_slug

    slug = await current_tenant_slug()
    if not slug or not almacenamiento.key_limpia(key):
        return None
    ruta = almacenamiento.abrir(key, dentro_de=almacenamiento.carpeta_de("media", slug))
    if ruta is None or not ruta.is_file() or ruta.stat().st_size > MAX_BYTES:
        return None
    return ruta.read_bytes()


async def comprobar(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    from core.db.session import get_session
    from pack_commerce.comprobantes import (
        ComprobanteNoLeido,
        ComprobanteTransferencia,
        PedidoAComparar,
        evaluar_comprobante,
        extraido_para_guardar,
        leer_comprobante,
    )
    from pack_commerce.formas_de_pago import config_del_agente
    from pack_commerce.models import Order, PaymentProof
    from pack_commerce.module import CRMModule

    tenant_id = get_current_tenant_id()
    try:
        order_uuid = uuid.UUID(str(arguments.get("order_id") or ""))
    except ValueError:
        return {"error": "pedido_no_encontrado", "detail": "Pedido no encontrado."}
    nota = str(arguments.get("nota") or "").strip()[:300] or None

    async with get_session() as session:
        order = (
            await session.execute(
                select(Order).where(
                    Order.tenant_id == tenant_id, Order.id == order_uuid, Order.customer_id == context.customer_id
                )
            )
        ).scalar_one_or_none()
        if order is None:
            return {"error": "pedido_no_encontrado", "detail": "Pedido no encontrado."}
        if order.payment_method != "transferencia":
            return {"error": "no_es_transferencia", "detail": "Ese pedido no se paga por transferencia."}
        if order.payment_status == "pagado":
            return _salida(order, "pagado", repetido=True)
        mensaje = await _ultima_imagen(session, tenant_id, context.conversation_id, order.created_at)
        if mensaje is None:
            return {
                "error": "sin_comprobante",
                "detail": "Todavía no hay una foto del comprobante en esta conversación.",
                "hint": "Pídele al cliente la foto o captura del comprobante de la transferencia.",
            }
        image_key, message_id, mime = str(mensaje.media_url), mensaje.id, str(mensaje.media_type)
        creado, total = order.created_at, Decimal(str(order.total))

    contenido = await _leer_archivo(image_key)
    if contenido is None:
        return {
            "error": "sin_comprobante",
            "detail": "No pudimos abrir la foto del comprobante.",
            "hint": "Pídele al cliente que la mande de nuevo.",
        }
    sha = hashlib.sha256(contenido).hexdigest()

    async with get_session() as session:
        previos = (
            await session.execute(
                select(PaymentProof).where(
                    PaymentProof.tenant_id == tenant_id,
                    PaymentProof.image_sha256 == sha,
                    PaymentProof.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    mismo = next((p for p in previos if p.order_id == order_uuid), None)
    if mismo is not None:
        async with get_session() as session:
            order = (
                await session.execute(select(Order).where(Order.tenant_id == tenant_id, Order.id == order_uuid))
            ).scalar_one()
            return _salida(order, mismo.status, repetido=True)
    duplicado = bool(previos)

    config = await config_del_agente(context.agent_id)
    cuentas = list(getattr(config, "transfer_accounts", None) or [])
    if getattr(config, "transfer_ocr_enabled", True):
        try:
            extraido = await leer_comprobante(
                tenant_id,
                contenido,
                mime,
                resultado_ledger={"order_id": str(order_uuid), "image_sha256": sha},
                moneda=str(getattr(config, "currency_code", None) or "COP"),
            )
            # El comprobante trae la hora local del banco, sin zona: el pedido se compara en la zona
            # del negocio (con UTC, un pago de minutos después parecía anterior al pedido).
            from core.shared.tiempo_negocio import zona_del_negocio

            creado_local = creado.astimezone(await zona_del_negocio(tenant_id)) if creado.tzinfo else creado
            evaluacion = evaluar_comprobante(
                extraido, PedidoAComparar(total=total, creado=creado_local), cuentas, duplicado=duplicado
            )
            guardado, match = extraido_para_guardar(extraido), evaluacion.como_json()
            status = evaluacion.status
        except ComprobanteNoLeido as exc:
            if exc.motivo != "ilegible":
                log.info("comprobante_no_leido", motivo=exc.motivo, order_id=str(order_uuid))
                return {
                    "error": exc.motivo,
                    "detail": "No pudimos revisar el comprobante automáticamente; no se cobró.",
                    "hint": "Dile al cliente que una persona revisa su pago y pásalo a una persona (escalate_to_human).",
                    "escalar": True,
                }
            vacio = ComprobanteTransferencia()
            guardado, match, status = extraido_para_guardar(vacio), {"resultado": "en_revision", "motivos": ["ilegible"]}, "en_revision"
    else:
        guardado, match, status = {}, {"resultado": "en_revision", "motivos": ["lectura_apagada"]}, "en_revision"
    if nota:
        match["nota_del_cliente"] = nota

    async with get_session() as session:
        order = (
            await session.execute(
                select(Order).where(Order.tenant_id == tenant_id, Order.id == order_uuid).with_for_update()
            )
        ).scalar_one()
        try:
            async with session.begin_nested():
                session.add(
                    PaymentProof(
                        tenant_id=tenant_id,
                        order_id=order_uuid,
                        conversation_id=context.conversation_id,
                        message_id=message_id,
                        image_key=image_key,
                        image_sha256=sha,
                        extracted=guardado,
                        match=match,
                        status=status,
                    )
                )
        except IntegrityError:
            # Otra llamada guardó el mismo comprobante a la vez: vale el que quedó.
            previo = (
                await session.execute(
                    select(PaymentProof).where(
                        PaymentProof.tenant_id == tenant_id,
                        PaymentProof.order_id == order_uuid,
                        PaymentProof.image_sha256 == sha,
                        PaymentProof.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            return _salida(order, previo.status, repetido=True)
        if order.payment_status != "pagado":
            order.payment_status = status
            if status == "pagado":
                await CRMModule._emitir_pedido_pagado(session, tenant_id, order)
        log.info("comprobante_evaluado", order_id=str(order_uuid), resultado=status, duplicado=duplicado)
        return _salida(order, status)
