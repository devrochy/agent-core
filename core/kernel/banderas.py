"""Banderas del Agent OS por negocio: las de la colaboración con pila de tareas en
`tenants.settings.agent_os` (HU-AOS-20, adenda de ADR-0116). El runtime, el motor de procedimientos y
el ruteo entre especialistas ya no son banderas (ADR-0191, HU-AOS-69): todo negocio corre en el Agent
OS con el motor encendido y el ruteo en `active` (el KernelSpec de la release aún puede pedir sombra,
`routing.shadowMode`).

Las guarda la base y solo las edita el master. Se leen en cada turno, así un cambio surte efecto sin redeploy.
Un solo lector para todo el kernel: no se consultan `tenants` sueltas en cada módulo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MODOS_PILA = ("off", "shadow", "active")
MODOS_VERIFICACION = ("shadow", "active")


@dataclass(frozen=True)
class Colaboracion:
    """Banderas de la colaboración con pila de tareas (HU-AOS-20, adenda de ADR-0116), en
    `tenants.settings.agent_os`. Un valor desconocido cuenta como el más apagado."""

    pila: str = "off"                    # off | shadow | active
    verificar_horarios: str = "shadow"   # shadow | active
    verificar_preguntas: str = "shadow"  # shadow | active

    @classmethod
    def desde(cls, crudo: object) -> Colaboracion:
        d = crudo if isinstance(crudo, dict) else {}
        pila = d.get("pila")
        horarios = d.get("verificar_horarios")
        preguntas = d.get("verificar_preguntas")
        return cls(
            pila=pila if pila in MODOS_PILA else "off",
            verificar_horarios=horarios if horarios in MODOS_VERIFICACION else "shadow",
            verificar_preguntas=preguntas if preguntas in MODOS_VERIFICACION else "shadow",
        )


@dataclass(frozen=True)
class BanderasAOS:
    colaboracion: Colaboracion = Colaboracion()


APAGADAS = BanderasAOS()


async def banderas_del_negocio(session: AsyncSession, tenant_id: uuid.UUID) -> BanderasAOS:
    """Banderas del negocio; apagadas si el negocio no existe (fail-closed hacia lo de siempre)."""
    fila = (
        await session.execute(
            text("SELECT settings->'agent_os' FROM tenants WHERE id = :t"),
            {"t": tenant_id},
        )
    ).first()
    if fila is None:
        return APAGADAS
    return BanderasAOS(colaboracion=Colaboracion.desde(fila[0]))


async def negocios_activos(session: AsyncSession) -> list[uuid.UUID]:
    """Negocios vivos y no suspendidos (job del canary, HU-AOS-32; reporte de bindings). `tenants`
    es tabla de sistema, sin RLS: cada negocio se recorre después bajo su `tenant_scope`."""
    filas = await session.execute(
        text(
            "SELECT id FROM tenants WHERE deleted_at IS NULL "
            "AND status IS DISTINCT FROM 'suspended' ORDER BY id"
        )
    )
    return [fila[0] for fila in filas]


async def negocios_vivos(session: AsyncSession) -> list[uuid.UUID]:
    """Negocios no borrados, también los suspendidos (crons del kernel, HU-AOS-07: una espera vencida
    se cancela igual). `tenants` es tabla de sistema, sin RLS: cada negocio se recorre después bajo
    su `tenant_scope`."""
    filas = await session.execute(text("SELECT id FROM tenants WHERE deleted_at IS NULL ORDER BY id"))
    return [fila[0] for fila in filas]


# Candado por negocio de los cambios del Agent OS (backfills, construir): lo comparten «Preparar mis
# datos» (`core.qa.datos_aos`) y los escenarios (`core.qa.escenarios`) para que no se intercalen.
# Parámetro `:t` = el id del negocio como texto.
CANDADO_SQL = "SELECT pg_try_advisory_xact_lock(hashtextextended('agent_os:' || :t, 0))"
