"""Informe de una corrida, arreglo sugerido y volver a probar las fallidas (HU-AOS-104, ADR-0212 §6, SPEC-53).

Todo dentro del `tenant_scope` del negocio y con `tenant_id` explícito además de la RLS; un resultado, corrida o agente
de otro negocio es 404 sin eco.

- `informe`: estado, progreso, créditos y, por escenario, sus intentos con transcripción, razón del juez, criterio
  incumplido y el arreglo (con su diff contra el borrador de AHORA). `porcentaje` es lo que pasa sobre lo que se pudo
  juzgar: `no_se_pudo_probar` no cuenta y una falla marcada `juez_equivocado` tampoco (ni a favor ni en contra).
- `aplicar`: escribe el arreglo en `agent_drafts` por `versiones.guardar_borrador` (contrato + linter + `lock_version`),
  **nunca publica** (no crea `agent_versions`) y solo con la confirmación explícita del dueño: el `lock_version` que vio
  al mirar el diff. Es idempotente: si la regla ya está en el borrador solo marca `arreglo_aplicado_en`.
  Una `respuesta` o un `dato` no se escriben: 409 `arreglo_no_aplicable` con la sección donde se editan.
- `marcar_juez_equivocado`: solo sobre una falla; la deja `falla` pero fuera del porcentaje y de «volver a probar».
- `fallidas` / `estimar_fallidas` / `crear_de_fallidas`: los escenarios con alguna falla vigente de una corrida terminada.
  Los ids se calculan en el servidor (el cliente no los manda) y la corrida nueva pasa por la misma compuerta del dinero
  que cualquier otra (`corridas.estimar` / `corridas.crear`: estimado propio, aprobación, una activa por negocio).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update

from core.db.models import (
    AgentDraftRecord,
    AgentTestCase,
    AgentTestResult,
    AgentTestRun,
    AuditLog,
)
from core.db.session import get_session
from core.pruebas import arreglo as mod
from core.pruebas import corridas
from core.pruebas.corridas import ErrorPruebas
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACCION_APLICAR = "prueba_arreglo_aplicado"
ACCION_JUEZ = "prueba_juez_equivocado"
_ACTIVAS = ("en_cola", "corriendo")


def porcentaje(pasan: int, fallan: int) -> int | None:
    """% de intentos que pasan entre los que se pudieron juzgar (`pasan + fallan`, sin las fallas marcadas
    `juez_equivocado` ni los `no_se_pudo_probar`). `None` si no hay nada que juzgar."""
    juzgados = pasan + fallan
    return None if juzgados == 0 else round(100 * pasan / juzgados)


def _no_encontrado(code: str = "not_found") -> ErrorPruebas:
    return ErrorPruebas(404, code, "No encontrado.")


# ------------------------------------------------------------------------------------------------------- lecturas
async def _corrida(session: Any, tenant_id: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> AgentTestRun:
    corrida = (
        await session.execute(
            select(AgentTestRun).where(
                AgentTestRun.tenant_id == tenant_id,
                AgentTestRun.agent_id == agent_id,
                AgentTestRun.id == run_id,
                AgentTestRun.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if corrida is None:
        raise _no_encontrado()
    return corrida


async def _borrador(session: Any, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> tuple[dict[str, Any], int] | None:
    fila = (
        await session.execute(
            select(AgentDraftRecord.content, AgentDraftRecord.lock_version).where(
                AgentDraftRecord.tenant_id == tenant_id,
                AgentDraftRecord.agent_id == agent_id,
                AgentDraftRecord.deleted_at.is_(None),
            )
        )
    ).first()
    return None if fila is None else (dict(fila[0] or {}), int(fila[1]))


def _vista_del_arreglo(
    guardado: dict[str, Any] | None, aplicado_en: datetime | None, borrador: tuple[dict[str, Any], int] | None
) -> dict[str, Any] | None:
    a = mod.leer(guardado)
    if a is None:
        return None
    diff: list[dict[str, Any]] = []
    aplicable, motivo = a.aplicable, None
    if not a.aplicable:
        motivo = "tipo"
    elif borrador is None:
        aplicable, motivo = False, "sin_borrador"
    else:
        try:
            _, diff = mod.preparar_arreglo(borrador[0], a)
        except mod.ArregloNoAplicable:
            aplicable, motivo = False, "tipo"
    return {
        **mod.a_json(a),
        "aplicable": aplicable and aplicado_en is None,
        "motivo_no_aplicable": motivo if aplicado_en is None else "ya_aplicado",
        "diff": diff,
    }


def _intento(r: AgentTestResult, borrador: tuple[dict[str, Any], int] | None) -> dict[str, Any]:
    return {
        "result_id": r.id,
        "intento": r.intento,
        "estado": r.estado,
        "fin": r.fin,
        "criterio_incumplido": r.criterio_incumplido,
        "razon": r.razon,
        "transcripcion": [
            {"quien": "cliente" if t.get("quien") == "cliente" else "agente", "texto": str(t.get("texto", ""))}
            for t in (r.transcripcion or [])
            if isinstance(t, dict)
        ],
        "tools_llamadas": list(r.tools_llamadas or []),
        "arreglo": _vista_del_arreglo(r.arreglo, r.arreglo_aplicado_en, borrador),
        "arreglo_aplicado_en": r.arreglo_aplicado_en,
        "juez_equivocado": bool(r.juez_equivocado),
    }


async def informe(tenant_id: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    """El informe de la corrida. 404 si no es de este agente y negocio."""
    async with tenant_scope(tenant_id), get_session() as session:
        corrida = await _corrida(session, tenant_id, agent_id, run_id)
        resultados = list(
            (
                await session.execute(
                    select(AgentTestResult)
                    .where(
                        AgentTestResult.tenant_id == tenant_id,
                        AgentTestResult.run_id == run_id,
                        AgentTestResult.deleted_at.is_(None),
                    )
                    .order_by(AgentTestResult.created_at, AgentTestResult.intento)
                )
            ).scalars()
        )
        pedidos = [uuid.UUID(str(i)) for i in (corrida.version_probada or {}).get("escenarios", [])]
        titulos = {
            c.id: c.titulo
            for c in (
                await session.execute(
                    select(AgentTestCase).where(AgentTestCase.tenant_id == tenant_id, AgentTestCase.id.in_(pedidos))
                )
            ).scalars()
        }
        borrador = await _borrador(session, tenant_id, agent_id)

    por_caso: dict[uuid.UUID, list[dict[str, Any]]] = {i: [] for i in pedidos}
    for r in resultados:
        por_caso.setdefault(r.case_id, []).append(_intento(r, borrador))
    pasan = sum(1 for r in resultados if r.estado == "pasa")
    marcadas = sum(1 for r in resultados if r.estado == "falla" and r.juez_equivocado)
    fallan = sum(1 for r in resultados if r.estado == "falla" and not r.juez_equivocado)
    sin_probar = sum(1 for r in resultados if r.estado == "no_se_pudo_probar")
    reales = Decimal(corrida.creditos_reales)
    de_cuota = min(reales, Decimal(corrida.creditos_de_cuota))  # lo gastado que la cuota gratis cubre
    version = corrida.version_probada or {}
    return {
        "id": corrida.id,
        "agent_id": corrida.agent_id,
        "estado": corrida.estado,
        "k": corrida.k,
        "reintento_de": version.get("reintento_de"),
        "version_probada": (version.get("version") or {}).get("tipo"),
        "iniciada_en": corrida.iniciada_en,
        "terminada_en": corrida.terminada_en,
        "creditos_estimados": float(corrida.creditos_estimados),
        "creditos_reales": float(reales),
        "creditos_reales_de_cuota": float(de_cuota),
        "creditos_reales_del_saldo": float(reales - de_cuota),
        "tope_creditos": float(corrida.tope_creditos),
        "progreso": {"hechos": len(resultados), "total": len(pedidos) * int(corrida.k)},
        "resumen": {
            "pasan": pasan,
            "fallan": fallan,
            "no_se_pudo_probar": sin_probar,
            "juez_equivocado": marcadas,
            "porcentaje": porcentaje(pasan, fallan),
        },
        "borrador_lock_version": None if borrador is None else borrador[1],
        "escenarios": [
            {"case_id": i, "titulo": titulos.get(i, ""), "intentos": por_caso.get(i, [])} for i in por_caso
        ],
    }


# ----------------------------------------------------------------------------------------- un resultado del agente
async def _resultado(
    session: Any, tenant_id: uuid.UUID, agent_id: uuid.UUID, result_id: uuid.UUID, *, bloquear: bool = False
) -> AgentTestResult:
    """El resultado, solo si su corrida es de este agente y negocio (404 sin eco si no)."""
    consulta = (
        select(AgentTestResult)
        .join(
            AgentTestRun,
            (AgentTestRun.tenant_id == AgentTestResult.tenant_id) & (AgentTestRun.id == AgentTestResult.run_id),
        )
        .where(
            AgentTestResult.tenant_id == tenant_id,
            AgentTestResult.id == result_id,
            AgentTestResult.deleted_at.is_(None),
            AgentTestRun.agent_id == agent_id,
            AgentTestRun.deleted_at.is_(None),
        )
    )
    if bloquear:
        consulta = consulta.with_for_update(of=AgentTestResult)
    fila = (await session.execute(consulta)).scalar_one_or_none()
    if fila is None:
        raise _no_encontrado("resultado_no_encontrado")
    return fila


async def vista_previa(tenant_id: uuid.UUID, agent_id: uuid.UUID, result_id: uuid.UUID) -> dict[str, Any]:
    """El arreglo de un resultado con su diff contra el borrador de ahora y el `lock_version` que hay que confirmar."""
    async with tenant_scope(tenant_id), get_session() as session:
        r = await _resultado(session, tenant_id, agent_id, result_id)
        borrador = await _borrador(session, tenant_id, agent_id)
    vista = _vista_del_arreglo(r.arreglo, r.arreglo_aplicado_en, borrador)
    if vista is None:
        raise ErrorPruebas(409, "sin_arreglo", "Este resultado no tiene un arreglo sugerido.")
    return {**vista, "borrador_lock_version": None if borrador is None else borrador[1]}


def _campos(issues: list[Any]) -> dict[str, str]:
    campos: dict[str, str] = {}
    for issue in issues:
        campos[issue.path] = f"{campos[issue.path]} {issue.message}" if issue.path in campos else issue.message
    return campos


async def aplicar(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    result_id: uuid.UUID,
    *,
    lock_version: int,
    actor_id: uuid.UUID,
    actor: str,
    catalogo: Any,
) -> dict[str, Any]:
    """Escribe el arreglo en el borrador (sin publicar) con la confirmación del dueño (`lock_version`). Ver el módulo."""
    from core.agents import versiones

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            r = await _resultado(session, tenant_id, agent_id, result_id)
            guardado, aplicado_en, estado = r.arreglo, r.arreglo_aplicado_en, r.estado
        if estado != "falla":
            raise ErrorPruebas(409, "resultado_no_es_falla", "Solo una prueba que falló tiene arreglo.")
        arreglo = mod.leer(guardado)
        if arreglo is None:
            raise ErrorPruebas(409, "sin_arreglo", "Este resultado no tiene un arreglo sugerido.")
        if aplicado_en is not None:
            raise ErrorPruebas(409, "arreglo_ya_aplicado", "Este arreglo ya se aplicó al borrador.")
        if not arreglo.aplicable:
            raise ErrorPruebas(
                409,
                "arreglo_no_aplicable",
                "Este arreglo no se aplica solo: edítalo tú en la sección del agente.",
                {"tipo": arreglo.tipo, "seccion": arreglo.seccion, "texto": arreglo.texto},
            )
        try:
            borrador = await versiones.obtener_borrador(tenant_id, agent_id)
        except versiones.AgenteNoEncontrado:
            raise ErrorPruebas(409, "sin_borrador", "Este agente no tiene borrador que editar.") from None
        if borrador.lock_version != lock_version:
            raise ErrorPruebas(
                409,
                "stale_lock_version",
                "Otra persona guardó este borrador antes: recárgalo y revisa el cambio.",
                {"lock_version": borrador.lock_version},
            )
        nuevo, cambios = mod.preparar_arreglo(dict(borrador.content or {}), arreglo)
        lock_final = borrador.lock_version
        if cambios:  # sin cambios = la regla ya estaba (reintento tras un corte): solo se marca como aplicado
            try:
                guardado_ok = await versiones.guardar_borrador(
                    tenant_id, agent_id, nuevo, lock_version, catalogo=catalogo, actor=actor_id
                )
            except versiones.ErroresDeContrato as exc:
                raise ErrorPruebas(
                    422,
                    "arreglo_invalido",
                    "El arreglo no es válido para el borrador de este agente.",
                    fields=_campos(exc.issues),
                ) from None
            except versiones.Conflicto as exc:
                if exc.lock_version is None:  # el agente se archivó mientras tanto
                    raise ErrorPruebas(409, "agente_archivado", str(exc)) from None
                raise ErrorPruebas(
                    409,
                    "stale_lock_version",
                    "Otra persona guardó este borrador antes: recárgalo y revisa el cambio.",
                    {"lock_version": exc.lock_version},
                ) from None
            lock_final = guardado_ok.lock_version
        ahora = datetime.now(UTC)
        async with get_session() as session:
            await session.execute(
                update(AgentTestResult)
                .where(
                    AgentTestResult.tenant_id == tenant_id,
                    AgentTestResult.id == result_id,
                    AgentTestResult.arreglo_aplicado_en.is_(None),
                )
                .values(arreglo_aplicado_en=ahora, updated_at=func.now())
            )
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=actor[:120],
                    action=ACCION_APLICAR,
                    input={
                        "result_id": str(result_id),
                        "agent_id": str(agent_id),
                        "tipo": arreglo.tipo,
                        "cambios": [c["ruta"] for c in cambios],
                    },
                )
            )
    log.info(ACCION_APLICAR, tenant_id=str(tenant_id), result_id=str(result_id), cambios=len(cambios))
    return {
        "result_id": result_id,
        "arreglo_aplicado_en": ahora,
        "borrador_lock_version": lock_final,
        "cambios": cambios,
        "publicado": False,
    }


async def marcar_juez_equivocado(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, result_id: uuid.UUID, valor: bool, *, actor: str
) -> AgentTestResult:
    """«Esto no es una falla»: el resultado sigue `falla` pero sale del porcentaje. Solo sobre una falla."""
    async with tenant_scope(tenant_id), get_session() as session:
        r = await _resultado(session, tenant_id, agent_id, result_id, bloquear=True)
        if r.estado != "falla":
            raise ErrorPruebas(409, "resultado_no_es_falla", "Solo una prueba que falló se puede marcar así.")
        if bool(r.juez_equivocado) != valor:
            r.juez_equivocado = valor
            r.updated_at = datetime.now(UTC)
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=actor[:120],
                    action=ACCION_JUEZ,
                    input={"result_id": str(result_id), "agent_id": str(agent_id), "juez_equivocado": valor},
                )
            )
            await session.flush()
        session.expunge(r)
        return r


# ------------------------------------------------------------------------------------------- volver a probar las fallidas
async def fallidas(tenant_id: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> list[uuid.UUID]:
    """Los escenarios de la corrida con alguna falla vigente (no `juez_equivocado`), sin repetir y en el orden en que
    corrieron, que siguen vivos y aprobados. 404 si la corrida no es de este agente; 409 `corrida_en_curso` si sigue
    activa; 422 `sin_fallidas` si no queda ninguno."""
    async with tenant_scope(tenant_id), get_session() as session:
        corrida = await _corrida(session, tenant_id, agent_id, run_id)
        if corrida.estado in _ACTIVAS:
            raise ErrorPruebas(409, "corrida_en_curso", "La corrida todavía no termina.", {"estado": corrida.estado})
        filas = (
            await session.execute(
                select(AgentTestResult.case_id)
                .join(
                    AgentTestCase,
                    (AgentTestCase.tenant_id == AgentTestResult.tenant_id) & (AgentTestCase.id == AgentTestResult.case_id),
                )
                .where(
                    AgentTestResult.tenant_id == tenant_id,
                    AgentTestResult.run_id == run_id,
                    AgentTestResult.deleted_at.is_(None),
                    AgentTestResult.estado == "falla",
                    AgentTestResult.juez_equivocado.is_(False),
                    AgentTestCase.agent_id == agent_id,
                    AgentTestCase.deleted_at.is_(None),
                    AgentTestCase.estado == "aprobado",
                )
                .order_by(AgentTestResult.created_at, AgentTestResult.intento)
            )
        ).scalars()
        ids = list(dict.fromkeys(filas))
    if not ids:
        raise ErrorPruebas(422, "sin_fallidas", "Esta corrida no tiene pruebas fallidas para repetir.")
    return ids


async def estimar_fallidas(tenant_id: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> Any:
    ids = await fallidas(tenant_id, agent_id, run_id)
    return await corridas.estimar(tenant_id, agent_id, ids)


async def crear_de_fallidas(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    k: int,
    confirmo_creditos: int,
    pedida_por: uuid.UUID | None,
    actor: str,
) -> AgentTestRun:
    ids = await fallidas(tenant_id, agent_id, run_id)
    return await corridas.crear(
        tenant_id,
        agent_id,
        ids,
        k=k,
        confirmo_creditos=confirmo_creditos,
        pedida_por=pedida_por,
        actor=actor,
        reintento_de=run_id,
    )


__all__ = [
    "aplicar",
    "crear_de_fallidas",
    "estimar_fallidas",
    "fallidas",
    "informe",
    "marcar_juez_equivocado",
    "porcentaje",
    "vista_previa",
]
