"""Ayudas de los tests del alta automática (HU-SH-03/04/07).

Desde HU-SH-07 (ADR-0125) un alta solo sigue sola si el pedido tiene la aceptación de
términos, privacidad y DPA registrada (`pedidos_de_alta`). Los tests que arman el pedido a
mano (sin la tool `crear_pedido`) la registran con `aceptar_terminos`, igual que la tool.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

from sqlalchemy import select

from core.provisioning import legal


async def publicar_documentos(vendedor: uuid.UUID, *, version: str = "v1") -> dict[str, legal.Documento]:
    """Publica términos, privacidad y DPA del vendedor si todavía no tiene versión vigente."""
    vigentes = await legal.documentos_vigentes(vendedor)
    for tipo in legal.faltantes(vigentes):
        await legal.publicar(
            vendedor, tipo=tipo, version=version, url=f"https://solvershub.test/{tipo}/{version}", por="test"
        )
    return await legal.documentos_vigentes(vendedor)


async def aceptar_terminos(
    vendedor: uuid.UUID,
    referencia: str,
    *,
    customer_id: uuid.UUID | None = None,
    gratis: bool = False,
    telefono: str | None = None,
    documento: str | None = None,
    senales: list[str] | None = None,
    con_aceptacion: bool = True,
) -> None:
    """Registra el pedido de alta de `referencia` como lo hace `crear_pedido`."""
    from datetime import UTC, datetime

    from core.db.session import get_session
    from core.provisioning.antiabuso import (
        PedidoPreparado,
        huella,
        normalizar_documento,
        normalizar_telefono,
        registrar_pedido_de_alta,
    )
    from core.shared.tenant import tenant_scope

    aceptacion: dict[str, Any] | None = None
    if con_aceptacion:
        vigentes = await publicar_documentos(vendedor)
        aceptacion = {
            "documentos": [vigentes[t].para_aceptar() for t in legal.TIPOS],
            "version_legal": legal.version_legal(vigentes),
            "mensaje_id": None,
        }
    huellas = []
    if telefono and normalizar_telefono(telefono):
        huellas.append(huella("tel", normalizar_telefono(telefono) or ""))
    if documento and normalizar_documento(documento):
        huellas.append(huella("doc", normalizar_documento(documento) or ""))
    pedido = PedidoPreparado(
        vendedor=vendedor,
        customer_id=customer_id,
        conversation_id=None,
        canal="whatsapp",
        gratis=gratis,
        huellas=huellas,
        senales=senales or [],
        aceptacion=aceptacion,
        aceptado_en=datetime.now(UTC) if aceptacion else None,
    )
    async with tenant_scope(vendedor):
        async with get_session() as session:
            registrar_pedido_de_alta(session, pedido, referencia=referencia)


_SEQ_PLANES = itertools.count(1)


async def plan_del_catalogo(
    tid: uuid.UUID, *, plan: str | None = "premium", precio: int = 600_000, periodicidad: str = "mensual"
) -> uuid.UUID:
    """Plan del catálogo de `pack_subscriptions` que vende un plan de la plataforma (SPEC-43).

    D-16: solo un plan de $0 tiene prueba (30 días). `plan=None` es un plan propio del vendedor, sin
    plan de plataforma. Cada llamada crea uno nuevo con un código único."""
    from decimal import Decimal

    from pack_subscriptions.planes import DatosDePlan, crear

    from core.shared.tenant import tenant_scope

    n = next(_SEQ_PLANES)
    async with tenant_scope(tid):
        creado = await crear(
            DatosDePlan(
                codigo=f"{(plan or 'propio').upper()}-{n}-{uuid.uuid4().hex[:4].upper()}",
                nombre=f"Plan {(plan or 'propio').title()}",
                precio=Decimal(precio),
                periodicidad=periodicidad,
                trial_dias=30 if precio == 0 else 0,
                plan_plataforma=plan,
            )
        )
    return creado.id


async def suscripcion_de_plan(
    tid: uuid.UUID,
    *,
    plan: str | None = "premium",
    precio: int = 600_000,
    datos_alta: dict[str, Any] | None = None,
    customer_id: uuid.UUID | None = None,
    pagado: bool = True,
    activar: bool = True,
    aceptar: bool = True,
    origen: str = "agente",
    plan_id: uuid.UUID | None = None,
    telefono: str | None = None,
    documento: str | None = None,
    senales: list[str] | None = None,
) -> tuple[str, uuid.UUID]:
    """Inscripción de un plan de la plataforma armada a mano (sin la tool); `activar` emite
    `subscription.activated`. Devuelve (referencia = `numero_publico`, cliente).

    Es el equivalente de lo que antes era un pedido de commerce activado: el core da de alta el negocio
    leyendo la inscripción por su referencia. `plan_id` usa un plan ya creado; `pagado=False` la deja `pendiente_pago`; `aceptar` registra
    la aceptación de términos como lo hace `inscribir` (HU-SH-07)."""
    from datetime import UTC, datetime, timedelta

    from pack_subscriptions.capability import _emitir_activada
    from pack_subscriptions.inscripcion import _siguiente_numero
    from pack_subscriptions.models import Plan, Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    plan_id = plan_id or await plan_del_catalogo(tid, plan=plan, precio=precio)
    customer_id = customer_id or uuid.uuid4()
    ahora = datetime.now(UTC)
    async with tenant_scope(tid):
        async with get_session() as s:
            catalogo = (await s.execute(select(Plan).where(Plan.id == plan_id))).scalar_one()
            precio = int(catalogo.precio)  # con `plan_id` el precio es el del catálogo
            vigente = bool(pagado)
            estado = ("trialing" if precio == 0 else "activa") if vigente else "pendiente_pago"
            sub = Subscription(
                tenant_id=tid,
                customer_id=customer_id,
                plan_id=catalogo.id,
                numero_publico=await _siguiente_numero(s, "SUB"),
                origen=origen,
                datos_alta=datos_alta,
                concepto=catalogo.nombre,
                estado=estado,
                valor_periodo=catalogo.precio,
                moneda=catalogo.moneda,
                periodicidad=catalogo.periodicidad,
                dia_ancla=ahora.day,
                periodo_actual_inicio=ahora if vigente else None,
                trial_termina=(ahora + timedelta(days=30)) if (vigente and precio == 0) else None,
                proximo_cargo=(ahora + timedelta(days=30)) if vigente else None,
                fecha_alta=ahora if vigente else None,
            )
            s.add(sub)
            await s.flush()
            referencia = sub.numero_publico
            if activar and vigente:
                await _emitir_activada(s, sub, cambio=False)
    if aceptar:
        await aceptar_terminos(
            tid, referencia, customer_id=customer_id, gratis=precio == 0, telefono=telefono,
            documento=documento, senales=senales,
        )
    return referencia, customer_id


async def activar_inscripcion(tid: uuid.UUID, referencia: str) -> None:
    """Activa la inscripción `referencia` ya creada con `activar=False` (emite el evento)."""
    from pack_subscriptions.capability import _emitir_activada
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            sub = (await s.execute(select(Subscription).where(Subscription.numero_publico == referencia))).scalar_one()
            await _emitir_activada(s, sub, cambio=False)


async def corregir_datos_alta(tid: uuid.UUID, referencia: str, **cambios: Any) -> None:
    """Corrige los datos del comprador de la inscripción (lo que hace el master antes de reintentar)."""
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            sub = (await s.execute(select(Subscription).where(Subscription.numero_publico == referencia))).scalar_one()
            sub.datos_alta = {**(sub.datos_alta or {}), **cambios}
