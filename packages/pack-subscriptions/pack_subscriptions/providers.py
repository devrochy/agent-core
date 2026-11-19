"""Contribución del pack a la vista global de Clientes (ADR-0056, SPEC-35 · HU-SUB-19).

Un cliente con suscripción se ve en la lista de Clientes con su plan, el estado, el próximo cobro
y lo que debe; se filtra por estado de suscripción y en su ficha aparece la historia de cobros.
No hay una segunda tabla de clientes: la pestaña «Clientes» de Suscripciones agrupa por cliente y
enlaza a esta misma ficha.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select

from core.admin.lead_registry import (
    Cursor,
    LeadColumn,
    LeadFilter,
    LeadRelacion,
    TimelineEvent,
    antes_del_cursor,
    lead_registry,
)
from core.db.models import Customer
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_subscriptions import datos as _datos  # noqa: F401  (acciones de la política de datos)
from pack_subscriptions import ficha as _ficha
from pack_subscriptions.models import Subscription, SubscriptionCharge

PLUGIN_ID = "pack_subscriptions"

ESTADOS = (
    ("pendiente_pago", "Pendiente de pago"),
    ("activa", "Activa"),
    ("trialing", "En prueba"),
    ("pausada", "Pausada"),
    ("vencida", "Vencida"),
    ("cancelada", "Cancelada"),
)
_ESTADO = dict(ESTADOS)
# «Con mora» no es un estado: es tener algún cobro vencido sin pagar. «Sin suscripción» filtra a los que no tienen ninguna.
OPCIONES_FILTRO = (*ESTADOS, ("mora", "Con mora"), ("ninguna", "Sin suscripción"))
_VIVAS = ("trialing", "activa", "pausada", "vencida")


def _pesos(valor: Any) -> str:
    return f"${float(valor or 0):,.0f}".replace(",", ".")


def _fecha(valor: datetime | None) -> str:
    return valor.date().isoformat() if valor else "—"


async def _filter_query(key: str, value: str) -> Select[Any] | None:
    if key != "suscripcion":
        return None
    tid = get_current_tenant_id()
    vivas = (Subscription.tenant_id == tid, Subscription.deleted_at.is_(None))
    if value == "ninguna":
        con_suscripcion = select(Subscription.customer_id).where(*vivas)
        return select(Customer.id).where(Customer.tenant_id == tid, Customer.id.not_in(con_suscripcion))
    if value == "mora":
        return (
            select(Subscription.customer_id)
            .join(SubscriptionCharge, SubscriptionCharge.subscription_id == Subscription.id)
            .where(
                *vivas,
                Subscription.estado != "cancelada",
                SubscriptionCharge.tenant_id == tid,
                SubscriptionCharge.deleted_at.is_(None),
                SubscriptionCharge.estado == "pendiente",
                SubscriptionCharge.tipo == "periodo",
                SubscriptionCharge.fecha_vencimiento < datetime.now(UTC),
            )
        )
    if value in _ESTADO:
        return select(Subscription.customer_id).where(*vivas, Subscription.estado == value)
    return None


async def _con_datos(ids: list[uuid.UUID] | None) -> Select[Any]:
    """Clientes con alguna suscripción no borrada, en cualquier estado."""
    q = select(Subscription.customer_id).where(
        Subscription.tenant_id == get_current_tenant_id(), Subscription.deleted_at.is_(None)
    )
    if ids is not None:
        q = q.where(Subscription.customer_id.in_(ids))
    return q.distinct()


async def _resolver(customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not customer_ids:
        return {}
    tid = get_current_tenant_id()
    ahora = datetime.now(UTC)
    async with get_session() as session:
        subs = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == tid,
                    Subscription.customer_id.in_(customer_ids),
                    Subscription.deleted_at.is_(None),
                )
                .order_by(Subscription.created_at.desc())
            )
        ).scalars().all()
        if not subs:
            return {}
        cargos = (
            await session.execute(
                select(SubscriptionCharge.subscription_id, SubscriptionCharge.monto)
                .where(
                    SubscriptionCharge.tenant_id == tid,
                    SubscriptionCharge.subscription_id.in_([s.id for s in subs]),
                    SubscriptionCharge.estado == "pendiente",
                    SubscriptionCharge.tipo == "periodo",
                    SubscriptionCharge.deleted_at.is_(None),
                )
            )
        ).all()
        vencidos = {
            sid
            for sid, in (
                await session.execute(
                    select(SubscriptionCharge.subscription_id)
                    .where(
                        SubscriptionCharge.tenant_id == tid,
                        SubscriptionCharge.subscription_id.in_([s.id for s in subs]),
                        SubscriptionCharge.estado == "pendiente",
                        SubscriptionCharge.tipo == "periodo",
                        SubscriptionCharge.deleted_at.is_(None),
                        SubscriptionCharge.fecha_vencimiento < ahora,
                    )
                    .distinct()
                )
            ).all()
        }
    deuda_de: dict[uuid.UUID, float] = {}
    for sid, monto in cargos:
        deuda_de[sid] = deuda_de.get(sid, 0.0) + float(monto or 0)
    por_cliente: dict[uuid.UUID, list[Subscription]] = {}
    for sub in subs:
        por_cliente.setdefault(sub.customer_id, []).append(sub)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for cid, lista in por_cliente.items():
        # Se muestra la más reciente que siga viva; si ninguna, la última (cancelada).
        viva = [s for s in lista if s.estado in _VIVAS]
        principal = (viva or lista)[0]
        otras = len(viva) - 1 if viva else 0
        deuda = sum(deuda_de.get(s.id, 0.0) for s in lista if s.estado != "cancelada")
        en_mora = any(s.id in vencidos for s in lista if s.estado != "cancelada")
        estado = "Con mora" if en_mora else _ESTADO.get(principal.estado, principal.estado)
        out[cid] = {
            "sub_plan": principal.concepto + (f" (+{otras})" if otras > 0 else ""),
            "sub_estado": estado,
            "sub_proximo_cargo": (
                f"{_fecha(principal.proximo_cargo)} · {_pesos(principal.valor_periodo)}"
                if principal.proximo_cargo
                else None
            ),
            "sub_deuda": _pesos(deuda) if deuda else None,
        }
    return out


async def _timeline(customer_id: uuid.UUID, cursor: Cursor | None, limit: int) -> list[TimelineEvent]:
    """Altas, cobros y bajas de las suscripciones del cliente (HU-CRM-04)."""
    tid = get_current_tenant_id()
    eventos: list[TimelineEvent] = []
    async with get_session() as session:
        altas = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == tid,
                    Subscription.customer_id == customer_id,
                    Subscription.deleted_at.is_(None),
                    Subscription.fecha_alta.is_not(None),
                    antes_del_cursor(Subscription.fecha_alta, Subscription.id, "pack_subscriptions.alta", cursor),
                )
                .order_by(Subscription.fecha_alta.desc(), Subscription.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for s in altas:
            assert s.fecha_alta is not None
            eventos.append(
                TimelineEvent(
                    at=s.fecha_alta, source="pack_subscriptions.alta", id=str(s.id), kind="cobro",
                    title=f"Suscripción a «{s.concepto}»", detail=f"{_pesos(s.valor_periodo)} {s.periodicidad}",
                )
            )
        bajas = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == tid,
                    Subscription.customer_id == customer_id,
                    Subscription.deleted_at.is_(None),
                    Subscription.fecha_cancelacion.is_not(None),
                    antes_del_cursor(
                        Subscription.fecha_cancelacion, Subscription.id, "pack_subscriptions.baja", cursor
                    ),
                )
                .order_by(Subscription.fecha_cancelacion.desc(), Subscription.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for s in bajas:
            assert s.fecha_cancelacion is not None
            eventos.append(
                TimelineEvent(
                    at=s.fecha_cancelacion, source="pack_subscriptions.baja", id=str(s.id), kind="cobro",
                    title=f"Suscripción cancelada: «{s.concepto}»",
                )
            )
        cargos = (
            await session.execute(
                select(SubscriptionCharge, Subscription.concepto)
                .join(Subscription, Subscription.id == SubscriptionCharge.subscription_id)
                .where(
                    SubscriptionCharge.tenant_id == tid,
                    Subscription.tenant_id == tid,
                    Subscription.customer_id == customer_id,
                    SubscriptionCharge.deleted_at.is_(None),
                    antes_del_cursor(
                        SubscriptionCharge.created_at, SubscriptionCharge.id, "pack_subscriptions.cobro", cursor
                    ),
                )
                .order_by(SubscriptionCharge.created_at.desc(), SubscriptionCharge.id.desc())
                .limit(limit)
            )
        ).all()
        for cargo, concepto in cargos:
            eventos.append(
                TimelineEvent(
                    at=cargo.created_at, source="pack_subscriptions.cobro", id=str(cargo.id), kind="cobro",
                    title=f"Cobro {cargo.periodo} de «{concepto}» por {_pesos(cargo.monto)}",
                    detail=cargo.estado,
                )
            )
    return eventos


lead_registry.register(
    PLUGIN_ID,
    columns=[
        LeadColumn(key="sub_plan", label="Plan"),
        LeadColumn(key="sub_estado", label="Suscripción"),
        LeadColumn(key="sub_proximo_cargo", label="Próximo cobro"),
        LeadColumn(key="sub_deuda", label="Debe"),
    ],
    resolver=_resolver,
    filters=[LeadFilter(key="suscripcion", label="Suscripción", options=OPCIONES_FILTRO)],
    filter_query=_filter_query,
    timeline=_timeline,
    agent_key="suscripciones",
    agent_name="Suscripciones",
    icon="repeat",
    relacion=LeadRelacion(con_datos=_con_datos, ficha=_ficha.ficha, detalle=_ficha.detalle),
)


# Registra también los KPIs del módulo para el Resumen por agente (ADR-0171).
from pack_subscriptions import resumen as _resumen  # noqa: E402, F401
