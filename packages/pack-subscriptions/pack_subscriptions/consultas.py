"""Consultas de lectura de la administración de suscripciones (SPEC-35).

Todas filtran por `tenant_id` de forma explícita además de RLS (defensa en profundidad) y no
traen el catálogo de commerce: el plan es el `concepto` de la suscripción (invariante nº5).

Definiciones que la UI muestra:
- **deuda**: suma de los cargos `pendiente` de tipo `periodo` (vencidos o por vencer).
- **en mora**: la parte de la deuda cuyo `fecha_vencimiento` ya pasó; **días de mora**, los del
  cargo vencido más viejo. Un cargo sin vencimiento (anterior a HU-SUB-04) no cuenta como mora.
- **MRR**: suscripciones `activa` con valor > 0, normalizadas por periodicidad
  (trimestral ÷ 3, anual ÷ 12). Las `trialing` y los planes de $0 no suman.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.api.pagination import PageParams
from core.db.models import AuditLog, Customer
from pack_subscriptions.esquemas import (
    CargoItem,
    ClienteConSuscripcion,
    ClienteRef,
    CohorteFila,
    HistorialItem,
    Metricas,
    MoraResumen,
    SuscripcionDetalle,
    SuscripcionItem,
    TramoDeMora,
)
from pack_subscriptions.models import Subscription, SubscriptionCharge

# `pendiente_pago` (SPEC-43) es una inscripción que espera su primer pago: NO es una suscripción viva, no
# suma a MRR, altas, churn, cohortes ni mora, y los crons no la tocan. Solo se puede cancelar.
ESTADOS = ("pendiente_pago", "trialing", "activa", "pausada", "vencida", "cancelada")
VIVAS = ("trialing", "activa", "pausada", "vencida")
ESTADOS_CARGO = ("pendiente", "pagado", "anulado")
TIPOS_CARGO = ("periodo", "cambio_plan", "inscripcion")
PERIODICIDADES = ("mensual", "trimestral", "anual")
ACCIONES_DEL_HISTORIAL = (
    "subs_pausada",
    "subs_reanudada",
    "subs_cancelada",
    "subs_pausada_por_mora",
    "subs_link_reenviado",
)
_MAX_CARGOS_DETALLE = 120
_MAX_HISTORIAL = 50

CargoRow = tuple[SubscriptionCharge, Subscription, Customer | None]


def acciones_validas(estado: str) -> list[str]:
    """Qué se puede hacer con una suscripción según su estado (la UI solo muestra estas)."""
    if estado in ("activa", "trialing", "vencida"):
        return ["pausar", "cancelar"]
    if estado == "pausada":
        return ["reanudar", "cancelar"]
    if estado == "pendiente_pago":
        return ["cancelar"]
    return []


def _comodin(texto: str) -> str:
    """Patrón `ilike` que trata `%`, `_` y `\\` del usuario como texto literal."""
    limpio = texto.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{limpio}%"


def _dias(hasta: datetime, desde: datetime | None) -> int | None:
    if desde is None:
        return None
    return max((hasta - desde).days, 0)


def _cliente(cid: uuid.UUID, nombre: str | None, telefono: str | None, canal: str | None) -> ClienteRef:
    return ClienteRef(id=cid, nombre=nombre, telefono=telefono, canal=canal)


def _pendientes(tid: uuid.UUID, ahora: datetime):  # type: ignore[no-untyped-def]
    """Subconsulta: por suscripción, lo pendiente de pago de sus cargos de período."""
    c = SubscriptionCharge
    vencido = c.fecha_vencimiento < ahora
    return (
        select(
            c.subscription_id.label("sid"),
            func.sum(c.monto).label("deuda"),
            func.count().label("n"),
            func.coalesce(func.sum(case((vencido, c.monto), else_=0)), 0).label("en_mora"),
            func.min(case((vencido, c.fecha_vencimiento))).label("vence_min"),
        )
        .where(c.tenant_id == tid, c.estado == "pendiente", c.tipo == "periodo", c.deleted_at.is_(None))
        .group_by(c.subscription_id)
        .subquery("pend")
    )


def _aplicar_filtros_suscripcion(
    stmt: Select[Any],
    pend: Any,
    *,
    estado: str | None,
    variante_id: uuid.UUID | None,
    periodicidad: str | None,
    con_mora: bool,
    q: str | None,
    cliente_id: uuid.UUID | None,
) -> Select[Any]:
    if estado:
        stmt = stmt.where(Subscription.estado == estado)
    if variante_id:
        stmt = stmt.where(Subscription.variante_id == variante_id)
    if periodicidad:
        stmt = stmt.where(Subscription.periodicidad == periodicidad)
    if con_mora:
        stmt = stmt.where(pend.c.en_mora > 0)
    if cliente_id:
        stmt = stmt.where(Subscription.customer_id == cliente_id)
    if q and q.strip():
        patron = _comodin(q)
        stmt = stmt.where(
            or_(
                Customer.name.ilike(patron, escape="\\"),
                Customer.contact_phone.ilike(patron, escape="\\"),
                Subscription.concepto.ilike(patron, escape="\\"),
            )
        )
    return stmt


def _item(sub: Subscription, nombre: Any, telefono: Any, canal: Any, deuda: Any, n: Any, en_mora: Any, vence_min: Any, ahora: datetime) -> dict[str, Any]:
    return {
        "id": sub.id,
        "cliente": _cliente(sub.customer_id, nombre, telefono, canal),
        "plan": sub.concepto,
        "variante_id": sub.variante_id,
        "estado": sub.estado,
        "motivo_pausa": sub.motivo_pausa,
        "periodicidad": sub.periodicidad,
        "valor_periodo": float(sub.valor_periodo or 0),
        "moneda": sub.moneda,
        "proximo_cargo": sub.proximo_cargo,
        "trial_termina": sub.trial_termina,
        "fecha_alta": sub.fecha_alta,
        "deuda": float(deuda or 0),
        "cargos_pendientes": int(n or 0),
        "en_mora": float(en_mora or 0),
        "dias_mora": _dias(ahora, vence_min),
    }


async def listar_suscripciones(
    session: AsyncSession,
    tid: uuid.UUID,
    params: PageParams,
    *,
    estado: str | None = None,
    variante_id: uuid.UUID | None = None,
    periodicidad: str | None = None,
    con_mora: bool = False,
    q: str | None = None,
    cliente_id: uuid.UUID | None = None,
) -> tuple[list[SuscripcionItem], int]:
    ahora = datetime.now(UTC)
    pend = _pendientes(tid, ahora)
    filtros = {
        "estado": estado,
        "variante_id": variante_id,
        "periodicidad": periodicidad,
        "con_mora": con_mora,
        "q": q,
        "cliente_id": cliente_id,
    }

    def base(*cols: Any) -> Select[Any]:
        stmt = (
            select(*cols)
            .select_from(Subscription)
            .outerjoin(Customer, and_(Customer.id == Subscription.customer_id, Customer.tenant_id == tid))
            .outerjoin(pend, pend.c.sid == Subscription.id)
            .where(Subscription.tenant_id == tid, Subscription.deleted_at.is_(None))
        )
        return _aplicar_filtros_suscripcion(stmt, pend, **filtros)

    total = (await session.execute(base(func.count(Subscription.id)))).scalar_one()
    filas = (
        await session.execute(
            base(
                Subscription,
                Customer.name,
                Customer.contact_phone,
                Customer.channel,
                pend.c.deuda,
                pend.c.n,
                pend.c.en_mora,
                pend.c.vence_min,
            )
            .order_by(Subscription.created_at.desc(), Subscription.id)
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).all()
    return [SuscripcionItem(**_item(*fila, ahora)) for fila in filas], int(total)


def _cargo_item(
    cargo: SubscriptionCharge,
    ahora: datetime,
    *,
    sub: Subscription | None = None,
    cliente: ClienteRef | None = None,
) -> CargoItem:
    vencido = cargo.estado == "pendiente" and cargo.fecha_vencimiento is not None and cargo.fecha_vencimiento < ahora
    return CargoItem(
        id=cargo.id,
        suscripcion_id=cargo.subscription_id,
        cliente=cliente,
        plan=sub.concepto if sub is not None else None,
        periodo=cargo.periodo,
        tipo=cargo.tipo,
        monto=float(cargo.monto or 0),
        moneda=sub.moneda if sub is not None else None,
        estado=cargo.estado,
        fecha_vencimiento=cargo.fecha_vencimiento,
        dias_mora=_dias(ahora, cargo.fecha_vencimiento) if vencido else None,
        link_url=cargo.link_url if cargo.estado == "pendiente" else None,
        pago_externo=cargo.pago_externo,
        creado=cargo.created_at,
    )


def _detalle_historial(accion: str, entrada: dict[str, Any] | None, salida: dict[str, Any] | None) -> str | None:
    entrada = entrada or {}
    salida = salida or {}
    if accion == "subs_pausada_por_mora":
        dias = salida.get("dias_mora")
        return f"Pausada por mora ({dias} días)" if dias is not None else "Pausada por mora"
    partes = []
    if salida.get("estado_antes") and salida.get("estado"):
        partes.append(f"{salida['estado_antes']} → {salida['estado']}")
    if entrada.get("motivo"):
        partes.append(f"Motivo: {entrada['motivo']}")
    if entrada.get("periodo"):
        partes.append(f"Período {entrada['periodo']}")
    return " · ".join(partes) or None


async def detalle_suscripcion(session: AsyncSession, tid: uuid.UUID, sid: uuid.UUID) -> SuscripcionDetalle | None:
    ahora = datetime.now(UTC)
    fila = (
        await session.execute(
            select(Subscription, Customer.name, Customer.contact_phone, Customer.channel)
            .outerjoin(Customer, and_(Customer.id == Subscription.customer_id, Customer.tenant_id == tid))
            .where(Subscription.tenant_id == tid, Subscription.id == sid, Subscription.deleted_at.is_(None))
        )
    ).first()
    if fila is None:
        return None
    sub, nombre, telefono, canal = fila
    cargos = (
        await session.execute(
            select(SubscriptionCharge)
            .where(
                SubscriptionCharge.tenant_id == tid,
                SubscriptionCharge.subscription_id == sid,
                SubscriptionCharge.deleted_at.is_(None),
            )
            .order_by(SubscriptionCharge.created_at.desc(), SubscriptionCharge.id)
            .limit(_MAX_CARGOS_DETALLE)
        )
    ).scalars().all()
    pendientes = [c for c in cargos if c.estado == "pendiente" and c.tipo == "periodo"]
    vencidos = [c for c in pendientes if c.fecha_vencimiento is not None and c.fecha_vencimiento < ahora]
    deuda = sum((c.monto for c in pendientes), Decimal(0))
    en_mora = sum((c.monto for c in vencidos), Decimal(0))
    vence_min = min((c.fecha_vencimiento for c in vencidos if c.fecha_vencimiento), default=None)

    sid_txt = str(sid)
    eventos = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tid,
                AuditLog.action.in_(ACCIONES_DEL_HISTORIAL),
                or_(
                    AuditLog.input["subscription_id"].astext == sid_txt,
                    AuditLog.output["subscription_id"].astext == sid_txt,
                ),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .limit(_MAX_HISTORIAL)
        )
    ).scalars().all()
    base = _item(sub, nombre, telefono, canal, deuda, len(pendientes), en_mora, vence_min, ahora)
    return SuscripcionDetalle(
        **base,
        fecha_cancelacion=sub.fecha_cancelacion,
        cambio_programado=sub.cambio_programado,
        cargos=[_cargo_item(c, ahora, sub=sub) for c in cargos],
        historial=[
            HistorialItem(
                at=e.created_at, accion=e.action, actor=e.actor, detalle=_detalle_historial(e.action, e.input, e.output)
            )
            for e in eventos
        ],
        acciones=acciones_validas(sub.estado),
    )


async def listar_cargos(
    session: AsyncSession,
    tid: uuid.UUID,
    params: PageParams,
    *,
    estado: str | None = None,
    tipo: str | None = None,
    suscripcion_id: uuid.UUID | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    vencidos: bool = False,
    q: str | None = None,
) -> tuple[list[CargoItem], int]:
    """Pagos de suscripción = cargos (D-SPEC-35-2)."""
    ahora = datetime.now(UTC)

    def base(*cols: Any) -> Select[Any]:
        stmt = (
            select(*cols)
            .select_from(SubscriptionCharge)
            .join(
                Subscription,
                and_(Subscription.id == SubscriptionCharge.subscription_id, Subscription.tenant_id == tid),
            )
            .outerjoin(Customer, and_(Customer.id == Subscription.customer_id, Customer.tenant_id == tid))
            .where(SubscriptionCharge.tenant_id == tid, SubscriptionCharge.deleted_at.is_(None), Subscription.deleted_at.is_(None))
        )
        if estado:
            stmt = stmt.where(SubscriptionCharge.estado == estado)
        if tipo:
            stmt = stmt.where(SubscriptionCharge.tipo == tipo)
        if suscripcion_id:
            stmt = stmt.where(SubscriptionCharge.subscription_id == suscripcion_id)
        if desde:
            stmt = stmt.where(SubscriptionCharge.fecha_vencimiento >= datetime.combine(desde, datetime.min.time(), UTC))
        if hasta:
            stmt = stmt.where(
                SubscriptionCharge.fecha_vencimiento < datetime.combine(hasta + timedelta(days=1), datetime.min.time(), UTC)
            )
        if vencidos:
            stmt = stmt.where(SubscriptionCharge.estado == "pendiente", SubscriptionCharge.fecha_vencimiento < ahora)
        if q and q.strip():
            patron = _comodin(q)
            stmt = stmt.where(
                or_(
                    Customer.name.ilike(patron, escape="\\"),
                    Customer.contact_phone.ilike(patron, escape="\\"),
                    Subscription.concepto.ilike(patron, escape="\\"),
                )
            )
        return stmt

    total = (await session.execute(base(func.count(SubscriptionCharge.id)))).scalar_one()
    filas = (
        await session.execute(
            base(SubscriptionCharge, Subscription, Customer.name, Customer.contact_phone, Customer.channel)
            .order_by(SubscriptionCharge.fecha_vencimiento.desc().nulls_last(), SubscriptionCharge.id)
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).all()
    items = [
        _cargo_item(cargo, ahora, sub=sub, cliente=_cliente(sub.customer_id, nombre, telefono, canal))
        for cargo, sub, nombre, telefono, canal in filas
    ]
    return items, int(total)


async def listar_clientes(
    session: AsyncSession,
    tid: uuid.UUID,
    params: PageParams,
    *,
    q: str | None = None,
    con_mora: bool = False,
    incluir_canceladas: bool = False,
) -> tuple[list[ClienteConSuscripcion], int]:
    """Clientes con al menos una suscripción (por defecto, una no cancelada)."""
    ahora = datetime.now(UTC)
    pend = _pendientes(tid, ahora)
    vivas = func.count(Subscription.id).filter(Subscription.estado.in_(VIVAS))
    agrupado = (
        select(
            Subscription.customer_id.label("cid"),
            func.count(Subscription.id).label("total"),
            vivas.label("vivas"),
            func.coalesce(func.sum(pend.c.deuda), 0).label("deuda"),
            func.coalesce(func.sum(pend.c.en_mora), 0).label("en_mora"),
            func.min(pend.c.vence_min).label("vence_min"),
        )
        .select_from(Subscription)
        .outerjoin(pend, pend.c.sid == Subscription.id)
        .where(Subscription.tenant_id == tid, Subscription.deleted_at.is_(None))
        .group_by(Subscription.customer_id)
    )
    if not incluir_canceladas:
        agrupado = agrupado.having(vivas > 0)
    if con_mora:
        agrupado = agrupado.having(func.coalesce(func.sum(pend.c.en_mora), 0) > 0)
    g = agrupado.subquery("g")

    def base(*cols: Any) -> Select[Any]:
        stmt = select(*cols).select_from(g).outerjoin(
            Customer, and_(Customer.id == g.c.cid, Customer.tenant_id == tid)
        )
        if q and q.strip():
            patron = _comodin(q)
            stmt = stmt.where(
                or_(Customer.name.ilike(patron, escape="\\"), Customer.contact_phone.ilike(patron, escape="\\"))
            )
        return stmt

    total = (await session.execute(base(func.count()))).scalar_one()
    filas = (
        await session.execute(
            base(g, Customer.name, Customer.contact_phone, Customer.channel)
            .order_by(g.c.en_mora.desc(), Customer.name.asc().nulls_last(), g.c.cid)
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).all()
    ids = [f.cid for f in filas]
    planes: dict[uuid.UUID, list[str]] = {}
    pagado: dict[uuid.UUID, str] = {}
    if ids:
        for cid, concepto in (
            await session.execute(
                select(Subscription.customer_id, Subscription.concepto)
                .where(
                    Subscription.tenant_id == tid,
                    Subscription.deleted_at.is_(None),
                    Subscription.customer_id.in_(ids),
                    Subscription.estado.in_(VIVAS),
                )
                .order_by(Subscription.created_at)
            )
        ).all():
            if concepto not in planes.setdefault(cid, []):
                planes[cid].append(concepto)
        pagado = dict(
            (
                await session.execute(
                    select(Subscription.customer_id, func.max(SubscriptionCharge.periodo))
                    .join(SubscriptionCharge, SubscriptionCharge.subscription_id == Subscription.id)
                    .where(
                        Subscription.tenant_id == tid,
                        SubscriptionCharge.tenant_id == tid,
                        Subscription.customer_id.in_(ids),
                        SubscriptionCharge.estado == "pagado",
                        SubscriptionCharge.tipo == "periodo",
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                    .group_by(Subscription.customer_id)
                )
            ).tuples().all()
        )
    items = [
        ClienteConSuscripcion(
            cliente=_cliente(f.cid, f.name, f.contact_phone, f.channel),
            suscripciones=int(f.total),
            vivas=int(f.vivas),
            planes=planes.get(f.cid, []),
            deuda=float(f.deuda or 0),
            en_mora=float(f.en_mora or 0),
            dias_mora=_dias(ahora, f.vence_min),
            ultimo_periodo_pagado=pagado.get(f.cid),
        )
        for f in filas
    ]
    return items, int(total)


# ----------------------------------------------------------------------------- métricas
def _mrr(valor: Any, periodicidad: Any) -> float:
    base = float(valor or 0)
    if periodicidad == "trimestral":
        return base / 3
    if periodicidad == "anual":
        return base / 12
    return base


def _inicio_de_mes(ahora: datetime, meses_atras: int = 0) -> datetime:
    mes = ahora.year * 12 + (ahora.month - 1) - meses_atras
    return datetime(mes // 12, mes % 12 + 1, 1, tzinfo=UTC)


_TRAMOS = (("Hasta 7 días", 0, 7), ("8 a 30 días", 8, 30), ("Más de 30 días", 31, 10**6))


async def metricas(session: AsyncSession, tid: uuid.UUID, ahora: datetime | None = None) -> Metricas:
    """MRR, altas, bajas, mora y cohortes del negocio (HU-SUB-16). Meses en UTC.

    Solo cuenta la habilidad Suscripciones (no cobranza, HU-SUB-15) y una moneda: la que más
    suscripciones vivas tiene; el resto se informa en `otras_monedas`.
    """
    ahora = ahora or datetime.now(UTC)
    s = Subscription
    inicio_mes = _inicio_de_mes(ahora)
    inicio_mes_ant = _inicio_de_mes(ahora, 1)
    cond = [s.tenant_id == tid, s.deleted_at.is_(None)]
    # Una inscripción sin pagar (o cancelada sin haber activado nunca) no es alta ni baja: solo cuenta
    # lo que llegó a tener `fecha_alta`.
    con_alta = [*cond, s.fecha_alta.is_not(None), s.estado != "pendiente_pago"]

    moneda_fila = (
        await session.execute(
            select(s.moneda, func.count()).where(*cond, s.estado.in_(VIVAS)).group_by(s.moneda).order_by(func.count().desc())
        )
    ).first()
    moneda = moneda_fila[0] if moneda_fila else "COP"

    conteo = dict((await session.execute(select(s.estado, func.count()).where(*cond).group_by(s.estado))).tuples().all())
    activas_pago = (
        await session.execute(
            select(s.valor_periodo, s.periodicidad).where(*cond, s.estado == "activa", s.moneda == moneda, s.valor_periodo > 0)
        )
    ).all()
    mrr = sum(_mrr(v, p) for v, p in activas_pago)
    otras = (
        await session.execute(select(func.count()).where(*cond, s.estado == "activa", s.moneda != moneda))
    ).scalar_one()

    # MRR del cierre del mes anterior (aproximado): vivas entonces, a su precio de hoy.
    vivas_al_cierre = (
        await session.execute(
            select(s.valor_periodo, s.periodicidad).where(
                *cond,
                s.moneda == moneda,
                s.valor_periodo > 0,
                s.fecha_alta < inicio_mes,
                or_(s.fecha_cancelacion.is_(None), s.fecha_cancelacion >= inicio_mes),
                s.estado.not_in(("trialing", "pendiente_pago")),
            )
        )
    ).all()
    mrr_anterior = sum(_mrr(v, p) for v, p in vivas_al_cierre)

    async def contar(columna: Any, desde: datetime, hasta: datetime) -> int:
        return int(
            (await session.execute(select(func.count()).where(*con_alta, columna >= desde, columna < hasta))).scalar_one()
        )

    altas_mes = await contar(s.fecha_alta, inicio_mes, ahora + timedelta(seconds=1))
    altas_ant = await contar(s.fecha_alta, inicio_mes_ant, inicio_mes)
    bajas_mes = await contar(s.fecha_cancelacion, inicio_mes, ahora + timedelta(seconds=1))
    bajas_ant = await contar(s.fecha_cancelacion, inicio_mes_ant, inicio_mes)

    vivas_al_inicio = int(
        (
            await session.execute(
                select(func.count()).where(
                    *con_alta,
                    s.fecha_alta < inicio_mes,
                    or_(s.fecha_cancelacion.is_(None), s.fecha_cancelacion >= inicio_mes),
                )
            )
        ).scalar_one()
    )
    churn = (bajas_mes / vivas_al_inicio) if vivas_al_inicio else None

    c = SubscriptionCharge
    vencidos = (
        await session.execute(
            select(c.monto, c.fecha_vencimiento)
            .join(s, s.id == c.subscription_id)
            .where(
                c.tenant_id == tid,
                s.tenant_id == tid,
                c.deleted_at.is_(None),
                s.deleted_at.is_(None),
                s.estado != "cancelada",
                c.estado == "pendiente",
                c.tipo == "periodo",
                c.fecha_vencimiento < ahora,
            )
        )
    ).all()
    tramos = []
    for etiqueta, minimo, maximo in _TRAMOS:
        del_tramo = [m for m, f in vencidos if minimo <= (ahora - f).days <= maximo]
        tramos.append(TramoDeMora(etiqueta=etiqueta, cargos=len(del_tramo), monto=float(sum(del_tramo, Decimal(0)))))
    mora = MoraResumen(
        cargos=len(vencidos), monto=float(sum((m for m, _ in vencidos), Decimal(0))), tramos=tramos
    )

    mes_alta = func.to_char(s.fecha_alta, "YYYY-MM")
    desde_cohortes = _inicio_de_mes(ahora, 5)
    cohortes_raw = (
        await session.execute(
            select(
                mes_alta.label("cohorte"),
                s.concepto,
                func.count().label("altas"),
                func.count().filter(s.estado != "cancelada").label("vigentes"),
            )
            .where(*con_alta, s.valor_periodo > 0, s.fecha_alta >= desde_cohortes)
            .group_by(mes_alta, s.concepto)
            .order_by(mes_alta, s.concepto)
        )
    ).all()
    return Metricas(
        moneda=moneda,
        otras_monedas=int(otras),
        mrr=round(mrr, 2),
        mrr_mes_anterior=round(mrr_anterior, 2),
        activas=int(conteo.get("activa", 0)),
        en_prueba=int(conteo.get("trialing", 0)),
        pendientes_de_pago=int(conteo.get("pendiente_pago", 0)),
        pausadas=int(conteo.get("pausada", 0)),
        vencidas=int(conteo.get("vencida", 0)),
        canceladas=int(conteo.get("cancelada", 0)),
        altas_mes=altas_mes,
        altas_mes_anterior=altas_ant,
        bajas_mes=bajas_mes,
        bajas_mes_anterior=bajas_ant,
        churn_mes=round(churn, 4) if churn is not None else None,
        mora=mora,
        cohortes=[
            CohorteFila(cohorte=r.cohorte, plan=r.concepto, altas=int(r.altas), vigentes=int(r.vigentes))
            for r in cohortes_raw
        ],
        desde=inicio_mes.date(),
    )
