"""Cron jobs de pack-cobranza.

- `marcar_mora`: pasa las cuotas vencidas a `atrasada` y actualiza el color de
  riesgo del cliente (blanco/azul/rojo). Corre de madrugada.
- `cierre_diario`: congela el reporte diario + snapshot de KPIs de cada cartera
  (idempotente por fecha). El mismo `snapshot_cartera` lo usa el botón del panel.

El decorador `@register_cron` salta automáticamente si el pack está inactivo.
El filtro por tenant habilitado es explícito (`_tenant_ids`, ADR-0019).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select, text, update

from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from pack_cobranza.models import (
    Abono,
    Cartera,
    CarteraApertura,
    CarteraEstadisticasSnapshot,
    Cliente,
    Cuota,
    Gasto,
    Pago,
    Prestamo,
    ReporteDiario,
    Visita,
)

log = get_logger(__name__)

PACK_ID = "pack_cobranza"


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants cuyo AgentSpec incluye este pack (ADR-0019)."""
    from core.agents.spec import enabled_packs, load_active_spec

    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null"))
        ).scalars().all()

    result: list[uuid.UUID] = []
    for raw in rows:
        tid = uuid.UUID(str(raw))
        try:
            async with tenant_scope(tid):
                spec = await load_active_spec()
            if PACK_ID in enabled_packs(spec):
                result.append(tid)
        except Exception as exc:
            log.warning(
                "cobranza_tenant_scope_failed", tenant_id=str(tid), error=type(exc).__name__
            )
            continue
    return result


@register_cron("pack_cobranza", "0 2 * * *")
async def marcar_mora() -> None:
    """Marca cuotas vencidas como `atrasada` y recalcula el color de riesgo del cliente."""
    from datetime import date

    hoy = date.today()
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                await session.execute(
                    update(Cuota)
                    .where(
                        Cuota.tenant_id == tenant_id,
                        Cuota.estatus == "pendiente",
                        Cuota.fecha_vencimiento < hoy,
                    )
                    .values(estatus="atrasada")
                )
                # Recalcula color de riesgo: rojo si tiene cuotas atrasadas.
                clientes = (
                    await session.execute(
                        select(Cliente).where(
                            Cliente.tenant_id == tenant_id, Cliente.deleted_at.is_(None)
                        )
                    )
                ).scalars().all()
                for cliente in clientes:
                    prestamos = (
                        await session.execute(
                            select(Prestamo.id).where(
                                Prestamo.tenant_id == tenant_id,
                                Prestamo.cliente_id == cliente.id,
                                Prestamo.estatus == "vigente",
                            )
                        )
                    ).scalars().all()
                    if not prestamos:
                        nuevo = "blanco"
                    else:
                        atrasadas = (
                            await session.execute(
                                select(Cuota.id).where(
                                    Cuota.tenant_id == tenant_id,
                                    Cuota.prestamo_id.in_(prestamos),
                                    Cuota.estatus == "atrasada",
                                )
                            )
                        ).scalars().all()
                        nuevo = "rojo" if atrasadas else "azul"
                    if cliente.color_riesgo != nuevo:
                        cliente.color_riesgo = nuevo
            log.info("cobranza_mora_actualizada", tenant_id=str(tenant_id))


async def _suma(session, stmt) -> Decimal:
    return Decimal(await session.scalar(stmt) or 0)


async def snapshot_cartera(session, tenant_id: uuid.UUID, cartera: Cartera, hoy: date | None = None) -> None:
    """Congela el reporte diario y las estadísticas de una cartera (idempotente por fecha).

    Reutilizado por el cron `cierre_diario` y por el botón "Generar cierre" del panel.
    """
    d = hoy or date.today()
    desde = datetime(d.year, d.month, d.day, tzinfo=UTC)
    tid = tenant_id
    prestamo_ids = (await session.execute(select(Prestamo.id).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera.id))).scalars().all()
    cobrado = await _suma(session, select(func.coalesce(func.sum(Pago.valor), 0)).where(Pago.tenant_id == tid, Pago.prestamo_id.in_(prestamo_ids), Pago.fecha_hora >= desde, Pago.deleted_at.is_(None)))
    cobrado += await _suma(session, select(func.coalesce(func.sum(Abono.valor), 0)).where(Abono.tenant_id == tid, Abono.prestamo_id.in_(prestamo_ids), Abono.fecha_hora >= desde, Abono.deleted_at.is_(None)))
    prestado = await _suma(session, select(func.coalesce(func.sum(Prestamo.valor), 0)).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera.id, Prestamo.fecha_otorgado == d, Prestamo.deleted_at.is_(None)))
    visitas = (await session.execute(select(Visita).where(Visita.tenant_id == tid, Visita.cartera_id == cartera.id, Visita.fecha == d, Visita.deleted_at.is_(None)))).scalars().all()
    apertura = (await session.execute(select(CarteraApertura).where(CarteraApertura.tenant_id == tid, CarteraApertura.cartera_id == cartera.id, CarteraApertura.fecha == d).order_by(CarteraApertura.created_at.desc()).limit(1))).scalar_one_or_none()
    # Idempotencia: reemplaza el snapshot previo del mismo día.
    await session.execute(delete(ReporteDiario).where(ReporteDiario.tenant_id == tid, ReporteDiario.cartera_id == cartera.id, ReporteDiario.fecha == d))
    await session.execute(delete(CarteraEstadisticasSnapshot).where(CarteraEstadisticasSnapshot.tenant_id == tid, CarteraEstadisticasSnapshot.cartera_id == cartera.id, CarteraEstadisticasSnapshot.fecha == d))
    session.add(
        ReporteDiario(
            tenant_id=tid, cartera_id=cartera.id, fecha=d, cobrado=cobrado, prestado=prestado,
            clientes_visitados=[str(v.cliente_id) for v in visitas],
            clientes_sin_pago=[str(v.cliente_id) for v in visitas if v.resultado != "pago"],
            hora_inicio=apertura.hora_inicio if apertura else None,
            hora_fin=datetime.now(UTC).strftime("%H:%M"),
        )
    )
    clientes = await session.scalar(select(func.count()).select_from(Cliente).where(Cliente.tenant_id == tid, Cliente.cartera_id == cartera.id, Cliente.deleted_at.is_(None)))
    vigentes = await session.scalar(select(func.count()).select_from(Prestamo).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera.id, Prestamo.estatus == "vigente", Prestamo.deleted_at.is_(None)))
    saldo = await _suma(session, select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(Cuota.tenant_id == tid, Cuota.prestamo_id.in_(prestamo_ids), Cuota.estatus.in_(("pendiente", "atrasada")), Cuota.deleted_at.is_(None)))
    mora = await _suma(session, select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(Cuota.tenant_id == tid, Cuota.prestamo_id.in_(prestamo_ids), Cuota.estatus == "atrasada", Cuota.deleted_at.is_(None)))
    gastos = await _suma(session, select(func.coalesce(func.sum(Gasto.valor), 0)).where(Gasto.tenant_id == tid, Gasto.cartera_id == cartera.id, Gasto.deleted_at.is_(None)))
    session.add(
        CarteraEstadisticasSnapshot(
            tenant_id=tid, cartera_id=cartera.id, fecha=d,
            datos={"clientes": int(clientes or 0), "prestamos_vigentes": int(vigentes or 0), "saldo": float(saldo), "mora": float(mora), "gastos": float(gastos), "cobrado_dia": float(cobrado)},
        )
    )


@register_cron("pack_cobranza", "55 23 * * *")
async def cierre_diario() -> None:
    """Congela reporte diario + estadísticas de cada cartera (idempotente por fecha)."""
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                carteras = (await session.execute(select(Cartera).where(Cartera.tenant_id == tenant_id, Cartera.deleted_at.is_(None)))).scalars().all()
                for cartera in carteras:
                    await snapshot_cartera(session, tenant_id, cartera)
            log.info("cobranza_cierre_diario", tenant_id=str(tenant_id))
