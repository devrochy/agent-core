"""Catálogo de planes del pack (SPEC-43, HU-SUB-27, ADR-0177).

El pack es dueño del plan: tabla `pack_subscriptions_plans` con código único por negocio (el SKU),
precio, periodicidad, prueba (solo $0, D-16), `entitlements` (objeto jsonb) y `plan_plataforma` (el
id en `core/billing/plans.py` cuando el plan vende un plan de la plataforma). Aquí viven las reglas
y el CRUD; los endpoints son `admin_api.py` y la inscripción es `inscripcion.py`.

Editar un plan NO toca las suscripciones ya inscritas: conservan su snapshot (`concepto`,
`valor_periodo`, `periodicidad`). Borrar es lógico (`deleted_at`) y las suscripciones vivas siguen.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.billing.plans import get_plan
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.subscriptions import PlanSnapshot
from pack_subscriptions.models import Plan
from pack_subscriptions.periodos import PERIODICIDADES

log = get_logger(__name__)

MAX_PRECIO = Decimal("999999999999.99")
_MAX_ENTITLEMENTS_BYTES = 4096


class PlanError(Exception):
    """Rechazo de negocio del catálogo, con código estable (la API lo traduce a 404, 409 o 422)."""

    def __init__(self, code: str, message: str, *, campo: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.campo = campo


@dataclass(frozen=True)
class DatosDePlan:
    """Lo que el dueño edita de un plan. El servidor valida; nada de esto es texto del LLM."""

    codigo: str
    nombre: str
    precio: Decimal
    periodicidad: str
    descripcion: str = ""
    moneda: str = "COP"
    trial_dias: int = 0
    entitlements: dict[str, Any] | None = None
    plan_plataforma: str | None = None
    activo: bool = True
    orden: int = 0


def _precio(valor: Any) -> Decimal:
    try:
        precio = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PlanError("precio_invalido", "El precio no es un número válido.", campo="precio") from exc
    if not precio.is_finite() or precio < 0 or precio > MAX_PRECIO:
        raise PlanError("precio_invalido", "El precio debe estar entre 0 y 999.999.999.999.", campo="precio")
    return precio.quantize(Decimal("0.01"))


def validar(datos: DatosDePlan) -> DatosDePlan:
    """Reglas de un plan (las mismas de los CHECK del esquema, con mensajes para el panel).

    D-16: solo un plan de $0 tiene prueba. `plan_plataforma` debe existir en el catálogo de la plataforma
    (`core/billing/plans.py`). Devuelve el plan normalizado o lanza `PlanError`."""
    codigo = datos.codigo.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", codigo):
        raise PlanError(
            "codigo_invalido",
            "El código lleva de 1 a 40 letras, números, guion o guion bajo, y empieza con letra o número.",
            campo="codigo",
        )
    nombre = datos.nombre.strip()
    if not 1 <= len(nombre) <= 120:
        raise PlanError("nombre_invalido", "El nombre lleva de 1 a 120 caracteres.", campo="nombre")
    if datos.periodicidad not in PERIODICIDADES:
        raise PlanError("periodicidad_invalida", "La periodicidad es mensual, trimestral o anual.", campo="periodicidad")
    precio = _precio(datos.precio)
    if not 0 <= int(datos.trial_dias) <= 365:
        raise PlanError("prueba_invalida", "Los días de prueba van de 0 a 365.", campo="trial_dias")
    if precio > 0 and int(datos.trial_dias) > 0:
        raise PlanError("prueba_en_plan_de_pago", "Solo el plan Free ($0) tiene días de prueba (D-16).", campo="trial_dias")
    moneda = (datos.moneda or "COP").strip().upper()
    if len(moneda) != 3 or not moneda.isalpha():
        raise PlanError("moneda_invalida", "La moneda es un código de 3 letras (p. ej. COP).", campo="moneda")
    entitlements = dict(datos.entitlements or {})
    if datos.entitlements is not None and not isinstance(datos.entitlements, dict):
        raise PlanError("entitlements_invalidos", "Lo que incluye el plan es un objeto, no una lista.", campo="entitlements")
    if len(json.dumps(entitlements, default=str)) > _MAX_ENTITLEMENTS_BYTES:
        raise PlanError("entitlements_invalidos", "Lo que incluye el plan es demasiado largo.", campo="entitlements")
    plataforma = (datos.plan_plataforma or "").strip().lower() or None
    if plataforma is not None and get_plan(plataforma) is None:
        raise PlanError("plan_plataforma_desconocido", "Ese plan de la plataforma no existe.", campo="plan_plataforma")
    return DatosDePlan(
        codigo=codigo,
        nombre=nombre,
        precio=precio,
        periodicidad=datos.periodicidad,
        descripcion=(datos.descripcion or "").strip()[:2000],
        moneda=moneda,
        trial_dias=int(datos.trial_dias),
        entitlements=entitlements,
        plan_plataforma=plataforma,
        activo=bool(datos.activo),
        orden=int(datos.orden),
    )


def snapshot(plan: Plan) -> PlanSnapshot:
    """El plan como lo ven las tools y el contrato: lo arma el servidor, nunca el LLM."""
    return {
        "plan_code": plan.codigo,
        "plan_id": str(plan.id),
        "nombre": plan.nombre,
        "descripcion": plan.descripcion or "",
        "precio": float(plan.precio or 0),
        "moneda": plan.moneda,
        "periodicidad": plan.periodicidad,
        "trial_dias": int(plan.trial_dias or 0),
        "entitlements": dict(plan.entitlements or {}),
    }


def _tenant() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("catálogo de planes sin tenant_scope")
    return tenant_id


async def listar(*, incluir_inactivos: bool = False) -> list[Plan]:
    """Planes vigentes del negocio, ordenados. Sin borrados; `incluir_inactivos` agrega los apagados."""
    filtros = [Plan.tenant_id == _tenant(), Plan.deleted_at.is_(None)]
    if not incluir_inactivos:
        filtros.append(Plan.activo.is_(True))
    async with get_session() as session:
        return list(
            (await session.execute(select(Plan).where(*filtros).order_by(Plan.orden, Plan.precio, Plan.codigo)))
            .scalars()
            .all()
        )


async def obtener(plan_id: uuid.UUID) -> Plan | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(Plan).where(Plan.tenant_id == _tenant(), Plan.id == plan_id, Plan.deleted_at.is_(None))
            )
        ).scalar_one_or_none()


async def por_codigo(codigo: str, *, solo_activos: bool = True) -> Plan | None:
    """Plan por código (sin distinguir mayúsculas), vigente y no borrado."""
    filtros = [
        Plan.tenant_id == _tenant(),
        func.lower(Plan.codigo) == (codigo or "").strip().lower(),
        Plan.deleted_at.is_(None),
    ]
    if solo_activos:
        filtros.append(Plan.activo.is_(True))
    async with get_session() as session:
        return (await session.execute(select(Plan).where(*filtros))).scalar_one_or_none()


def _aplicar(plan: Plan, datos: DatosDePlan) -> None:
    plan.codigo = datos.codigo
    plan.nombre = datos.nombre
    plan.descripcion = datos.descripcion
    plan.precio = datos.precio
    plan.moneda = datos.moneda
    plan.periodicidad = datos.periodicidad
    plan.trial_dias = datos.trial_dias
    plan.entitlements = datos.entitlements or {}
    plan.plan_plataforma = datos.plan_plataforma
    plan.activo = datos.activo
    plan.orden = datos.orden


def vista(plan: Plan) -> dict[str, Any]:
    """Foto del plan para la auditoría (antes y después)."""
    return {
        "id": str(plan.id),
        "codigo": plan.codigo,
        "nombre": plan.nombre,
        "precio": str(plan.precio),
        "moneda": plan.moneda,
        "periodicidad": plan.periodicidad,
        "trial_dias": plan.trial_dias,
        "plan_plataforma": plan.plan_plataforma,
        "activo": plan.activo,
        "orden": plan.orden,
    }


async def crear(datos: DatosDePlan, *, actor: str = "") -> Plan:
    """Crea un plan. 409 `codigo_duplicado` si ya hay uno vigente con ese código en el negocio."""
    datos = validar(datos)
    tenant = _tenant()
    plan = Plan(tenant_id=tenant)
    _aplicar(plan, datos)
    try:
        async with get_session() as session:
            session.add(plan)
            await session.flush()
            await _auditar(session, tenant, actor, "subs_plan_creado", None, plan)
    except IntegrityError as exc:
        if "uq_pack_subscriptions_plans_tenant_codigo" in str(exc):
            raise PlanError("codigo_duplicado", "Ya existe un plan con ese código.", campo="codigo") from exc
        raise
    log.info("subs_plan_creado", plan_id=str(plan.id), codigo=plan.codigo, plan_plataforma=plan.plan_plataforma)
    return plan


async def actualizar(plan_id: uuid.UUID, datos: DatosDePlan, *, actor: str = "") -> Plan:
    """Reemplaza los datos de un plan (el último gana; queda auditado el antes y el después).

    No toca las suscripciones ya inscritas: conservan su snapshot."""
    datos = validar(datos)
    tenant = _tenant()
    try:
        async with get_session() as session:
            plan = (
                await session.execute(
                    select(Plan)
                    .where(Plan.tenant_id == tenant, Plan.id == plan_id, Plan.deleted_at.is_(None))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if plan is None:
                raise PlanError("plan_no_encontrado", "El plan no existe.")
            antes = vista(plan)
            _aplicar(plan, datos)
            await session.flush()
            await _auditar(session, tenant, actor, "subs_plan_editado", antes, plan)
    except IntegrityError as exc:
        if "uq_pack_subscriptions_plans_tenant_codigo" in str(exc):
            raise PlanError("codigo_duplicado", "Ya existe un plan con ese código.", campo="codigo") from exc
        raise
    log.info("subs_plan_editado", plan_id=str(plan.id), codigo=plan.codigo)
    return plan


async def borrar(plan_id: uuid.UUID, *, actor: str = "") -> None:
    """Borrado lógico: deja de listarse para inscribir; las suscripciones vivas siguen cobrándose."""
    tenant = _tenant()
    async with get_session() as session:
        plan = (
            await session.execute(
                select(Plan)
                .where(Plan.tenant_id == tenant, Plan.id == plan_id, Plan.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if plan is None:
            raise PlanError("plan_no_encontrado", "El plan no existe.")
        antes = vista(plan)
        plan.deleted_at = datetime.now(UTC)
        plan.activo = False
        await session.flush()
        await _auditar(session, tenant, actor, "subs_plan_borrado", antes, plan)
    log.info("subs_plan_borrado", plan_id=str(plan_id))


async def _auditar(session, tenant: uuid.UUID, actor: str, accion: str, antes: dict | None, plan: Plan) -> None:  # type: ignore[no-untyped-def]
    """`audit_log` en la MISMA transacción que la mutación (quién y cuándo). Sin `actor` (siembra, cron)
    no se audita acá: la siembra audita por su lado."""
    if not actor:
        return
    from core.db.models import AuditLog

    session.add(
        AuditLog(
            tenant_id=tenant,
            actor=actor,
            action=accion,
            input={"plan_id": str(plan.id), "antes": antes},
            output=vista(plan),
        )
    )
