"""Revisión de reportes de clientes por el equipo de SolversHub (SPEC-54, HU-FB-04, ADR-0213 §6-7).

Tres decisiones sobre un reporte de cualquier negocio: **tomar** (`recibido → en_revision`), **aprobar** con
un monto de créditos de regalo y **rechazar** con un motivo. Aprobado y rechazado son terminales.

- El negocio del reporte sale de la función `SECURITY DEFINER` `feedback_reporte_tenant` (solo devuelve el
  `tenant_id`); todo lo demás se lee y se escribe **dentro de `tenant_scope(negocio)`**, con la RLS puesta.
  Nunca se usa el negocio activo de la sesión del master.
- Cada decisión es una transición condicional (`UPDATE … WHERE estado IN (…) RETURNING`). Si no cambia nada,
  se relee la fila: la misma decisión repetida por el mismo revisor es un éxito (doble clic); cualquier otra,
  409.
- **Aprobar** corre en una sola transacción con el lock del saldo del negocio
  (`pg_advisory_xact_lock('billing:creditos:<tenant>')`, la clave de `core/billing/creditos.py`): ciclo y topes,
  transición, abono `bonificacion` (`acreditar_bonificacion`) y aviso al autor (`notify`, con SAVEPOINT: si
  falla, la aprobación sigue). Una transición que pasa sin que entre el abono es una inconsistencia: rollback,
  500 y alerta.
- Después del commit, de mejor esfuerzo: `record_audit`, el log y las métricas.

Los errores son `ErrorDeRevision` con status y código estables; el router los traduce a `ApiError`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from core.config import get_settings
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ESTADOS_ABIERTOS = ("recibido", "en_revision")
MOTIVOS_DE_RECHAZO = ("duplicado", "no_reproducible", "fuera_de_alcance", "informacion_insuficiente", "otro")

# Columnas que devuelve cada operación: todo lo que ve el master en el detalle, sin los adjuntos.
_COLUMNAS = (
    "id, tenant_id, autor_admin_id, tipo, area, titulo, descripcion, estado, revisado_por, tomado_en, "
    "decidido_en, motivo_codigo, motivo, nota_interna, creditos_otorgados, duplicado_de, created_at, updated_at"
)


class ErrorDeRevision(Exception):
    """Fallo de una decisión con status HTTP y código estables (el router lo pasa a `ApiError`)."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _no_encontrado() -> ErrorDeRevision:
    return ErrorDeRevision(404, "not_found", "No existe ese reporte.")


def _ya_decidido(estado: str) -> ErrorDeRevision:
    return ErrorDeRevision(
        409, "reporte_ya_decidido", "Ese reporte ya tiene una decisión.", details={"estado": estado}
    )


@dataclass(frozen=True, slots=True)
class Resultado:
    """La fila del reporte después de la operación y si fue una repetición idempotente (nada cambió)."""

    reporte: dict[str, Any]
    repetido: bool


# ------------------------------------------------------------------------------ lógica pura
def validar_monto(creditos: int, *, max_reporte: int, regalado_en_el_mes: int, max_mes: int) -> None:
    """Topes de ADR-0213 §4. 422 `monto_invalido`, `tope_reporte_excedido` o `tope_mensual_excedido`."""
    if isinstance(creditos, bool) or not isinstance(creditos, int) or creditos <= 0:
        raise ErrorDeRevision(422, "monto_invalido", "El monto debe ser un número entero de créditos mayor que 0.")
    if creditos > max_reporte:
        raise ErrorDeRevision(
            422,
            "tope_reporte_excedido",
            f"El máximo por reporte es {max_reporte} créditos.",
            details={"maximo": max_reporte},
        )
    if regalado_en_el_mes + creditos > max_mes:
        disponible = max(0, max_mes - regalado_en_el_mes)
        raise ErrorDeRevision(
            422,
            "tope_mensual_excedido",
            f"Este negocio ya recibió {regalado_en_el_mes} créditos de regalo en el ciclo; quedan {disponible}.",
            details={"maximo": max_mes, "regalado": regalado_en_el_mes, "disponible": disponible},
        )


def es_misma_aprobacion(reporte: dict[str, Any], revisor_id: uuid.UUID, creditos: int) -> bool:
    return (
        reporte["estado"] == "aprobado"
        and reporte["revisado_por"] == revisor_id
        and reporte["creditos_otorgados"] == creditos
    )


def es_mismo_rechazo(reporte: dict[str, Any], revisor_id: uuid.UUID, motivo_codigo: str) -> bool:
    return (
        reporte["estado"] == "rechazado"
        and reporte["revisado_por"] == revisor_id
        and reporte["motivo_codigo"] == motivo_codigo
    )


def segundos_hasta_decidir(reporte: dict[str, Any]) -> float:
    if reporte.get("decidido_en") is None or reporte.get("created_at") is None:
        return 0.0
    return max(0.0, (reporte["decidido_en"] - reporte["created_at"]).total_seconds())


# ---------------------------------------------------------------------------- acceso a datos
async def negocio_del_reporte(report_id: uuid.UUID) -> uuid.UUID | None:
    """`feedback_reporte_tenant` (SECURITY DEFINER, ADR-0213 §6): solo el `tenant_id`, o `None`."""
    async with tenant_scope(None):
        async with get_session() as session:
            valor = (
                await session.execute(text("SELECT feedback_reporte_tenant(:id)"), {"id": report_id})
            ).scalar_one_or_none()
    return uuid.UUID(str(valor)) if valor is not None else None


async def _negocio_o_404(report_id: uuid.UUID) -> uuid.UUID:
    tenant_id = await negocio_del_reporte(report_id)
    if tenant_id is None:
        raise _no_encontrado()
    return tenant_id


async def _leer(session, report_id: uuid.UUID) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    fila = (
        await session.execute(text(f"SELECT {_COLUMNAS} FROM feedback_reports WHERE id = :id"), {"id": report_id})
    ).mappings().first()
    return dict(fila) if fila is not None else None


async def _transicion(session, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    fila = (await session.execute(text(f"{sql} RETURNING {_COLUMNAS}"), params)).mappings().first()
    return dict(fila) if fila is not None else None


async def _auditar(revisor_email: str, accion: str, entrada: dict[str, Any], salida: dict[str, Any]) -> None:
    """Después del commit y dentro del `tenant_scope` del reporte: `record_audit` abre su propia sesión y
    toma el negocio del scope. Nunca lanza."""
    from core.shared.audit import record_audit

    await record_audit(f"admin:{revisor_email}", accion, input_data=entrada, output_data=salida)


# ------------------------------------------------------------------------------------ tomar
async def tomar(report_id: uuid.UUID, *, revisor_id: uuid.UUID, revisor_email: str) -> Resultado:
    """`recibido → en_revision`. El mismo revisor que repite: éxito sin cambios. Si otro master ya lo tomó,
    409 `reporte_ya_tomado`; si ya está decidido, 409 `reporte_ya_decidido`."""
    tenant_id = await _negocio_o_404(report_id)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            fila = await _transicion(
                session,
                "UPDATE feedback_reports SET estado = 'en_revision', revisado_por = :r, tomado_en = now() "
                "WHERE id = :id AND estado = 'recibido'",
                {"id": report_id, "r": revisor_id},
            )
            repetido = fila is None
            if fila is None:
                fila = await _leer(session, report_id)
                if fila is None:
                    raise _no_encontrado()
                if fila["estado"] != "en_revision":
                    raise _ya_decidido(fila["estado"])
                if fila["revisado_por"] != revisor_id:
                    raise ErrorDeRevision(409, "reporte_ya_tomado", "Otra persona del equipo ya está revisando este reporte.")
        if not repetido:
            log.info("feedback.tomado", report_id=str(report_id), tenant_id=str(tenant_id), revisor=str(revisor_id))
            await _auditar(revisor_email, "feedback.tomado", {"report_id": str(report_id)}, {"estado": "en_revision"})
    return Resultado(reporte=fila, repetido=repetido)


# ----------------------------------------------------------------------------------- aprobar
async def _ventana_del_ciclo(session, tenant_id: uuid.UUID, now: datetime) -> tuple[datetime, datetime] | None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from core.billing.periodos import ventana_mensual
    from core.db.models import TenantSubscription

    sub = (
        await session.execute(
            select(TenantSubscription.period_start, TenantSubscription.dia_ancla).where(
                TenantSubscription.tenant_id == tenant_id, TenantSubscription.deleted_at.is_(None)
            )
        )
    ).first()
    if sub is None or sub.period_start is None:
        return None
    return ventana_mensual(sub.period_start, sub.dia_ancla, now)


async def _regalado_en(session, tenant_id: uuid.UUID, inicio: datetime, fin: datetime) -> int:  # type: ignore[no-untyped-def]
    """Créditos de los reportes aprobados del negocio con `decidido_en` en la ventana (ADR-0213 §4). No se
    leen de `credit_grants`: el arrastre del cierre copia cada bonificación al ciclo siguiente con su mismo
    `origen`, y se contaría dos veces."""
    total = (
        await session.execute(
            text(
                "SELECT coalesce(sum(creditos_otorgados), 0) FROM feedback_reports "
                "WHERE tenant_id = :t AND estado = 'aprobado' AND decidido_en >= :ini AND decidido_en < :fin"
            ),
            {"t": tenant_id, "ini": inicio, "fin": fin},
        )
    ).scalar_one()
    return int(total)


class _AbonoInconsistente(Exception):
    """La transición a aprobado pasó y el abono no entró: se deshace todo."""

    def __init__(self, resultado: str) -> None:
        super().__init__(resultado)
        self.resultado = resultado


async def aprobar(
    report_id: uuid.UUID,
    *,
    revisor_id: uuid.UUID,
    revisor_email: str,
    creditos: int,
    now: datetime | None = None,
) -> Resultado:
    """Aprueba y acredita `creditos` de regalo al negocio del reporte, todo o nada (ADR-0213 §7)."""
    from core.billing.bonificacion import acreditar_bonificacion
    from core.notifications import catalog
    from core.notifications.service import notify

    settings = get_settings()
    momento = now or datetime.now(UTC)
    tenant_id = await _negocio_o_404(report_id)
    async with tenant_scope(tenant_id):
        try:
            async with get_session() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": f"billing:creditos:{tenant_id}"},
                )
                actual = await _leer(session, report_id)
                if actual is None:
                    raise _no_encontrado()
                if actual["estado"] not in ESTADOS_ABIERTOS:
                    # Doble clic: la misma aprobación ya está hecha. Nunca se vuelve a abonar.
                    if es_misma_aprobacion(actual, revisor_id, creditos):
                        return Resultado(reporte=actual, repetido=True)
                    raise _ya_decidido(actual["estado"])

                ventana = await _ventana_del_ciclo(session, tenant_id, momento)
                if ventana is None:
                    raise ErrorDeRevision(
                        409,
                        "sin_ciclo_de_creditos",
                        "El negocio no tiene suscripción con ciclo de créditos: no se le puede acreditar el regalo.",
                    )
                validar_monto(
                    creditos,
                    max_reporte=settings.feedback_creditos_max_reporte,
                    regalado_en_el_mes=await _regalado_en(session, tenant_id, *ventana),
                    max_mes=settings.feedback_creditos_max_mes_negocio,
                )

                fila = await _transicion(
                    session,
                    "UPDATE feedback_reports SET estado = 'aprobado', revisado_por = :r, decidido_en = :ahora, "
                    "creditos_otorgados = :c WHERE id = :id AND estado IN ('recibido', 'en_revision')",
                    {"id": report_id, "r": revisor_id, "c": creditos, "ahora": momento},
                )
                if fila is None:
                    # Lo decidió un rechazo concurrente (el rechazo no toma el lock del saldo).
                    otro = await _leer(session, report_id)
                    raise _ya_decidido(otro["estado"] if otro else "desconocido")

                abono = await acreditar_bonificacion(
                    session, tenant_id, f"feedback:{report_id}", creditos, now=momento
                )
                if abono != "acreditada":
                    raise _AbonoInconsistente(abono)

                await notify(session, tenant_id, catalog.feedback_aprobado(report_id, fila["autor_admin_id"], creditos))
        except _AbonoInconsistente as exc:
            from core.shared.alerts import send_alert

            log.error(
                "feedback.abono_inconsistente", report_id=str(report_id), tenant_id=str(tenant_id), resultado=exc.resultado
            )
            await send_alert(
                "Aprobación de reporte sin abono",
                f"Reporte {report_id} (negocio {tenant_id}): la transición pasó y el abono devolvió "
                f"`{exc.resultado}`. Se deshizo la aprobación.",
                severity="critical",
            )
            raise ErrorDeRevision(
                500, "abono_inconsistente", "No se pudo acreditar el regalo; no se aprobó el reporte."
            ) from None

        log.info(
            "feedback.aprobado",
            report_id=str(report_id),
            tenant_id=str(tenant_id),
            revisor=str(revisor_id),
            creditos=creditos,
        )
        from core.observability import metricas_negocio

        metricas_negocio.feedback_aprobado(fila["tipo"], creditos, segundos_hasta_decidir(fila))
        await _auditar(
            revisor_email,
            "feedback.aprobado",
            {"report_id": str(report_id), "creditos": creditos},
            {"estado": "aprobado", "origen": f"feedback:{report_id}"},
        )
    return Resultado(reporte=fila, repetido=False)


# ---------------------------------------------------------------------------------- rechazar
async def rechazar(
    report_id: uuid.UUID,
    *,
    revisor_id: uuid.UUID,
    revisor_email: str,
    motivo_codigo: str,
    motivo: str,
    nota_interna: str | None = None,
    duplicado_de: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Resultado:
    """Rechaza con un motivo visible al cliente. `duplicado_de` solo con `motivo_codigo = duplicado`, y debe
    ser otro reporte que exista (de cualquier negocio, validado con la función definer)."""
    from core.notifications import catalog
    from core.notifications.service import notify

    if motivo_codigo not in MOTIVOS_DE_RECHAZO:
        raise ErrorDeRevision(422, "motivo_invalido", "Ese motivo de rechazo no existe.")
    if motivo_codigo == "duplicado":
        if duplicado_de is None:
            raise ErrorDeRevision(422, "duplicado_sin_original", "Indica de qué reporte es duplicado.")
        if duplicado_de == report_id or await negocio_del_reporte(duplicado_de) is None:
            raise ErrorDeRevision(422, "duplicado_invalido", "El reporte original no existe.")
    elif duplicado_de is not None:
        raise ErrorDeRevision(422, "duplicado_sin_motivo", "Solo un rechazo por duplicado lleva el reporte original.")

    momento = now or datetime.now(UTC)
    tenant_id = await _negocio_o_404(report_id)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            fila = await _transicion(
                session,
                "UPDATE feedback_reports SET estado = 'rechazado', revisado_por = :r, decidido_en = :ahora, "
                "motivo_codigo = :mc, motivo = :m, nota_interna = :n, duplicado_de = :d "
                "WHERE id = :id AND estado IN ('recibido', 'en_revision')",
                {
                    "id": report_id,
                    "r": revisor_id,
                    "ahora": momento,
                    "mc": motivo_codigo,
                    "m": motivo,
                    "n": nota_interna,
                    "d": duplicado_de,
                },
            )
            if fila is None:
                actual = await _leer(session, report_id)
                if actual is None:
                    raise _no_encontrado()
                if es_mismo_rechazo(actual, revisor_id, motivo_codigo):
                    return Resultado(reporte=actual, repetido=True)
                raise _ya_decidido(actual["estado"])
            await notify(session, tenant_id, catalog.feedback_rechazado(report_id, fila["autor_admin_id"], motivo_codigo))

        log.info(
            "feedback.rechazado",
            report_id=str(report_id),
            tenant_id=str(tenant_id),
            revisor=str(revisor_id),
            motivo_codigo=motivo_codigo,
        )
        from core.observability import metricas_negocio

        metricas_negocio.feedback_rechazado(segundos_hasta_decidir(fila))
        await _auditar(
            revisor_email,
            "feedback.rechazado",
            {"report_id": str(report_id), "motivo_codigo": motivo_codigo, "duplicado_de": str(duplicado_de) if duplicado_de else None},
            {"estado": "rechazado"},
        )
    return Resultado(reporte=fila, repetido=False)


__all__ = [
    "ESTADOS_ABIERTOS",
    "MOTIVOS_DE_RECHAZO",
    "ErrorDeRevision",
    "Resultado",
    "aprobar",
    "es_misma_aprobacion",
    "es_mismo_rechazo",
    "negocio_del_reporte",
    "rechazar",
    "tomar",
    "validar_monto",
]
