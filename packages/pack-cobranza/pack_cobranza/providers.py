"""Contribución del pack a la vista global de leads (ADR-0056, tanda 3).

Aporta las columnas de cartera (cartera, saldo, mora, riesgo, cuotas vencidas, próxima cuota,
promesa y último pago) al lead global del core. La data se agrupa por cliente de cobranza
(`Cliente.customer_id`), no por préstamo.

Desde la ficha se editan el riesgo, el tope de deuda y el estatus del cliente de cartera
(`LeadEdicion`, ADR-0149), con una fila de auditoría por cambio. El resto de los datos de
contacto son del perfil del core (ADR-0153).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Select, select

from core.admin.lead_registry import (
    LeadCampoEditable,
    LeadColumn,
    LeadEdicion,
    LeadRelacion,
    lead_registry,
)
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_cobranza import ficha as _ficha
from pack_cobranza.models import AuditoriaCartera, Cartera, Cliente, Cuota, Pago, Prestamo, Promesa

PLUGIN_ID = "pack_cobranza"

RIESGO = (("blanco", "Blanco"), ("azul", "Azul"), ("rojo", "Rojo"))
ESTATUS = (("activo", "Activo"), ("inactivo", "Inactivo"))
_RIESGO_LABEL = dict(RIESGO)
_CODIGOS_RIESGO = {v for v, _ in RIESGO}
_CODIGOS_ESTATUS = {v for v, _ in ESTATUS}
_MAX_TOPE = Decimal("999999999999")


def _pesos(valor: Any) -> str:
    return f"${float(valor or 0):,.0f}".replace(",", ".")


def _fecha(valor: date | None) -> str:
    return valor.isoformat() if valor else "—"


async def _con_datos(ids: list[uuid.UUID] | None) -> Select[Any]:
    """Clientes enlazados a un cliente de cartera no borrado."""
    q = select(Cliente.customer_id).where(
        Cliente.tenant_id == get_current_tenant_id(),
        Cliente.customer_id.is_not(None),
        Cliente.deleted_at.is_(None),
    )
    if ids is not None:
        q = q.where(Cliente.customer_id.in_(ids))
    return q.distinct()


async def _resolver(customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not customer_ids:
        return {}
    tid = get_current_tenant_id()
    async with get_session() as session:
        clientes = (
            await session.execute(
                select(Cliente).where(
                    Cliente.tenant_id == tid,
                    Cliente.customer_id.in_(customer_ids),
                    Cliente.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not clientes:
            return {}

        cartera_ids = {c.cartera_id for c in clientes}
        carteras = dict(
            (
                await session.execute(
                    select(Cartera.id, Cartera.nombre).where(
                        Cartera.tenant_id == tid, Cartera.id.in_(cartera_ids)
                    )
                )
            ).all()
        )

        cliente_ids = [c.id for c in clientes]
        prestamos = (
            await session.execute(
                select(Prestamo.id, Prestamo.cliente_id).where(
                    Prestamo.tenant_id == tid,
                    Prestamo.cliente_id.in_(cliente_ids),
                    Prestamo.estatus == "vigente",
                )
            )
        ).all()
        prestamos_de: dict[uuid.UUID, list[uuid.UUID]] = {}
        for prestamo_id, cliente_id in prestamos:
            prestamos_de.setdefault(cliente_id, []).append(prestamo_id)
        todos = [pid for ids in prestamos_de.values() for pid in ids]
        cuotas = (
            (
                await session.execute(
                    select(Cuota).where(
                        Cuota.tenant_id == tid,
                        Cuota.prestamo_id.in_(todos),
                        Cuota.estatus.in_(("pendiente", "atrasada")),
                    )
                )
            )
            .scalars()
            .all()
        ) if todos else []
        cuotas_de: dict[uuid.UUID, list[Cuota]] = {}
        for cuota in cuotas:
            if cuota.prestamo_id is not None:
                cuotas_de.setdefault(cuota.prestamo_id, []).append(cuota)

        promesas = (
            (
                await session.execute(
                    select(Promesa)
                    .where(
                        Promesa.tenant_id == tid,
                        Promesa.cliente_id.in_(cliente_ids),
                        Promesa.estado == "pendiente",
                        Promesa.deleted_at.is_(None),
                    )
                    .order_by(Promesa.fecha_prometida, Promesa.id)
                )
            )
            .scalars()
            .all()
        )
        promesa_de: dict[uuid.UUID, Promesa] = {}
        for promesa in promesas:
            promesa_de.setdefault(promesa.cliente_id, promesa)

        pagos = (
            await session.execute(
                select(Pago.cliente_id, Pago.valor, Pago.fecha_hora)
                .where(
                    Pago.tenant_id == tid,
                    Pago.cliente_id.in_(cliente_ids),
                    Pago.deleted_at.is_(None),
                )
                .order_by(Pago.cliente_id, Pago.fecha_hora.desc())
                .distinct(Pago.cliente_id)
            )
        ).all()
        pago_de = {fila.cliente_id: fila for fila in pagos}

    hoy = date.today()
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for cliente in clientes:
        if cliente.customer_id is None:
            continue
        abiertas = [cuota for pid in prestamos_de.get(cliente.id, []) for cuota in cuotas_de.get(pid, [])]
        saldo = sum((cuota.valor_esperado for cuota in abiertas), Decimal(0))
        vencidas = [cuota for cuota in abiertas if cuota.fecha_vencimiento < hoy]
        mora = max(((hoy - cuota.fecha_vencimiento).days for cuota in vencidas), default=0)
        proxima = min(abiertas, key=lambda c: (c.fecha_vencimiento, str(c.id)), default=None)
        promesa = promesa_de.get(cliente.id)
        pago = pago_de.get(cliente.id)
        out[cliente.customer_id] = {
            "cartera": carteras.get(cliente.cartera_id, "—"),
            "saldo": float(saldo),
            "mora": mora,
            "riesgo": _RIESGO_LABEL.get(cliente.color_riesgo, cliente.color_riesgo),
            "estatus": dict(ESTATUS).get(cliente.estatus, cliente.estatus),
            "cuotas_vencidas": len(vencidas),
            "proxima_cuota": (
                f"{_fecha(proxima.fecha_vencimiento)} · {_pesos(proxima.valor_esperado)}"
                if proxima is not None
                else None
            ),
            "promesa": (
                f"{_fecha(promesa.fecha_prometida)} · {_pesos(promesa.valor_prometido)}"
                if promesa is not None
                else None
            ),
            "ultimo_pago": (
                f"{pago.fecha_hora.strftime('%Y-%m-%d')} · {_pesos(pago.valor)}"
                if pago is not None
                else None
            ),
        }
    return out


async def _leer_cliente(customer_id: uuid.UUID) -> dict[str, Any]:
    tid = get_current_tenant_id()
    async with get_session() as session:
        cliente = (
            await session.execute(
                select(Cliente).where(
                    Cliente.tenant_id == tid,
                    Cliente.customer_id == customer_id,
                    Cliente.deleted_at.is_(None),
                )
            )
        ).scalars().first()
    if cliente is None:
        return {}
    return {
        "color_riesgo": cliente.color_riesgo,
        "tope_maximo_deuda": "" if cliente.tope_maximo_deuda is None else str(cliente.tope_maximo_deuda),
        "estatus": cliente.estatus,
    }


async def _escribir_cliente(customer_id: uuid.UUID, valores: dict[str, str]) -> None:
    """Cambia los campos de cobranza del cliente y deja una fila de auditoría por cambio."""
    tid = get_current_tenant_id()
    async with get_session() as session:
        cliente = (
            await session.execute(
                select(Cliente)
                .where(
                    Cliente.tenant_id == tid,
                    Cliente.customer_id == customer_id,
                    Cliente.deleted_at.is_(None),
                )
                .with_for_update(key_share=True)
            )
        ).scalars().first()
        if cliente is None:
            return

        cambios: dict[str, tuple[Any, Any]] = {}
        if "color_riesgo" in valores:
            nuevo = valores["color_riesgo"].strip()
            if nuevo not in _CODIGOS_RIESGO:
                raise ValueError("Elige un riesgo de la lista.")
            if nuevo != cliente.color_riesgo:
                cambios["color_riesgo"] = (cliente.color_riesgo, nuevo)
                cliente.color_riesgo = nuevo
        if "estatus" in valores:
            nuevo = valores["estatus"].strip()
            if nuevo not in _CODIGOS_ESTATUS:
                raise ValueError("Elige un estatus de la lista.")
            if nuevo != cliente.estatus:
                cambios["estatus"] = (cliente.estatus, nuevo)
                cliente.estatus = nuevo
        if "tope_maximo_deuda" in valores:
            crudo = (
                valores["tope_maximo_deuda"].strip().replace("$", "").replace(" ", "").replace(",", ".")
            )
            try:
                nuevo_tope = Decimal(crudo) if crudo else None
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("El tope de deuda debe ser un número.") from exc
            if nuevo_tope is not None and (nuevo_tope < 0 or nuevo_tope > _MAX_TOPE):
                raise ValueError("El tope de deuda está fuera de rango.")
            if nuevo_tope != cliente.tope_maximo_deuda:
                cambios["tope_maximo_deuda"] = (
                    None if cliente.tope_maximo_deuda is None else str(cliente.tope_maximo_deuda),
                    None if nuevo_tope is None else str(nuevo_tope),
                )
                cliente.tope_maximo_deuda = nuevo_tope

        for campo, (antes, despues) in cambios.items():
            session.add(
                AuditoriaCartera(
                    tenant_id=tid,
                    entidad="cliente",
                    entidad_id=cliente.id,
                    operacion="editar",
                    valores_antes={campo: antes},
                    valores_despues={campo: despues},
                    actor_rol="humano",
                    motivo="ficha del cliente",
                )
            )


lead_registry.register(
    PLUGIN_ID,
    columns=[
        LeadColumn(key="cartera", label="Cartera"),
        LeadColumn(key="saldo", label="Saldo"),
        LeadColumn(key="mora", label="Mora (días)"),
        LeadColumn(key="riesgo", label="Riesgo"),
        LeadColumn(key="estatus", label="Estatus"),
        LeadColumn(key="cuotas_vencidas", label="Cuotas vencidas"),
        LeadColumn(key="proxima_cuota", label="Próxima cuota"),
        LeadColumn(key="promesa", label="Promesa"),
        LeadColumn(key="ultimo_pago", label="Último pago"),
    ],
    resolver=_resolver,
    edicion=LeadEdicion(
        campos=(
            LeadCampoEditable(key="color_riesgo", label="Riesgo", tipo="opciones", options=RIESGO),
            LeadCampoEditable(
                key="tope_maximo_deuda",
                label="Tope de deuda (sin separadores)",
                tipo="texto",
                max_length=20,
            ),
            LeadCampoEditable(key="estatus", label="Estatus", tipo="opciones", options=ESTATUS),
        ),
        leer=_leer_cliente,
        escribir=_escribir_cliente,
    ),
    agent_key="cobranza",
    agent_name="Cobranza",
    icon="wallet",
    relacion=LeadRelacion(con_datos=_con_datos, ficha=_ficha.ficha, detalle=_ficha.detalle),
)


# Registra también los KPIs del módulo para el Resumen por agente (ADR-0171).
from pack_cobranza import resumen as _resumen  # noqa: E402, F401
