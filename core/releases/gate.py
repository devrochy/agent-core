"""Cierre del gate de pass^k (HU-AOS-21 parte 4, adenda de ADR-0117).

- **Bandera por negocio:** `tenants.settings.evals.gate = "cerrado"` (la ausencia es abierta). Solo la
  escribe el master (`fijar`), con un UPDATE atómico que mezcla dentro de `settings->'evals'` y queda en
  `audit_log`. Con la bandera apagada el gate sigue como en la parte 3 (`sin_evaluar` pasa).
- **Estado de la medición:** `ultima_medicion` lee el último `evaluated` de la candidata contra la activa
  (la misma consulta del corredor).
- **Encolado:** `solicitar_medicion` decide si encola `core.releases.evaluar_candidata` (el 409
  `gate_pendiente` y el botón «Evaluar» pasan por aquí). No encola si hay una medición en curso o si ya se
  cortaron los reintentos del día; si el encolado falla, no lanza.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

CERRADO = "cerrado"
TAREA = "core.releases.evaluar_candidata"
TOPE_MAXIMO_USD = 100.0
# Una sola tarea pendiente por (candidata, base): la clave vive hasta que la tarea termina o vence.
PENDIENTE = "gate:pendiente:{tenant}:{release}:{base}"


def clave_pendiente(tenant_id: uuid.UUID | str, release_id: uuid.UUID | str, base_release_id: uuid.UUID | str) -> str:
    return PENDIENTE.format(tenant=tenant_id, release=release_id, base=base_release_id)


def vida_pendiente_s() -> int:
    """Lo que puede tardar una tarea entre esperar el candado y medir."""
    from core.config import get_settings

    return get_settings().eval_timeout_segundos * 3 + 600


class SinBaseDeEval(RuntimeError):
    """No se puede cerrar el gate sin la base `agentcore_eval` configurada en el entorno."""


class NegocioNoEncontrado(LookupError):
    pass


@dataclass(frozen=True)
class Bandera:
    cerrado: bool
    tope_usd_mensual: float | None


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("gate: fuera del tenant_scope del negocio")


def base_de_eval_configurada() -> bool:
    from core.config import get_settings

    s = get_settings()
    return bool(getattr(s, "eval_database_url", None)) and bool(getattr(s, "eval_migrations_database_url", None))


async def cerrado(session: Any, tenant_id: uuid.UUID) -> bool:
    """¿El negocio tiene el gate cerrado? Se lee dentro de la sesión de `promover`."""
    valor = (
        await session.execute(
            text("SELECT settings->'evals'->>'gate' FROM tenants WHERE id = :t"), {"t": tenant_id}
        )
    ).scalar_one_or_none()
    return valor == CERRADO


async def leer(tenant_id: uuid.UUID) -> Bandera:
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                text("SELECT settings->'evals' FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
            )
        ).first()
    if fila is None:
        raise NegocioNoEncontrado(str(tenant_id))
    evals = fila[0] if isinstance(fila[0], dict) else {}
    tope = evals.get("tope_usd_mensual")
    try:
        tope_f = float(tope) if tope is not None else None
    except (TypeError, ValueError):
        tope_f = None
    return Bandera(evals.get("gate") == CERRADO, tope_f)


async def fijar(tenant_id: uuid.UUID, *, cerrado: bool, tope_usd_mensual: float | None, actor: str) -> Bandera:
    """Cambia la bandera y el tope con un UPDATE atómico (no pisa el resto de `settings`) y lo audita.
    `tope_usd_mensual = None` vuelve al tope del entorno (`EVAL_TOPE_USD_MENSUAL`)."""
    from core.db.models import AuditLog
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    if tope_usd_mensual is not None and (
        not math.isfinite(tope_usd_mensual) or not 0 <= tope_usd_mensual <= TOPE_MAXIMO_USD
    ):
        raise ValueError(f"el tope debe estar entre 0 y {TOPE_MAXIMO_USD:g} USD")
    if cerrado and not base_de_eval_configurada():
        raise SinBaseDeEval("Falta configurar la base de evaluación (EVAL_DATABASE_URL) en este entorno.")
    cambios: dict[str, Any] = {"gate": CERRADO if cerrado else None, "tope_usd_mensual": tope_usd_mensual}
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    # `jsonb_strip_nulls` quita las claves en null: sin bandera = abierta, sin tope = el
                    # del entorno. Mezcla dentro del subobjeto `evals`, sin reescribir `settings`.
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{evals}', "
                    "jsonb_strip_nulls(coalesce(settings->'evals', '{}'::jsonb) || cast(:cambios as jsonb))) "
                    "WHERE id = :t AND deleted_at IS NULL RETURNING settings->'evals'"
                ),
                {"t": tenant_id, "cambios": json.dumps(cambios)},
            )
        ).first()
        if fila is None:
            raise NegocioNoEncontrado(str(tenant_id))
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor[:120],
                action="release_gate_bandera",
                input={"cerrado": cerrado, "tope_usd_mensual": tope_usd_mensual},
            )
        )
    log.info("release_gate_bandera", tenant_id=str(tenant_id), cerrado=cerrado, tope=tope_usd_mensual)
    return Bandera(cerrado, tope_usd_mensual)


async def ultima_medicion(
    session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID, base_release_id: uuid.UUID
) -> dict[str, Any] | None:
    """El último `evaluated` de la candidata contra esa base: `{estado, data, creado}`, o None. Con las
    fallidas del día en el tope del corredor, el estado es `demasiados_intentos` (no se vuelve a pedir)."""
    from datetime import UTC, datetime

    from core.config import get_settings
    from core.releases.corredor import MAX_FALLIDAS_DIA, mediciones_contra

    eventos = await mediciones_contra(
        session, tenant_id, release_id, base_release_id, get_settings().eval_timeout_segundos
    )
    if not eventos:
        return None
    data = dict(eventos[0]["data"] or {})
    estado = str(data.get("estado") or "")
    if estado == "en_curso" and eventos[0]["vencido"]:
        estado = "vencida"
    hace_un_dia = datetime.now(UTC).timestamp() - 86400
    fallidas = sum(
        1 for e in eventos if (e["data"] or {}).get("estado") == "fallida" and e["created_at"].timestamp() > hace_un_dia
    )
    if estado == "fallida" and fallidas >= MAX_FALLIDAS_DIA:
        estado = "demasiados_intentos"
    return {"estado": estado, "data": data, "creado": eventos[0]["created_at"]}


async def solicitar_medicion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID, *, actor: str
) -> str:
    """Encola la medición si hace falta. Devuelve el estado para el panel: `encolada`, `en_curso`,
    `demasiados_intentos`, `sin_release_activa` o `no_encolada` (falló el encolado). Nunca lanza por
    la cola: el 409 se responde igual."""
    from core.db.session import get_session
    from core.tasks.queue import task_queue

    _exigir_scope(tenant_id)
    async with get_session() as session:
        activa = (
            await session.execute(
                text(
                    "SELECT active_release_id FROM channel_connections "
                    "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
        if activa is None:
            return "sin_release_activa"
        previa = await ultima_medicion(session, tenant_id, release_id, activa)
    if previa is not None and previa["estado"] in ("en_curso", "demasiados_intentos"):
        return previa["estado"]
    try:
        # Idempotente: con una tarea ya pendiente para esta candidata y base no se encola otra.
        from core.memory.working import get_redis

        if not await get_redis().set(clave_pendiente(tenant_id, release_id, activa), "1", nx=True, ex=vida_pendiente_s()):
            return "encolada"
    except Exception as exc:  # sin Redis: se encola igual (el corredor sigue siendo idempotente)
        log.warning("release_gate_dedupe_fallo", error=type(exc).__name__)
    try:
        task_queue.enqueue(
            TAREA,
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            release_id=str(release_id),
            actor=actor[:120],
        )
    except Exception as exc:
        log.error("release_gate_encolado_fallo", error=type(exc).__name__)
        return "no_encolada"
    log.info("release_gate_encolado", release_id=str(release_id))
    return "encolada"


async def pedir_evaluacion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID, *, actor: str
) -> str:
    """El botón «Evaluar»: resuelve en la petición lo que no necesita medir y si no, encola.
    Devuelve `ya_activa`, `gate_abierto`, `sin_release_activa`, `sin_cambios`, `ya_medida` o lo de
    `solicitar_medicion`. `LookupError` si la release no es de esa conexión."""
    from core.db.session import get_session
    from core.releases import veredicto
    from core.releases.cargar import cargar_release
    from core.releases.diferencias import procedimientos_tocados

    _exigir_scope(tenant_id)
    async with get_session() as session:
        activa = (
            await session.execute(
                text(
                    "SELECT active_release_id FROM channel_connections "
                    "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
        candidata = await cargar_release(session, tenant_id, release_id)
        if candidata is None or candidata.connection_id != connection_id:
            raise LookupError(str(release_id))
        if activa == release_id:
            return "ya_activa"
        if not await cerrado(session, tenant_id):
            return "gate_abierto"
        if activa is None:
            return "sin_release_activa"
        base = await cargar_release(session, tenant_id, activa)
        if not procedimientos_tocados(candidata, base):
            return "sin_cambios"
        if await veredicto.ultimo(session, tenant_id, release_id, activa) is not None:
            return "ya_medida"
    return await solicitar_medicion(tenant_id, connection_id, release_id, actor=actor)


async def estado_de_evaluacion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID
) -> dict[str, Any]:
    """La medición de la candidata contra la activa de ahora, para el panel: bandera, último
    veredicto (por procedimiento y lo `sin_medir`) y última medición. Sin textos de escenarios."""
    from core.db.session import get_session
    from core.releases import veredicto
    from core.releases.cargar import cargar_release

    _exigir_scope(tenant_id)
    async with get_session() as session:
        activa = (
            await session.execute(
                text(
                    "SELECT active_release_id FROM channel_connections "
                    "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
        candidata = await cargar_release(session, tenant_id, release_id)
        if candidata is None or candidata.connection_id != connection_id:
            raise LookupError(str(release_id))
        gate_cerrado = await cerrado(session, tenant_id)
        previo = await veredicto.ultimo(session, tenant_id, release_id, activa) if activa else None
        medicion = await ultima_medicion(session, tenant_id, release_id, activa) if activa else None
    resultado: dict[str, Any] = {
        "gate_cerrado": gate_cerrado,
        "base_release_id": str(activa) if activa else None,
        "es_la_activa": activa == release_id,
        "veredicto": None,
        "medicion": None,
    }
    if previo is not None:
        resultado["veredicto"] = {
            "aprobado": previo["tipo"] == veredicto.APROBADO,
            "motivo": str(previo.get("motivo") or "")[:200],
            "k": previo.get("k"),
            "umbral": previo.get("umbral"),
            "por_procedimiento": previo.get("por_procedimiento") or {},
            "sin_medir": list(previo.get("sin_medir") or []),
        }
    if medicion is not None:
        data = medicion["data"]
        resultado["medicion"] = {
            "estado": medicion["estado"],
            "codigo": data.get("codigo"),
            "creado": medicion["creado"].isoformat() if medicion["creado"] else None,
            "sin_escenarios": list(data.get("sin_escenarios") or []),
        }
    return resultado


__all__ = [
    "CERRADO",
    "TAREA",
    "Bandera",
    "NegocioNoEncontrado",
    "SinBaseDeEval",
    "base_de_eval_configurada",
    "cerrado",
    "clave_pendiente",
    "estado_de_evaluacion",
    "fijar",
    "leer",
    "pedir_evaluacion",
    "solicitar_medicion",
    "ultima_medicion",
]
