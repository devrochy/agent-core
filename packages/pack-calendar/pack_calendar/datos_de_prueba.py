"""Datos de prueba del agente de agenda (HU-QA-10, ADR-0146): tipos de cita y citas próximas.

El esquema valida lo que genera el LLM (dato no confiable): topes de cantidad, duración y días,
hora entre las 7 y las 19, y sin datos de contacto. La zona horaria es la del negocio, no la del
modelo. El respaldo es el fragmento `qa`. Retirar es borrado lógico (`deleted_at`).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

from core.db.models import utcnow
from core.db.session import get_session
from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
from core.shared.tenant import tenant_scope
from pack_calendar.models import Appointment, AppointmentType, CalendarBlock

_MODELOS: dict[str, Any] = {"tipo": AppointmentType, "cita": Appointment, "bloqueo": CalendarBlock}


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class TipoDeCitaDePrueba(_Estricto):
    name: str = Field(min_length=2, max_length=80, description="Servicio que se agenda.")
    duration_minutes: int = Field(ge=15, le=240)
    description: str = Field(default="", max_length=300)


class CitaDePrueba(_Estricto):
    tipo: str = Field(min_length=2, max_length=80, description="`name` de uno de los tipos.")
    en_dias: int = Field(ge=1, le=14, description="Días desde hoy.")
    hora: str = Field(pattern=r"^(0[7-9]|1[0-8]):(00|30)$", description="HH:MM entre 07:00 y 18:30.")
    title: str = Field(min_length=2, max_length=120, description="Servicio y nombre ficticio del cliente.")


class AgendaDePrueba(_Estricto):
    tipos: list[TipoDeCitaDePrueba] = Field(min_length=2, max_length=6)
    citas: list[CitaDePrueba] = Field(default_factory=list, max_length=5)


def formas() -> list[FormaDeDatos]:
    from core.presets import load_fragment

    return [
        FormaDeDatos(
            modo="agenda",
            titulo="Agenda",
            habilidad="pack_calendar",
            prioridad=10,
            esquema=AgendaDePrueba,
            respaldo=load_fragment("pack_calendar", "qa").seed,
        )
    ]


async def sembrar(
    tenant_id: uuid.UUID, modo: str, datos: BaseModel, contexto: ContextoDeNegocio
) -> list[RegistroSembrado]:
    from pack_calendar.seed import sembrar_agenda

    assert isinstance(datos, AgendaDePrueba)
    data = {"timezone": contexto.zona_horaria, **datos.model_dump()}
    creados: list[tuple[str, uuid.UUID]] = []
    await sembrar_agenda(tenant_id, data, creados=creados)
    return [RegistroSembrado(kind=k, ref=r) for k, r in creados]


async def retirar(tenant_id: uuid.UUID, registros: list[RegistroSembrado]) -> int:
    total = 0
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            # Primero las citas: un tipo retirado no deja citas vivas colgando de él.
            for kind in ("cita", "bloqueo", "tipo"):
                ids = [r.ref for r in registros if r.kind == kind]
                if not ids:
                    continue
                modelo = _MODELOS[kind]
                res = await session.execute(
                    update(modelo)
                    .where(modelo.tenant_id == tenant_id, modelo.id.in_(ids), modelo.deleted_at.is_(None))
                    .values(deleted_at=utcnow())
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
    return total
