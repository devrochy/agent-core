"""General de coordinación (HU-AOS-52, ADR-0171 §5). Al publicar un especialista de una plantilla:

1. **Enlace A2A** General → especialista (`handoff`), idempotente: sin él el General no puede pasarle la
   conversación (`pasar_a_agente`). Siempre se escribe: es un permiso, no cambia lo que hace nadie.
2. **Recorte del General**, solo con la bandera `settings.agent_os.recortar_general` del negocio (apagada
   por defecto: decisión del dueño, D-SPEC-39-5). El General deja los superpoderes que el especialista ya
   tiene y, cuando de un pack no le queda ninguno, también las tools y los playbooks de ese pack.
   Se publica como versión nueva del General (y se ajusta su borrador); como el especialista, entra en la
   release candidata, que no se activa sola: la reversa es republicar la versión anterior del General.

Nada de esto toca a los negocios sin especialistas: el General conserva todo."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import text

from core.db.session import get_session
from core.presets import PlantillaAgente
from core.shared.tenant import get_current_tenant_id

log = structlog.get_logger(__name__)

BANDERA = "recortar_general"
PACK_SUSCRIPCIONES = "pack_subscriptions"


@dataclass
class ResultadoAcople:
    """Lo que hizo el acople. `general_actualizado` es el número de versión nueva del General, si hubo."""

    enlace_creado: bool = False
    general_actualizado: int | None = None
    quitado: dict[str, list[str]] = field(default_factory=dict)


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("coordinación: fuera del tenant_scope del negocio")


def _pack_de(procedimiento: str) -> str:
    return procedimiento.split(".", 1)[0]


def _tools_del_pack(pack: str, catalogo: Mapping[str, Any]) -> set[str]:
    """Tools que son del pack: las que declara (`owner`) y, para Suscripciones, las `sub_*` del core."""
    from core.presets.agentes import _es_tool_de_suscripcion

    return {
        nombre
        for nombre, info in catalogo.items()
        if info.owner == pack or (pack == PACK_SUSCRIPCIONES and _es_tool_de_suscripcion(nombre, info.owner))
    }


def _superpoderes_por_pack() -> dict[str, set[str]]:
    """Ids de superpoder (`pack.nombre`) que declaran las plantillas, por pack. Los demás procedimientos de
    un pack (descubrimiento, cotización, gestión de citas...) son del General de hoy, no un superpoder."""
    from core.agents.editor import keys_de_plantillas
    from core.presets import load_plantilla

    por_pack: dict[str, set[str]] = {}
    for key in sorted(keys_de_plantillas()):
        for pid in load_plantilla(key).procedimientos:
            por_pack.setdefault(_pack_de(pid), set()).add(pid)
    return por_pack


def recortar_general(
    general: Mapping[str, Any],
    *,
    procedimientos: set[str],
    plantilla: PlantillaAgente,
    catalogo: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Contenido `agentspec/3` del General sin lo que ya hace el especialista. Puro e idempotente.

    - `procedures`: se quitan los superpoderes que tiene el especialista (`procedimientos`, ids `pack.nombre`).
    - Un pack queda **cubierto** cuando al General no le queda ningún superpoder suyo (los de todas las
      plantillas). De un pack cubierto se quitan todos sus procedimientos, sus tools (y las `sub_*` si es
      Suscripciones), los playbooks que nombra la plantilla y los guardrails `required_tool` de una tool
      quitada. Un especialista «solo planes» no cubre Comercio: el General sigue con `tomar_pedido`.
    Devuelve `(contenido, quitado)`; `quitado` (`procedures`, `tools`, `playbooks`, `guardrails`) va
    vacío si no cambia nada."""
    nuevo = copy.deepcopy(dict(general))
    quitado: dict[str, list[str]] = {}

    refs = [r for r in nuevo.get("procedures") or [] if isinstance(r, dict)]
    fuera_procs = {str(r.get("id")) for r in refs if r.get("id") in procedimientos}
    universo = _superpoderes_por_pack()
    tiene_super = {
        str(r.get("id"))
        for r in refs
        if r.get("id") not in procedimientos and any(r.get("id") in ids for ids in universo.values())
    }
    cubiertos = sorted(p for p in {_pack_de(x) for x in procedimientos} if not any(_pack_de(x) == p for x in tiene_super))
    fuera_procs |= {str(r.get("id")) for r in refs if _pack_de(str(r.get("id"))) in cubiertos}
    if fuera_procs:
        nuevo["procedures"] = [r for r in refs if r.get("id") not in fuera_procs]
        quitado["procedures"] = sorted(fuera_procs)

    tools_fuera: set[str] = set()
    for pack in cubiertos:
        tools_fuera |= _tools_del_pack(pack, catalogo)
    tools = [b for b in nuevo.get("tools") or [] if isinstance(b, dict)]
    quitan = sorted(str(b.get("tool")) for b in tools if b.get("tool") in tools_fuera)
    if quitan:
        nuevo["tools"] = [b for b in tools if b.get("tool") not in tools_fuera]
        quitado["tools"] = quitan
    if cubiertos:
        playbooks = [p for p in nuevo.get("playbooks") or [] if isinstance(p, dict)]
        fuera = sorted(str(p.get("name")) for p in playbooks if p.get("name") in plantilla.playbooks)
        if fuera:
            nuevo["playbooks"] = [p for p in playbooks if p.get("name") not in plantilla.playbooks]
            quitado["playbooks"] = fuera
    if quitan:
        guardias = [g for g in nuevo.get("guardrails") or [] if isinstance(g, dict)]
        sueltos = sorted(
            str(g.get("tool")) for g in guardias if g.get("kind") == "required_tool" and g.get("tool") in quitan
        )
        if sueltos:
            nuevo["guardrails"] = [
                g for g in guardias if not (g.get("kind") == "required_tool" and g.get("tool") in quitan)
            ]
            quitado["guardrails"] = sueltos
    return nuevo, quitado


async def bandera_encendida(tenant_id: uuid.UUID) -> bool:
    """`settings.agent_os.recortar_general` del negocio (solo la edita el master; apagada por defecto)."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        valor = (
            await session.execute(
                text("SELECT settings->'agent_os'->>:k FROM tenants WHERE id = :t"), {"k": BANDERA, "t": tenant_id}
            )
        ).scalar_one_or_none()
    return valor == "true"


async def fijar_bandera(tenant_id: uuid.UUID, encendida: bool, *, actor: str) -> bool:
    """Enciende o apaga `settings.agent_os.recortar_general` con un UPDATE atómico dentro de
    `settings.agent_os` (no pisa el resto de `settings`) y lo audita como `agent_os_recorte_general`.
    Devuelve si cambió algo (sin cambio no deja auditoría). Lo usan el alta (negocios nuevos) y el
    comando `agent-core db recorte-general` (el master, negocios existentes)."""
    from core.db.models import AuditLog

    _exigir_scope(tenant_id)
    valor = "true" if encendida else "false"
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "WITH antes AS (SELECT settings->'agent_os'->>:k AS v FROM tenants WHERE id = :t AND deleted_at IS NULL), "
                    "cambio AS (UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{agent_os}', "
                    "coalesce(settings->'agent_os', '{}'::jsonb) || jsonb_build_object(cast(:k as text), cast(:b as boolean))) "
                    "WHERE id = :t AND deleted_at IS NULL RETURNING 1) "
                    "SELECT (SELECT v FROM antes), (SELECT count(*) FROM cambio)"
                ),
                {"k": BANDERA, "t": tenant_id, "b": encendida},
            )
        ).one()
        if not fila[1]:
            raise LookupError(str(tenant_id))
        cambio = fila[0] != valor
        if cambio:
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=actor[:120],
                    action="agent_os_recorte_general",
                    input={"recortar_general": encendida, "antes": fila[0]},
                )
            )
    if cambio:
        log.info("agent_os_recorte_general", tenant_id=str(tenant_id), recortar_general=encendida, actor=actor)
    return cambio


async def estado_de_bandera(tenant_id: uuid.UUID) -> bool | None:
    """Valor guardado de la bandera (None si el negocio nunca la fijó)."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        valor = (
            await session.execute(
                text("SELECT settings->'agent_os'->>:k FROM tenants WHERE id = :t"), {"k": BANDERA, "t": tenant_id}
            )
        ).scalar_one_or_none()
    return None if valor is None else valor == "true"


async def _enlazar(
    tenant_id: uuid.UUID, general_id: uuid.UUID, agente_id: uuid.UUID, admin_id: uuid.UUID | None
) -> bool:
    """Agrega General → agente al grafo vigente; sin cambios si ya existe. True si publicó versión."""
    from core.agents import grafo_a2a

    vigente = await grafo_a2a.vigente(tenant_id)
    enlaces = list(vigente.enlaces) if vigente else []
    if any(e.from_agent_id == general_id and e.to_agent_id == agente_id for e in enlaces):
        return False
    enlaces.append(grafo_a2a.EnlaceA2A(from_agent_id=general_id, to_agent_id=agente_id, modes=["handoff"]))
    _, creada = await grafo_a2a.publicar(tenant_id, enlaces, created_by=admin_id)
    return creada


async def _contenido_vigente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any] | None:
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT content FROM agent_versions WHERE tenant_id = :t AND agent_id = :a "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).first()
    return dict(fila[0]) if fila is not None and isinstance(fila[0], dict) else None


async def acoplar_especialista(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    catalogo: Mapping[str, Any],
    admin_id: uuid.UUID | None,
) -> ResultadoAcople:
    """Enlaza al General con el especialista recién publicado y, con la bandera, lo recorta. No hace
    nada si el agente no es de una plantilla o el negocio no tiene General. Idempotente."""
    from core.agents import versiones
    from core.agents.tolerante import KEY_GENERAL
    from core.presets import PresetError, load_plantilla

    _exigir_scope(tenant_id)
    agente = await versiones.obtener_agente(tenant_id, agent_id)
    try:
        plantilla = load_plantilla(agente.key)
    except PresetError:
        return ResultadoAcople()
    async with get_session() as session:
        general_id = (
            await session.execute(
                text("SELECT id FROM agents WHERE tenant_id = :t AND key = :k AND deleted_at IS NULL LIMIT 1"),
                {"t": tenant_id, "k": KEY_GENERAL},
            )
        ).scalar_one_or_none()
    if general_id is None or general_id == agent_id:
        return ResultadoAcople()

    resultado = ResultadoAcople(enlace_creado=await _enlazar(tenant_id, general_id, agent_id, admin_id))
    if not await bandera_encendida(tenant_id):
        return resultado

    del_especialista = await _contenido_vigente(tenant_id, agent_id) or {}
    procs = {
        str(r["id"])
        for r in del_especialista.get("procedures") or []
        if isinstance(r, dict) and r.get("id") in plantilla.procedimientos
    }
    if not procs:
        return resultado
    vigente = await _contenido_vigente(tenant_id, general_id)
    if vigente is None:
        return resultado
    nuevo, quitado = recortar_general(vigente, procedimientos=procs, plantilla=plantilla, catalogo=catalogo)
    if quitado:
        version, _ = await versiones.publicar(
            tenant_id,
            general_id,
            catalogo=catalogo,  # type: ignore[arg-type]
            actor=admin_id,
            contenido=nuevo,
            notes=f"General de coordinación: ahora atiende {agente.key}",
        )
        resultado.general_actualizado = version.version
        resultado.quitado = quitado
        log.info("general_recortado", tenant_id=str(tenant_id), agente=agente.key, quitado=quitado)
    await _ajustar_borrador(tenant_id, general_id, procs, plantilla, catalogo, admin_id)
    return resultado


async def _ajustar_borrador(
    tenant_id: uuid.UUID,
    general_id: uuid.UUID,
    procs: set[str],
    plantilla: PlantillaAgente,
    catalogo: Mapping[str, Any],
    admin_id: uuid.UUID | None,
) -> None:
    """El borrador del General recibe el mismo recorte, para que seguir editándolo no devuelva lo quitado
    (los cambios sin publicar del dueño se conservan)."""
    from core.agents import versiones

    try:
        borrador = await versiones.obtener_borrador(tenant_id, general_id)
    except versiones.AgenteNoEncontrado:
        return
    nuevo, quitado = recortar_general(borrador.content, procedimientos=procs, plantilla=plantilla, catalogo=catalogo)
    if quitado:
        await versiones.guardar_borrador(
            tenant_id,
            general_id,
            nuevo,
            borrador.lock_version,
            catalogo=catalogo,  # type: ignore[arg-type]
            actor=admin_id,
        )
