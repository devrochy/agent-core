"""Importar los tipos de cita que el dueño ya tiene desde un CSV (HU-QA-12, ADR-0180).

Plantilla `tipos_de_cita`: nombre, duración y descripción. La clave es el nombre (sin distinguir
mayúsculas): un tipo vivo con ese nombre ya existe y no se toca. Reusa `sembrar_agenda`, que además
informa lo que crea para poder retirarlo.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from core.db.session import get_session
from core.qa.contrato_datos import RegistroSembrado
from core.qa.contrato_importacion import (
    ColumnaDeImportacion,
    ContextoDeImportacion,
    FilaDeImportacion,
    PlantillaDeImportacion,
    ResultadoDeImportacion,
    entero,
)
from core.shared.tenant import tenant_scope
from pack_calendar.models import AppointmentType


class TipoDeCitaImportado(FilaDeImportacion):
    nombre: Annotated[str, Field(min_length=2, max_length=80)]
    duracion_minutos: Annotated[int, Field(ge=5, le=480)]
    descripcion: Annotated[str, Field(max_length=300)] = ""

    @field_validator("duracion_minutos", mode="before")
    @classmethod
    def _duracion(cls, valor: Any) -> int:
        return entero(valor)

    @field_validator("descripcion", mode="before")
    @classmethod
    def _sin_nulo(cls, valor: Any) -> str:
        return valor or ""


def plantillas() -> list[PlantillaDeImportacion]:
    return [
        PlantillaDeImportacion(
            id="tipos_de_cita",
            titulo="Tipos de cita",
            modo="agenda",
            columnas=(
                ColumnaDeImportacion("nombre", "Servicio que se agenda (no se repite).", obligatoria=True),
                ColumnaDeImportacion("duracion_minutos", "Duración en minutos, entre 5 y 480.", obligatoria=True),
                ColumnaDeImportacion("descripcion", "Qué incluye (opcional)."),
            ),
            ejemplos=(
                {"nombre": "Corte de cabello", "duracion_minutos": "30", "descripcion": "Corte clásico o moderno"},
                {"nombre": "Barba y cejas", "duracion_minutos": "20", "descripcion": ""},
            ),
            esquema=TipoDeCitaImportado,
            columna_clave="nombre",
            clave=lambda fila: fila.nombre,
            ayuda="Un tipo de cita por fila. Si ya tienes uno con el mismo nombre, esa fila se omite.",
        )
    ]


async def _nombres_vivos(tenant_id: uuid.UUID) -> set[str]:
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            filas = await session.execute(
                select(func.lower(AppointmentType.name)).where(
                    AppointmentType.tenant_id == tenant_id, AppointmentType.deleted_at.is_(None)
                )
            )
            return {n for (n,) in filas.all()}


async def importar(
    tenant_id: uuid.UUID,
    plantilla_id: str,
    filas: list[BaseModel],
    contexto: ContextoDeImportacion,
    *,
    simular: bool,
) -> ResultadoDeImportacion:
    from pack_calendar.seed import sembrar_agenda

    existentes = await _nombres_vivos(tenant_id)
    resultado = ResultadoDeImportacion()
    nuevos: list[dict[str, Any]] = []
    for i, fila in enumerate(filas):
        assert isinstance(fila, TipoDeCitaImportado)
        if fila.nombre.casefold() in existentes:
            resultado.existentes.append(i)
            continue
        resultado.nuevas.append(i)
        nuevos.append(
            {"name": fila.nombre, "duration_minutes": fila.duracion_minutos, "description": fila.descripcion}
        )
    if nuevos and not simular:
        creados: list[tuple[str, uuid.UUID]] = []
        await sembrar_agenda(tenant_id, {"timezone": contexto.zona_horaria, "tipos": nuevos}, creados=creados)
        resultado.creados = [RegistroSembrado(kind=k, ref=r) for k, r in creados]
    return resultado
