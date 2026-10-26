"""Banderas del Agent OS por negocio (ADR-0112): `tenants.aos_runtime`, `tenants.aos_procedures` y
`tenants.aos_routing` (HU-AOS-17, ADR-0138), más las de la colaboración con pila de tareas en
`tenants.settings.agent_os` (HU-AOS-20, adenda de ADR-0116).

Las guarda la base (migración 0070, con el CHECK de orden `aos_procedures ⇒ aos_runtime = 'v3'`)
y solo las edita el master. Se leen en cada turno, así un cambio surte efecto sin redeploy.
Un solo lector para todo el kernel: no se consultan `tenants` sueltas en cada módulo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

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
    runtime: str = "legacy"   # legacy | v3
    procedimientos: bool = False
    # Enrutamiento entre especialistas (HU-AOS-17, migración 0094, ADR-0138): off | shadow | active.
    ruteo: str = "off"
    colaboracion: Colaboracion = Colaboracion()


APAGADAS = BanderasAOS()


async def banderas_del_negocio(session: AsyncSession, tenant_id: uuid.UUID) -> BanderasAOS:
    """Banderas del negocio; apagadas si el negocio no existe (fail-closed hacia lo de siempre)."""
    fila = (
        await session.execute(
            text("SELECT aos_runtime, aos_procedures, aos_routing, settings->'agent_os' FROM tenants WHERE id = :t"),
            {"t": tenant_id},
        )
    ).first()
    if fila is None:
        return APAGADAS
    return BanderasAOS(
        runtime=fila[0],
        procedimientos=bool(fila[1]),
        ruteo=str(fila[2] or "off"),
        colaboracion=Colaboracion.desde(fila[3]),
    )


async def negocios_con_procedimientos(session: AsyncSession) -> list[uuid.UUID]:
    """Negocios vivos con `aos_procedures` encendida (crons del kernel, HU-AOS-07). `tenants` es
    tabla de sistema, sin RLS: cada negocio se recorre después bajo su `tenant_scope`."""
    filas = await session.execute(
        text("SELECT id FROM tenants WHERE aos_procedures AND deleted_at IS NULL ORDER BY id")
    )
    return [fila[0] for fila in filas]


# Candado por negocio de los cambios del Agent OS (backfills, construir, banderas): lo comparten el
# panel del master (`core.admin.agent_os`) y «Preparar mis datos» (`core.qa.datos_aos`) para que no
# se intercalen. Parámetro `:t` = el id del negocio como texto.
CANDADO_SQL = "SELECT pg_try_advisory_xact_lock(hashtextextended('agent_os:' || :t, 0))"


class BanderasRechazadas(Exception):
    """La base rechazó el cambio (orden de banderas, permiso o negocio inexistente). `codigo`:
    `orden` | `permiso` | `no_existe` | `invalido`. El mensaje no lleva datos del negocio."""

    def __init__(self, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.codigo = codigo


_SQLSTATE = {
    "23514": ("orden", "combinación de banderas fuera de orden (procedimientos y enrutamiento exigen v3)"),
    "42501": ("permiso", "solo el admin master cambia las banderas del Agent OS"),
    "P0002": ("no_existe", "negocio no encontrado"),
    "22023": ("invalido", "valores de banderas inválidos"),
}


async def fijar_banderas(
    conn: AsyncConnection | AsyncSession,
    tenant_id: uuid.UUID,
    banderas: BanderasAOS,
    *,
    admin_id: uuid.UUID | None = None,
    actor: str | None = None,
) -> bool:
    """El único escritor de las tres banderas: `aos_fijar_banderas` (migración 0097, SECURITY
    DEFINER), que valida el orden y deja la fila de `audit_log` en la misma transacción. Con
    `admin_id` la base exige que sea master (la API, con `app_runtime`); sin él, la sesión debe ser
    del rol dueño (CLI y «Preparar mis datos»). Devuelve si cambió algo. `AOS_CONNECTION_ROUTING`
    (entorno) la valida quien llama."""
    from sqlalchemy.exc import DBAPIError

    try:
        resultado = (
            await conn.execute(
                text("SELECT aos_fijar_banderas(:t, :r, :p, :ru, :a, :actor)"),
                {
                    "t": tenant_id,
                    "r": banderas.runtime,
                    "p": banderas.procedimientos,
                    "ru": banderas.ruteo,
                    "a": admin_id,
                    "actor": actor,
                },
            )
        ).scalar_one()
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(getattr(exc.orig, "__cause__", None), "sqlstate", None)
        if sqlstate in _SQLSTATE:
            raise BanderasRechazadas(*_SQLSTATE[sqlstate]) from None
        raise
    return bool((resultado or {}).get("cambio"))
