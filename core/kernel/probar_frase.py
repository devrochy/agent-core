"""Probar una frase contra el router de una conexión (HU-AOS-26, panel de colaboración).

Qué agente y qué procedimiento atenderían una frase con la **release activa** de la conexión, y por
qué: una regla, un disparador con su puntaje o el agente por defecto. Usa el mismo router del turno
(`router.decidir`), que es léxico y sin LLM, así que probar no cuesta.

Sin conversación hay cosas que el router no puede saber: no hay tarea en foco ni dueño, y las
reglas por datos del cliente (`customerFact`), por etapa del flujo (`flowStage`) o por horario
(`withinHours`) no se evalúan. Se devuelven en `no_evaluadas` para que el panel lo diga.

No escribe nada ni registra la frase (puede traer datos del cliente).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from core.config import get_settings
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id


class ConexionNoEncontrada(LookupError):
    """La conexión no existe en el negocio (o está borrada)."""


class SinReleaseActiva(LookupError):
    """La conexión no tiene release activa."""


@dataclass(frozen=True)
class Prueba:
    modo: str  # off | shadow | active: si el router decide hoy en esta conexión
    source: str  # rule | trigger | default
    agent_key: str
    agent_id: uuid.UUID
    procedure_id: str | None
    rule_id: str | None
    confidence: float | None
    candidates: tuple[dict[str, Any], ...]
    no_evaluadas: tuple[str, ...] = field(default_factory=tuple)


async def probar(tenant_id: uuid.UUID, connection_id: uuid.UUID, frase: str) -> Prueba:
    from core.kernel import router
    from core.kernel.banderas import banderas_del_negocio
    from core.releases.cargar import cargar_release

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("probar_frase: fuera del tenant_scope del negocio")
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT active_release_id, channel FROM channel_connections "
                    "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).first()
        if fila is None:
            raise ConexionNoEncontrada(str(connection_id))
        if fila[0] is None:
            raise SinReleaseActiva(str(connection_id))
        release = await cargar_release(session, tenant_id, fila[0])
        if release is None:
            raise SinReleaseActiva(str(connection_id))
        banderas = await banderas_del_negocio(session, tenant_id)
    routing = release.ruteo()
    bandera = banderas.ruteo if get_settings().aos_connection_routing and banderas.runtime == "v3" else "off"
    entrada = router.Entrada(texto=frase[: router.MAX_TEXTO], channel=str(fila[1] or ""))
    ruta = router.decidir(release, routing, entrada, release.foco(None))
    no_evaluadas = tuple(
        r.id for r in routing.rules if r.when.customerFact or r.when.flowStage or r.when.withinHours
    )
    return Prueba(
        modo=router.modo_efectivo(bandera, routing),
        source=ruta.source,
        agent_key=ruta.agente.key,
        agent_id=ruta.agente.agent_id,
        procedure_id=ruta.procedure_id,
        rule_id=ruta.rule_id,
        confidence=ruta.confidence,
        candidates=tuple(dict(c) for c in ruta.candidates),
        no_evaluadas=no_evaluadas,
    )


__all__ = ["ConexionNoEncontrada", "Prueba", "SinReleaseActiva", "probar"]
