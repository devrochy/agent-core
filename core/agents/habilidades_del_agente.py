"""Lo que el panel muestra y edita dentro de un agente (ADR-0190 §8-§9, HU-AOS-64).

- **Acciones**: las tools de las habilidades del agente, agrupadas por lo que hacen (`BaseModule.grupos_de_acciones`;
  solo la clave, la etiqueta la pone el panel) y con el binding que tiene el borrador del agente (`ToolBindingV3`:
  encendida, tope y confirmación). Editar una acción es editar el borrador (`/agent-editor/{id}/draft`), como hoy.
- **Habilidades transversales**: Pagos en línea, Conocimiento y Captar leads. Pagos guarda en su instalación la
  `cuenta_id` elegida; Captar leads no es un pack (invariante 5): es el grupo de acciones `leads`.

Un agente sin instalaciones propias muestra las habilidades del negocio (hasta la resiembra, HU-AOS-65).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from core.agents.instalaciones_agente import (
    config_del_agente,
    desinstalar_de_agente,
    guardar_config_del_agente,
    instaladas_en_agente,
    instalar_en_agente,
)
from core.mcp.host import mcp_host
from core.plugin_manager import plugin_manager

OTRAS = "otras"
PAGOS = "pack_payments"
CONOCIMIENTO = "pack_knowledge"
# Tools del core con dueño de dominio (las `sub_*` de suscripciones viven en `core/subscriptions/tools.py`).
_GRUPOS_DEL_CORE = (("sub_", "suscripciones"),)


@dataclass(frozen=True)
class Accion:
    tool: str
    pack_id: str
    grupo: str
    enabled: bool
    en_el_agente: bool  # tiene binding en el borrador
    # La concede un superpoder (procedimiento) que el agente declara (D-67): corre en su etapa aunque no tenga binding.
    por_superpoder: bool
    max_calls_per_conversation: int | None
    requires_confirmation: bool


@dataclass(frozen=True)
class HabilidadDelAgente:
    pack_id: str
    instalada: bool
    status: str | None
    config: dict[str, Any]


class HabilidadNoDisponible(LookupError):
    pass


def _grupo(nombre: str, pack_id: str) -> str:
    modulo = plugin_manager.get(pack_id) if pack_id else None
    grupos = getattr(modulo, "grupos_de_acciones", None) or {}
    if nombre in grupos:
        return grupos[nombre]
    for prefijo, grupo in _GRUPOS_DEL_CORE:
        if nombre.startswith(prefijo):
            return grupo
    return OTRAS


async def packs_del_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID, habilitados: set[str]) -> set[str]:
    propias = set(await instaladas_en_agente(tenant_id, agent_id, solo_activas=False))
    return propias or set(habilitados)


def _de_superpoderes(contenido: dict[str, Any], packs: set[str]) -> frozenset[str]:
    """Acciones que nombran los procedimientos que el borrador declara (D-67: declarar un procedimiento concede sus
    acciones), entre los disponibles para esas habilidades."""
    from core.kernel.opciones.lint import acciones_de_procedimientos
    from core.kernel.procedures import registro

    declarados = {
        str(p.get("id")) for p in contenido.get("procedures") or [] if isinstance(p, dict) and p.get("id")
    }
    if not declarados:
        return frozenset()
    disponibles = registro.disponibles(packs)
    return acciones_de_procedimientos(p for pid, p in disponibles.items() if pid in declarados)


def acciones(contenido: dict[str, Any], packs: set[str]) -> list[Accion]:
    """Las acciones de `packs` con el binding del borrador `contenido` (`agentspec/3`), ordenadas por grupo."""
    concedidas = _de_superpoderes(contenido, packs)
    bindings = {
        str(b.get("tool")): b for b in contenido.get("tools") or [] if isinstance(b, dict) and b.get("tool")
    }
    salida: list[Accion] = []
    for tool in mcp_host.get_tools():
        pack_id = tool.plugin_id or ""
        de_suscripciones = tool.name.startswith("sub_")
        if pack_id not in packs and not (de_suscripciones and "pack_subscriptions" in packs):
            continue
        binding = bindings.get(tool.name)
        salida.append(
            Accion(
                tool=tool.name,
                pack_id="pack_subscriptions" if de_suscripciones else pack_id,
                grupo=_grupo(tool.name, pack_id),
                enabled=bool(binding.get("enabled", True)) if binding else False,
                en_el_agente=binding is not None,
                por_superpoder=tool.name in concedidas,
                max_calls_per_conversation=binding.get("maxCallsPerConversation") if binding else None,
                requires_confirmation=bool(binding.get("requiresConfirmation", False)) if binding else False,
            )
        )
    return sorted(salida, key=lambda a: (a.grupo, a.tool))


def _sin_secretos(config: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (config or {}).items() if "secret" not in k and "key" not in k}


async def transversales(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[HabilidadDelAgente]:
    instaladas = set(await instaladas_en_agente(tenant_id, agent_id, solo_activas=False))
    activas = set(await instaladas_en_agente(tenant_id, agent_id))
    salida = []
    for pack_id in sorted(m.id for m in plugin_manager.scan() if getattr(m, "tipo", "") == "transversal"):
        instalada = pack_id in instaladas
        salida.append(
            HabilidadDelAgente(
                pack_id=pack_id,
                instalada=instalada,
                status=("active" if pack_id in activas else "pending_config") if instalada else None,
                config=_sin_secretos(await config_del_agente(tenant_id, agent_id, pack_id)) if instalada else {},
            )
        )
    return salida


async def configurar(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    pack_id: str,
    *,
    instalada: bool,
    config: dict[str, Any] | None,
    actor: uuid.UUID | None,
) -> HabilidadDelAgente:
    """Instala, quita o configura una habilidad transversal del agente. Pagos exige una `cuenta_id` del negocio."""
    modulo = plugin_manager.get(pack_id)
    if modulo is None or getattr(modulo, "tipo", "") != "transversal":
        raise HabilidadNoDisponible(pack_id)
    if not instalada:
        await desinstalar_de_agente(tenant_id, agent_id, pack_id, actor=actor)
        return HabilidadDelAgente(pack_id=pack_id, instalada=False, status=None, config={})
    # El negocio tiene la habilidad si algún agente la tiene: crons, panel y `payment_registry` leen la del negocio.
    from core.agents.habilitacion import asegurar_habilidades

    await asegurar_habilidades(tenant_id, [pack_id], actor=actor)
    requiere = pack_id == PAGOS
    await instalar_en_agente(tenant_id, agent_id, pack_id, requiere_config=requiere, actor=actor)
    if config is not None:
        limpia = await _validar(pack_id, config)
        await guardar_config_del_agente(tenant_id, agent_id, pack_id, limpia)
    return next(h for h in await transversales(tenant_id, agent_id) if h.pack_id == pack_id)


async def _validar(pack_id: str, config: dict[str, Any]) -> dict[str, Any]:
    if pack_id == PAGOS:
        valor = config.get("cuenta_id")
        try:
            cuenta = uuid.UUID(str(valor))
        except (TypeError, ValueError):
            raise ValueError("cuenta_id") from None
        from core.payments import cuenta_existe

        if not await cuenta_existe(cuenta):
            raise ValueError("cuenta_id")
        return {"cuenta_id": str(cuenta)}
    modulo = plugin_manager.get(pack_id)
    esquema = getattr(modulo, "config_agente", None)
    if esquema is None:
        return {}
    return esquema.model_validate(config).model_dump(mode="json")


__all__ = [
    "Accion",
    "HabilidadDelAgente",
    "HabilidadNoDisponible",
    "acciones",
    "configurar",
    "packs_del_agente",
    "transversales",
]
