"""Datos de prueba del agente de cobranza (HU-QA-10, ADR-0146): una cartera con clientes al día,
en mora y sin préstamo.

El esquema valida lo que genera el LLM (dato no confiable): topes de cantidad y montos, documento
solo con dígitos y **sin teléfono ni correo** (los descarta `extra="ignore"`): así ningún
recordatorio llega a un número real, como en `seed.py`. La cartera lleva el sufijo del lote en el
nombre para no mezclarse con una cartera del negocio que se llame igual. Sin gestor: el lote es
del agente, no del equipo. Retirar es borrado lógico (`deleted_at`) en orden inverso de FK.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

from core.db.models import utcnow
from core.db.session import get_session
from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
from core.shared.tenant import tenant_scope
from pack_cobranza.models import Cartera, Cliente, Cuota, Pago, Prestamo

# Orden de retiro: de las hojas a la raíz.
_ORDEN: tuple[tuple[str, Any], ...] = (
    ("pago", Pago),
    ("cuota", Cuota),
    ("prestamo", Prestamo),
    ("cliente", Cliente),
    ("cartera", Cartera),
)


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class PrestamoDePrueba(_Estricto):
    valor: Decimal = Field(gt=0, le=1_000_000_000)
    num_cuotas: int = Field(ge=1, le=30)
    tipo_interes: Decimal = Field(ge=0, le=50, description="Porcentaje total del préstamo.")
    dias_entre_cuotas: int = Field(ge=1, le=30)
    hace_dias: int = Field(ge=0, le=90, description="Días desde que se otorgó.")
    cuotas_pagadas: int = Field(ge=0, le=30)


class ClienteDePrueba(_Estricto):
    nombre: str = Field(min_length=2, max_length=60)
    apellido: str = Field(default="", max_length=60)
    numero_documento: str = Field(pattern=r"^[0-9]{6,12}$", description="Ficticio.")
    negocio: str = Field(default="", max_length=80)
    color_riesgo: Literal["blanco", "azul", "rojo"] = "blanco"
    prestamo: PrestamoDePrueba | None = Field(default=None, description="Sin préstamo: cliente nuevo.")


class CarteraDePrueba(_Estricto):
    nombre: str = Field(min_length=2, max_length=60)
    descripcion: str = Field(default="", max_length=200)
    tipo_interes: Decimal = Field(ge=0, le=50)
    num_cuotas: int = Field(ge=1, le=30)
    saldo_caja: Decimal = Field(ge=0, le=10_000_000_000)
    clientes: list[ClienteDePrueba] = Field(min_length=3, max_length=10)


class CobranzaDePrueba(_Estricto):
    carteras: list[CarteraDePrueba] = Field(min_length=1, max_length=1)


def formas() -> list[FormaDeDatos]:
    from core.presets import load_fragment

    return [
        FormaDeDatos(
            modo="cartera",
            titulo="Cartera",
            habilidad="pack_cobranza",
            prioridad=10,
            esquema=CobranzaDePrueba,
            respaldo=load_fragment("pack_cobranza", "qa").seed,
        )
    ]


async def sembrar(
    tenant_id: uuid.UUID, modo: str, datos: BaseModel, contexto: ContextoDeNegocio
) -> list[RegistroSembrado]:
    from pack_cobranza.seed import sembrar_cartera

    assert isinstance(datos, CobranzaDePrueba)
    carteras = []
    for cartera in datos.carteras:
        item = cartera.model_dump(mode="json")
        item["nombre"] = f"{cartera.nombre} · {contexto.sufijo}"
        item["moneda"] = contexto.moneda
        carteras.append(item)
    creados: list[tuple[str, uuid.UUID]] = []
    await sembrar_cartera(tenant_id, {"carteras": carteras}, creados=creados)
    return [RegistroSembrado(kind=k, ref=r) for k, r in creados]


async def retirar(tenant_id: uuid.UUID, registros: list[RegistroSembrado]) -> int:
    total = 0
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for kind, modelo in _ORDEN:
                ids = [r.ref for r in registros if r.kind == kind]
                if not ids:
                    continue
                res = await session.execute(
                    update(modelo)
                    .where(modelo.tenant_id == tenant_id, modelo.id.in_(ids), modelo.deleted_at.is_(None))
                    .values(deleted_at=utcnow())
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
    return total
