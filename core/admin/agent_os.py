"""Agent OS por negocio desde el panel del master (HU-AOS-15, criterio «Orden de banderas»).

El master fija el estado objetivo de las tres banderas de un negocio (`aos_runtime`,
`aos_procedures`, `aos_routing`) y este módulo lo lleva ahí con las MISMAS funciones de la
operación, sin duplicar su lógica:

- `planificar` (puro): rechaza el objetivo fuera de orden (procedimientos o enrutamiento sin v3,
  `ObjetivoInvalido` → 422) y ordena los pasos: al encender runtime → procedimientos → ruteo; al
  apagar, el inverso (ADR-0112, ADR-0138).
- Pasar de legacy a v3 es `migrar_a_v3` con los backfills del negocio (idempotentes y neutros en
  legacy) y, solo si el master lo pide, `construir` (construye y promueve una release nueva: cambia
  la release activa). Volver a legacy es `migrar_a_v3(volver=True)`. Sus precondiciones fail-closed
  son los bloqueos (409).
- La escritura es una sola llamada a `aos_fijar_banderas` (migración 0097) con el `admin_id` del
  master: la base exige que sea master, valida el orden y deja la fila de `audit_log` en la misma
  transacción. El request del master no abre un engine con el rol dueño (el contenedor de la API
  sigue teniendo `MIGRATIONS_DATABASE_URL` para migrar, `sync-packs` y «Preparar mis datos»).

`simular` corre lo mismo en dry-run (nada se escribe): bloqueos, avisos y los pasos que
`aplicar` ejecutaría. Avisos del runbook (no bloquean): el pass^4 de HU-ARCH-28 antes de encender
procedimientos, una semana en `shadow` con `agent-core db reporte-ruteo` antes de `active`, y que
sin procedimientos el reporte de sombra no tiene con qué comparar.

`AOS_CONNECTION_ROUTING` es global (entorno): este módulo solo la lee; apagarla con negocios en
v3 es un cambio de despliegue, fuera del alcance de un endpoint por negocio.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from core.config import get_settings
from core.db.session import get_session
from core.kernel.banderas import (
    APAGADAS,
    CANDADO_SQL,
    BanderasAOS,
    BanderasRechazadas,
    banderas_del_negocio,
    fijar_banderas,
)
from core.kernel.router import MODOS
from core.shared.logger import get_logger

log = get_logger(__name__)

RUNTIMES = ("legacy", "v3")
AVISO_PASS_K = (
    "Antes de encender los procedimientos, mide el pass^4 del negocio (HU-ARCH-28) y anótalo en la "
    "bitácora: si empeora, apágalos."
)
AVISO_SHADOW = (
    "Runbook: deja el enrutamiento una semana en shadow y revisa `agent-core db reporte-ruteo` "
    "(precisión ≥ 0,90 y recall ≥ 0,85) antes de pasarlo a active."
)
AVISO_SIN_PROCEDIMIENTOS = (
    "Sin procedimientos, el reporte de sombra no tiene con qué comparar (queda vacío) y, en active, "
    "las reglas que inician un procedimiento no inician nada (el motor está apagado)."
)


class ObjetivoInvalido(ValueError):
    """El estado pedido viola el orden de banderas por sí solo (422)."""


class NegocioNoEncontrado(Exception):
    """El negocio no existe o está borrado (404). Propia: un `KeyError` interno no es un 404."""


@dataclass
class Resultado:
    antes: BanderasAOS
    objetivo: BanderasAOS
    despues: BanderasAOS
    # sin_cambios | listo | por_preparar | bloqueado | aplicado
    accion: str
    aos_connection_routing: bool
    pasos: list[str] = field(default_factory=list)
    bloqueos: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ planificador (puro)
def planificar(actual: BanderasAOS, objetivo: BanderasAOS) -> list[str]:
    """Pasos para ir de `actual` a `objetivo`, en el orden de ADR-0112. `ObjetivoInvalido` si el
    objetivo viola el orden por sí solo. Lista vacía: nada que hacer."""
    if objetivo.runtime not in RUNTIMES:
        raise ObjetivoInvalido(f"aos_runtime inválido: {objetivo.runtime}")
    if objetivo.ruteo not in MODOS:
        raise ObjetivoInvalido(f"aos_routing inválido: {objetivo.ruteo}")
    if objetivo.procedimientos and objetivo.runtime != "v3":
        raise ObjetivoInvalido("Los procedimientos exigen el runtime v3.")
    if objetivo.ruteo != "off" and objetivo.runtime != "v3":
        raise ObjetivoInvalido("El enrutamiento exige el runtime v3.")
    encender: list[str] = []
    apagar: list[str] = []
    if actual.runtime != objetivo.runtime:
        (encender if objetivo.runtime == "v3" else apagar).append(f"runtime_{objetivo.runtime}")
    if actual.procedimientos != objetivo.procedimientos:
        (encender if objetivo.procedimientos else apagar).append(
            "procedimientos_on" if objetivo.procedimientos else "procedimientos_off"
        )
    if actual.ruteo != objetivo.ruteo:
        (apagar if objetivo.ruteo == "off" else encender).append(f"ruteo_{objetivo.ruteo}")
    # Apagar va al revés del encendido: ruteo → procedimientos → runtime.
    orden_apagar = {"ruteo_off": 0, "procedimientos_off": 1, "runtime_legacy": 2}
    return sorted(apagar, key=lambda p: orden_apagar[p]) + encender


def avisos_del_runbook(actual: BanderasAOS, objetivo: BanderasAOS) -> list[str]:
    avisos: list[str] = []
    if objetivo.procedimientos and not actual.procedimientos:
        avisos.append(AVISO_PASS_K)
    if objetivo.ruteo == "active" and actual.ruteo != "active":
        avisos.append(AVISO_SHADOW)
    if objetivo.ruteo != "off" and not objetivo.procedimientos:
        avisos.append(AVISO_SIN_PROCEDIMIENTOS)
    return avisos


# ------------------------------------------------------------------ lectura
async def estado(tenant_id: uuid.UUID) -> BanderasAOS | None:
    """Banderas del negocio vivo, o None si no existe."""
    async with get_session() as session:
        vivo = (
            await session.execute(text("SELECT 1 FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id})
        ).first()
        if vivo is None:
            return None
        return await banderas_del_negocio(session, tenant_id)


async def ultimo_cambio(tenant_id: uuid.UUID) -> dict[str, Any] | None:
    """La última fila `agent_os.banderas` de `audit_log` del negocio (quién y cuándo)."""
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            fila = (
                await session.execute(
                    text(
                        "SELECT actor, created_at, input, output FROM audit_log WHERE tenant_id = :t "
                        "AND action = 'agent_os.banderas' ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"t": tenant_id},
                )
            ).first()
    if fila is None:
        return None
    return {"actor": fila[0], "fecha": fila[1], "antes": fila[2], "despues": fila[3]}


async def _tareas_gobernadas_abiertas(tenant_id: uuid.UUID) -> int:
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM conversation_tasks WHERE tenant_id = :t "
                            "AND status IN ('active','suspended')"
                        ),
                        {"t": tenant_id},
                    )
                ).scalar_one()
            )


# ------------------------------------------------------------------ simular / aplicar
async def _escribir(tenant_id: uuid.UUID, objetivo: BanderasAOS, admin_id: uuid.UUID, actor: str) -> None:
    from core.shared.tenant import tenant_scope

    # Fuera de cualquier tenant_scope: la función lo exige con admin (migración 0097).
    async with tenant_scope(None):
        async with get_session() as session:
            await fijar_banderas(session, tenant_id, objetivo, admin_id=admin_id, actor=actor)


async def _correr(
    tenant_id: uuid.UUID,
    objetivo: BanderasAOS,
    *,
    dry_run: bool,
    construir: bool,
    admin_id: uuid.UUID,
    actor: str,
) -> Resultado:
    from core.plugin_manager import plugin_manager
    from core.releases.migracion_v3 import migrar_a_v3

    actual = await estado(tenant_id)
    if actual is None:
        raise NegocioNoEncontrado("negocio no encontrado")
    pasos = planificar(actual, objetivo)  # ObjetivoInvalido → 422
    global_ = bool(get_settings().aos_connection_routing)
    res = Resultado(actual, objetivo, actual, "sin_cambios", global_, pasos=list(pasos))
    if not pasos:
        return res
    res.avisos.extend(avisos_del_runbook(actual, objetivo))
    if "procedimientos_off" in pasos:
        abiertas = await _tareas_gobernadas_abiertas(tenant_id)
        if abiertas:
            res.avisos.append(f"{abiertas} tarea(s) abierta(s) del motor dejan de gobernarse al apagar los procedimientos")

    escrito = False

    async def escritor(tid: uuid.UUID, runtime: str, _procedimientos: bool | None) -> None:
        # migrar_a_v3 pide su runtime; se escribe el objetivo completo de una vez (la función
        # valida el orden del conjunto y audita el cambio).
        nonlocal escrito
        if tid != tenant_id or runtime != objetivo.runtime:
            raise RuntimeError("migrar-a-v3 pidió escribir otro negocio u otro runtime")
        await _escribir(tenant_id, objetivo, admin_id, actor)
        escrito = True

    if actual.runtime != objetivo.runtime:
        volver = objetivo.runtime == "legacy"
        reporte = await migrar_a_v3(
            tenant_ids=[tenant_id],
            dry_run=dry_run,
            volver=volver,
            construir=construir and not volver,
            apagar_procedimientos=volver,
            modules=plugin_manager.list_modules(),
            backfills=not volver,
            escritor=escritor,
        )
        negocio = reporte.negocios[0] if reporte.negocios else None
        if negocio is None:
            raise NegocioNoEncontrado("negocio no encontrado")
        res.bloqueos.extend(negocio.bloqueos)
        res.avisos.extend(negocio.avisos)
        res.pasos.extend(negocio.pasos)
        if negocio.accion == "bloqueado":
            res.accion = "bloqueado"
            return res
        if dry_run:
            res.accion = "por_preparar" if negocio.accion == "por_preparar" else "listo"
            return res
        if not escrito:
            # `ya_estaba` / `ya_legacy` (carrera con otro cambio): se escribe el resto igual.
            await _escribir(tenant_id, objetivo, admin_id, actor)
    else:
        if objetivo.runtime == "v3" and not global_:
            # v3 ya encendido pero el entorno lo apagó: sin la global el runtime no lee releases.
            res.bloqueos.append("AOS_CONNECTION_ROUTING está apagada en este entorno: v3 no corre (ADR-0112)")
            res.accion = "bloqueado"
            return res
        if dry_run:
            res.accion = "listo"
            return res
        await _escribir(tenant_id, objetivo, admin_id, actor)
    res.despues = await estado(tenant_id) or APAGADAS
    res.accion = "aplicado"
    log.info(
        "agent_os_banderas",
        tenant_id=str(tenant_id),
        admin_id=str(admin_id),
        antes=f"{actual.runtime}/{actual.procedimientos}/{actual.ruteo}",
        despues=f"{res.despues.runtime}/{res.despues.procedimientos}/{res.despues.ruteo}",
    )
    return res


async def simular(tenant_id: uuid.UUID, objetivo: BanderasAOS, *, construir: bool, admin_id: uuid.UUID) -> Resultado:
    """Dry-run: nada se escribe (ni backfills ni releases ni banderas)."""
    return await _correr(tenant_id, objetivo, dry_run=True, construir=construir, admin_id=admin_id, actor="")


async def aplicar(
    tenant_id: uuid.UUID, objetivo: BanderasAOS, *, construir: bool, admin_id: uuid.UUID, actor: str
) -> Resultado:
    """Lleva el negocio al objetivo con un candado por negocio (`pg_try_advisory_xact_lock`: los
    backfills y `construir` no deben correr dos veces a la vez). `CambioEnCurso` si está tomado."""
    async with get_session() as candado:
        # El mismo candado que «Preparar mis datos» (`datos_aos.preparar`).
        tomado = (await candado.execute(text(CANDADO_SQL), {"t": str(tenant_id)})).scalar_one()
        if not tomado:
            raise CambioEnCurso("Ya hay un cambio del Agent OS en curso para este negocio. Espera un momento.")
        return await _correr(tenant_id, objetivo, dry_run=False, construir=construir, admin_id=admin_id, actor=actor)


class CambioEnCurso(Exception):
    """Otro cambio del Agent OS corre sobre el mismo negocio (409)."""


__all__ = [
    "AVISO_PASS_K",
    "AVISO_SHADOW",
    "AVISO_SIN_PROCEDIMIENTOS",
    "BanderasRechazadas",
    "CambioEnCurso",
    "NegocioNoEncontrado",
    "ObjetivoInvalido",
    "Resultado",
    "aplicar",
    "avisos_del_runbook",
    "estado",
    "planificar",
    "simular",
    "ultimo_cambio",
]
