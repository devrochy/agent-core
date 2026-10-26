"""El job del canary: vigila cada prueba viva y la retira si empeora (HU-AOS-32 parte 2, adenda de
ADR-0117 §5-§6).

Dos entradas en el beat (`core/tasks/celery_app.py`), sin estado propio para saber «cada cuánto»:
- **cada 5 min** (`no_inferioridad=False`): habilidad retirada y señal dura;
- **cada hora** (`no_inferioridad=True`): además, la no-inferioridad. El veredicto se decide una sola
  vez (`canary_evaluated`): `peor` retira la versión, `listo` avisa que se puede publicar (más los
  avisos de costo y lentitud) y a los 7 días sin muestra avisa que se puede publicar a mano.

Por negocio (`aos_runtime = v3`), bajo su `tenant_scope` y con `try` por negocio; por conexión, una
transacción con el `FOR UPDATE` de la conexión que vuelve a leer el canary (idempotente: una carrera
con el dueño, o una segunda corrida, no hace nada dos veces). Los avisos van en la misma transacción
que la acción (`notify`), uno de cada tipo por prueba (la versión y el inicio de la prueba).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from core.shared.logger import get_logger

log = get_logger(__name__)

ACTOR = "system:canary"


async def _usa_habilidad_retirada(session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID) -> bool:
    """Mismo criterio que `promocion._exigir_utilizable` (`pack_versions.yanked`)."""
    fila = (
        await session.execute(
            text(
                "SELECT 1 FROM connection_releases r "
                "CROSS JOIN LATERAL jsonb_array_elements(r.manifest->'skills') s "
                "JOIN pack_versions pv ON pv.pack_id = s->>'pack_id' AND pv.version = s->>'version' "
                "WHERE r.tenant_id = :t AND r.id = :r AND pv.yanked LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id},
        )
    ).first()
    return fila is not None


def _metricas(estado: Any) -> dict[str, Any]:
    """Solo números (va al evento `canary_stopped`)."""
    d = estado.senal_dura
    return {
        "conversaciones": estado.canary.conversaciones,
        "horas": round(estado.horas, 1),
        "veredicto": estado.veredicto.estado,
        "motivos": list(estado.veredicto.motivos),
        "senal_dura": {"evaluadas": d.evaluadas, "llegaron_mal": d.llegaron_mal, "tasa": d.tasa, "tasa_estable": d.tasa_estable},
    }


async def _numero(session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID) -> int | None:
    return (
        await session.execute(
            text("SELECT number FROM connection_releases WHERE tenant_id = :t AND id = :r"),
            {"t": tenant_id, "r": release_id},
        )
    ).scalar_one_or_none()


async def revisar_conexion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, *, ahora: datetime, no_inferioridad: bool
) -> str:
    """Una conexión, en una transacción con su `FOR UPDATE`. Devuelve qué hizo (para los conteos)."""
    from core.db.session import get_session
    from core.notifications import catalog
    from core.notifications.service import notify
    from core.releases import canary as svc
    from core.releases.promocion import _bloquear, detener_canary
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("canary: fuera del tenant_scope del negocio")
    async with get_session() as session:
        _activa, canary = await _bloquear(session, tenant_id, connection_id)
        if canary is None:
            return "sin_canary"
        numero = await _numero(session, tenant_id, canary)
        desde, _datos = await svc._inicio(session, tenant_id, canary)
        episodio = int(desde.timestamp())

        async def retirar(motivo: str, metricas: dict[str, Any] | None, peores: list[str] | None = None) -> str:
            await detener_canary(session, tenant_id, connection_id, canary, motivo=motivo, actor=ACTOR, metricas=metricas)
            await notify(
                session, tenant_id,
                catalog.canary_revertido(connection_id, canary, numero, motivo=motivo, episodio=episodio, metricas=peores),
            )
            log.info("canary_retirado", connection_id=str(connection_id), reason=motivo)
            return motivo

        if await _usa_habilidad_retirada(session, tenant_id, canary):
            return await retirar("habilidad_retirada", None)
        estado = await svc.evaluar(session, tenant_id, connection_id, canary, ahora, solo_senal=not no_inferioridad)
        if estado.senal_dura.activa:
            return await retirar("senal_dura", _metricas(estado))
        if not no_inferioridad:
            return "sigue"
        await svc._guardar_veredicto(session, tenant_id, connection_id, estado, actor=ACTOR)
        veredicto = estado.veredicto
        if veredicto.estado == "peor":
            return await retirar("no_inferioridad", _metricas(estado), list(veredicto.motivos))
        if veredicto.estado == "listo":
            await notify(session, tenant_id, catalog.canary_listo(connection_id, canary, numero, episodio=episodio))
            if "costo" in veredicto.advertencias:
                await notify(session, tenant_id, catalog.canary_costo(connection_id, canary, numero, episodio=episodio))
            if "lentitud" in veredicto.advertencias:
                await notify(session, tenant_id, catalog.canary_lentitud(connection_id, canary, numero, episodio=episodio))
            return "listo"
        if ahora - estado.desde >= timedelta(days=int(estado.umbrales["dias_manual"])):
            await notify(session, tenant_id, catalog.canary_sin_muestra(connection_id, canary, numero, episodio=episodio))
            return "sin_muestra_vencida"
        return "sin_muestra"


async def revisar(ahora: datetime | None = None, *, no_inferioridad: bool) -> dict[str, int]:
    """Recorre los negocios v3 y sus conexiones con canary vivo. Un negocio que falla no frena a los
    demás. Devuelve cuántas conexiones terminaron en cada resultado."""
    from core.db.session import get_session
    from core.kernel.banderas import negocios_v3
    from core.shared.tenant import tenant_scope

    ahora = ahora or datetime.now(UTC)
    async with get_session() as session:
        negocios = await negocios_v3(session)
    conteos: dict[str, int] = {}
    for tenant_id in negocios:
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    conexiones = [
                        f[0]
                        for f in (
                            await session.execute(
                                text(
                                    "SELECT id FROM channel_connections WHERE tenant_id = :t "
                                    "AND canary_release_id IS NOT NULL AND deleted_at IS NULL ORDER BY id"
                                ),
                                {"t": tenant_id},
                            )
                        ).all()
                    ]
                for connection_id in conexiones:
                    # Por conexión: una que falla no frena a las demás del negocio.
                    try:
                        resultado = await revisar_conexion(
                            tenant_id, connection_id, ahora=ahora, no_inferioridad=no_inferioridad
                        )
                    except Exception as exc:
                        resultado = "fallo"
                        log.warning(
                            "canary_job_fallo", tenant_id=str(tenant_id), connection_id=str(connection_id),
                            error=type(exc).__name__, codigo=getattr(exc, "codigo", None),
                        )
                    conteos[resultado] = conteos.get(resultado, 0) + 1
        except Exception as exc:
            conteos["fallo"] = conteos.get("fallo", 0) + 1
            log.warning("canary_job_fallo", tenant_id=str(tenant_id), error=type(exc).__name__)
    if conteos:
        log.info("canary_job", horaria=no_inferioridad, conteos=conteos)
    return conteos


__all__ = ["ACTOR", "revisar", "revisar_conexion"]
