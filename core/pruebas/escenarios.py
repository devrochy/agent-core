"""Escenarios de prueba de un agente: CRUD con borrado lógico (HU-AOS-101, ADR-0212 §1).

Un escenario (`agent_test_cases`) lo propone la IA (`origen='ia'`, nace `borrador`) o lo escribe el dueño
(`origen='manual'`). Solo los `aprobado` se corren. Todo lo que trae es dato del dueño o de la IA, nunca
instrucción (invariante 7): el objetivo y los criterios se tratan como texto.

Reglas de aplicación (las de forma las pone la base, migración 0160):
- Máx. 50 escenarios vivos por agente (`tope_de_escenarios`), contados bajo un advisory lock del agente para que
  dos pedidos a la vez no pasen del tope.
- Título único por agente sin distinguir mayúsculas entre los vivos (`titulo_repetido`).
- Solo tools que el agente tiene (`tool_desconocida`) y ninguna en las dos listas (`tools_en_conflicto`).
- Aprobar exige poder juzgar el escenario: al menos un criterio o una tool esperada o prohibida
  (`escenario_incompleto`).

El agente se busca con el `tenant_id` de la sesión además de la RLS: uno de otro negocio es 404 sin eco.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from core.db.models import AgentRecord, AgentTestCase, AgentTestRun, utcnow
from core.db.session import get_session

TOPE_ESCENARIOS = 50
TOPE_POR_PROPUESTA = 10
ORIGENES = ("ia", "manual")
ESTADOS = ("borrador", "aprobado")


class ErrorPruebas(Exception):
    """Error de dominio de las pruebas del agente: `status` HTTP, `code` estable y mensaje para el dueño."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def dato(c: AgentTestCase) -> dict[str, Any]:
    return {
        "id": c.id,
        "agent_id": c.agent_id,
        "titulo": c.titulo,
        "objetivo_cliente": c.objetivo_cliente,
        "datos_cliente": dict(c.datos_cliente or {}),
        "criterios": list(c.criterios or []),
        "tools_esperadas": list(c.tools_esperadas or []),
        "tools_prohibidas": list(c.tools_prohibidas or []),
        "origen": c.origen,
        "estado": c.estado,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


async def exigir_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    async with get_session() as session:
        existe = (
            await session.execute(
                select(AgentRecord.id).where(
                    AgentRecord.tenant_id == tenant_id,
                    AgentRecord.id == agent_id,
                    AgentRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if existe is None:
        raise ErrorPruebas(404, "not_found", "Agente no encontrado.")


async def tools_del_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, str]:
    """Las tools que el agente tiene hoy (borrador, o la última versión si no hay) y qué hace cada una.

    Son las que tienen binding encendido en el contenido más las que concede un superpoder que declara.
    """
    from core.agents import editor
    from core.agents import habilidades_del_agente as hda
    from core.mcp.host import mcp_host

    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT COALESCE(d.content, (SELECT v.content FROM agent_versions v "
                    "  WHERE v.tenant_id = a.tenant_id AND v.agent_id = a.id "
                    "  ORDER BY v.version DESC LIMIT 1)) "
                    "FROM agents a LEFT JOIN agent_drafts d "
                    "  ON d.tenant_id = a.tenant_id AND d.agent_id = a.id AND d.deleted_at IS NULL "
                    "WHERE a.tenant_id = :t AND a.id = :a AND a.deleted_at IS NULL"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).first()
    if fila is None:
        raise ErrorPruebas(404, "not_found", "Agente no encontrado.")
    contenido = dict(fila[0]) if isinstance(fila[0], Mapping) else {}
    packs = await hda.packs_del_agente(
        tenant_id, agent_id, await editor.habilitados_del_negocio(tenant_id)
    )
    descripciones = {t.name: (t.description or "") for t in mcp_host.get_tools()}
    return {
        a.tool: descripciones.get(a.tool, "")
        for a in hda.acciones(contenido, packs)
        if (a.en_el_agente and a.enabled) or a.por_superpoder
    }


def _limpiar(valores: list[str]) -> list[str]:
    """Sin vacíos ni repetidos, en el orden en que llegaron."""
    vistos: dict[str, None] = {}
    for v in valores:
        v = v.strip()
        if v:
            vistos.setdefault(v, None)
    return list(vistos)


def _validar(campos: Mapping[str, Any], tools: Mapping[str, str], *, verificar_tools: bool) -> None:
    esperadas, prohibidas = set(campos["tools_esperadas"]), set(campos["tools_prohibidas"])
    if esperadas & prohibidas:
        raise ErrorPruebas(
            422, "tools_en_conflicto", "Una tool no puede ser esperada y prohibida a la vez."
        )
    if verificar_tools and (desconocidas := sorted((esperadas | prohibidas) - set(tools))):
        raise ErrorPruebas(
            422,
            "tool_desconocida",
            f"El agente no tiene esta(s) tool(s): {', '.join(desconocidas)}.",
        )
    if campos["estado"] == "aprobado" and not (campos["criterios"] or esperadas or prohibidas):
        raise ErrorPruebas(
            422,
            "escenario_incompleto",
            "Para aprobarlo, el escenario necesita al menos un criterio o una tool esperada o prohibida.",
        )


async def _bloquear(session: Any, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Serializa las escrituras de escenarios de un agente hasta el commit (tope de 50 y título único)."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"pruebas_escenarios:{tenant_id}:{agent_id}"},
    )


async def _vivos(session: Any, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[AgentTestCase]:
    return list(
        (
            await session.execute(
                select(AgentTestCase)
                .where(
                    AgentTestCase.tenant_id == tenant_id,
                    AgentTestCase.agent_id == agent_id,
                    AgentTestCase.deleted_at.is_(None),
                )
                .order_by(AgentTestCase.created_at, AgentTestCase.id)
            )
        ).scalars()
    )


async def titulos_vivos(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[str]:
    async with get_session() as session:
        return [c.titulo for c in await _vivos(session, tenant_id, agent_id)]


async def listar(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    """Los escenarios vivos del agente y un resumen de su última corrida (`null` si nunca corrió)."""
    await exigir_agente(tenant_id, agent_id)
    async with get_session() as session:
        casos = await _vivos(session, tenant_id, agent_id)
        corrida = (
            await session.execute(
                select(AgentTestRun)
                .where(
                    AgentTestRun.tenant_id == tenant_id,
                    AgentTestRun.agent_id == agent_id,
                    AgentTestRun.deleted_at.is_(None),
                )
                .order_by(AgentTestRun.created_at.desc(), AgentTestRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return {
        "escenarios": [dato(c) for c in casos],
        "tope": TOPE_ESCENARIOS,
        "ultima_corrida": None
        if corrida is None
        else {
            "id": corrida.id,
            "estado": corrida.estado,
            "k": corrida.k,
            "iniciada_en": corrida.iniciada_en,
            "terminada_en": corrida.terminada_en,
        },
    }


async def crear(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    campos: Mapping[str, Any],
    *,
    origen: str,
    created_by: uuid.UUID | None,
) -> dict[str, Any]:
    """Escribe un escenario del dueño (`manual`). `campos`: lo del cuerpo ya validado por forma."""
    assert origen in ORIGENES
    await exigir_agente(tenant_id, agent_id)
    valores = _normalizar(campos)
    _validar(valores, await tools_del_agente(tenant_id, agent_id), verificar_tools=True)
    try:
        async with get_session() as session:
            await _bloquear(session, tenant_id, agent_id)
            vivos = await _vivos(session, tenant_id, agent_id)
            _exigir_cupo(vivos, 1, valores["titulo"])
            caso = AgentTestCase(
                tenant_id=tenant_id,
                agent_id=agent_id,
                origen=origen,
                created_by=created_by,
                **valores,
            )
            session.add(caso)
            await session.flush()
            return dato(caso)
    except IntegrityError as exc:  # el índice único ganó una carrera con otro pedido
        raise _repetido() from exc


async def guardar_propuestos(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    propuestos: Sequence[Mapping[str, Any]],
    *,
    created_by: uuid.UUID | None,
) -> list[dict[str, Any]]:
    """Guarda en `borrador` lo que la IA propuso (ya depurado). Salta los que chocan con el tope o el título."""
    guardados: list[AgentTestCase] = []
    async with get_session() as session:
        await _bloquear(session, tenant_id, agent_id)
        vivos = await _vivos(session, tenant_id, agent_id)
        titulos = {c.titulo.casefold() for c in vivos}
        cupo = TOPE_ESCENARIOS - len(vivos)
        for p in propuestos:
            if cupo <= 0:
                break
            if p["titulo"].casefold() in titulos:
                continue
            caso = AgentTestCase(
                tenant_id=tenant_id,
                agent_id=agent_id,
                origen="ia",
                estado="borrador",
                created_by=created_by,
                **{k: p[k] for k in _CAMPOS if k != "estado"},
            )
            session.add(caso)
            titulos.add(p["titulo"].casefold())
            cupo -= 1
            guardados.append(caso)
        await session.flush()
        return [dato(c) for c in guardados]


async def cupo(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> int:
    """Cuántos escenarios más caben en el agente."""
    async with get_session() as session:
        n = (
            await session.execute(
                select(func.count())
                .select_from(AgentTestCase)
                .where(
                    AgentTestCase.tenant_id == tenant_id,
                    AgentTestCase.agent_id == agent_id,
                    AgentTestCase.deleted_at.is_(None),
                )
            )
        ).scalar_one()
    return max(0, TOPE_ESCENARIOS - int(n))


async def actualizar(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, case_id: uuid.UUID, cambios: Mapping[str, Any]
) -> dict[str, Any]:
    """Edita o aprueba un escenario vivo del agente. `cambios`: solo los campos enviados."""
    await exigir_agente(tenant_id, agent_id)
    tools = await tools_del_agente(tenant_id, agent_id)
    try:
        async with get_session() as session:
            await _bloquear(session, tenant_id, agent_id)
            caso = await _vivo(session, tenant_id, agent_id, case_id)
            valores = _normalizar({**_campos_de(caso), **cambios})
            # Una tool que ya tenía y el agente perdió no impide aprobar o corregir otra cosa; las nuevas sí.
            previas = set(caso.tools_esperadas or []) | set(caso.tools_prohibidas or [])
            nuevas = (set(valores["tools_esperadas"]) | set(valores["tools_prohibidas"])) - previas
            _validar(valores, tools, verificar_tools=False)
            if desconocidas := sorted(nuevas - set(tools)):
                raise ErrorPruebas(
                    422,
                    "tool_desconocida",
                    f"El agente no tiene esta(s) tool(s): {', '.join(desconocidas)}.",
                )
            otros = [c for c in await _vivos(session, tenant_id, agent_id) if c.id != case_id]
            if valores["titulo"].casefold() in {c.titulo.casefold() for c in otros}:
                raise _repetido()
            for k, v in valores.items():
                setattr(caso, k, v)
            await session.flush()
            return dato(caso)
    except IntegrityError as exc:
        raise _repetido() from exc


async def borrar(tenant_id: uuid.UUID, agent_id: uuid.UUID, case_id: uuid.UUID) -> None:
    """Borrado lógico. Los resultados de corridas pasadas siguen apuntando al escenario."""
    await exigir_agente(tenant_id, agent_id)
    async with get_session() as session:
        await _vivo(session, tenant_id, agent_id, case_id)
        await session.execute(
            update(AgentTestCase)
            .where(AgentTestCase.tenant_id == tenant_id, AgentTestCase.id == case_id)
            .values(deleted_at=utcnow())
        )


_CAMPOS = (
    "titulo",
    "objetivo_cliente",
    "datos_cliente",
    "criterios",
    "tools_esperadas",
    "tools_prohibidas",
    "estado",
)


def _campos_de(c: AgentTestCase) -> dict[str, Any]:
    return {k: getattr(c, k) for k in _CAMPOS}


def _normalizar(campos: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "titulo": str(campos["titulo"]).strip(),
        "objetivo_cliente": str(campos["objetivo_cliente"]).strip(),
        "datos_cliente": dict(campos.get("datos_cliente") or {}),
        "criterios": _limpiar(list(campos.get("criterios") or [])),
        "tools_esperadas": _limpiar(list(campos.get("tools_esperadas") or [])),
        "tools_prohibidas": _limpiar(list(campos.get("tools_prohibidas") or [])),
        "estado": campos.get("estado") or "borrador",
    }


def _repetido() -> ErrorPruebas:
    return ErrorPruebas(
        409, "titulo_repetido", "Ya hay un escenario con ese título en este agente."
    )


def _exigir_cupo(vivos: list[AgentTestCase], nuevos: int, titulo: str) -> None:
    if len(vivos) + nuevos > TOPE_ESCENARIOS:
        raise ErrorPruebas(
            422,
            "tope_de_escenarios",
            f"Un agente puede tener hasta {TOPE_ESCENARIOS} escenarios. Borra alguno para agregar otro.",
        )
    if titulo.casefold() in {c.titulo.casefold() for c in vivos}:
        raise _repetido()


async def _vivo(
    session: Any, tenant_id: uuid.UUID, agent_id: uuid.UUID, case_id: uuid.UUID
) -> AgentTestCase:
    caso = (
        await session.execute(
            select(AgentTestCase).where(
                AgentTestCase.tenant_id == tenant_id,
                AgentTestCase.agent_id == agent_id,
                AgentTestCase.id == case_id,
                AgentTestCase.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if caso is None:
        raise ErrorPruebas(404, "not_found", "Escenario no encontrado.")
    return caso
