"""Flujo de atención de un agente (ADR-0195, SPEC-46, HU-AOS-74).

El flujo es un artefacto operativo del agente, **fuera** del AgentSpec v3: se guarda en
`agent_drafts.flow` y `agent_versions.flow` y se valida contra las acciones que el agente
puede ejecutar. La semilla sale de los `flow.json` de sus packs; las secciones del core
(`saludo` y `escalamiento`) no se guardan por agente: las aporta `resolve_flow`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.kernel.procedures.linter import Issue

MAX_SECTIONS = 20
MAX_TOOLS_PER_SECTION = 40
MAX_LABEL = 80
MAX_PURPOSE = 500
_SECTION_ID_RE = r"^[a-z0-9_]{1,40}$"
_TOOL_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")


class FlowInvalido(ValueError):
    """El JSON del flujo no tiene la forma esperada; `issues` usa las rutas de la API."""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("flujo inválido")
        self.issues = issues


class AgentFlowSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_SECTION_ID_RE)
    label: str = Field(min_length=1, max_length=MAX_LABEL)
    purpose: str = Field(default="", max_length=MAX_PURPOSE)
    tools: list[str] = Field(default_factory=list, max_length=MAX_TOOLS_PER_SECTION)

    @field_validator("tools")
    @classmethod
    def _tools(cls, value: list[str]) -> list[str]:
        salida: list[str] = []
        vistos: set[str] = set()
        for tool in value:
            if not isinstance(tool, str) or not _TOOL_RE.match(tool):
                raise ValueError(f"'{tool}' no es un nombre de acción válido")
            if tool not in vistos:
                vistos.add(tool)
                salida.append(tool)
        return salida


class AgentFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[AgentFlowSection] = Field(default_factory=list, max_length=MAX_SECTIONS)

    @field_validator("sections")
    @classmethod
    def _ids_unicos(cls, value: list[AgentFlowSection]) -> list[AgentFlowSection]:
        vistos: set[str] = set()
        for seccion in value:
            if seccion.id in vistos:
                raise ValueError(f"la sección '{seccion.id}' está repetida")
            vistos.add(seccion.id)
        return value


def normalizar(raw: Any) -> dict[str, Any]:
    """Valida la forma y devuelve el flujo canónico. Lanza `FlowInvalido` con rutas de la API."""
    try:
        modelo = AgentFlow.model_validate(raw)
    except ValidationError as exc:
        issues = [
            Issue(
                "flujo_invalido",
                ".".join(str(p) for p in error["loc"]) or "flow",
                str(error["msg"]),
            )
            for error in exc.errors()
        ]
        raise FlowInvalido(issues) from None
    return modelo.model_dump(mode="json")


def flow_hash(flow: Mapping[str, Any] | None) -> str | None:
    """sha256 del flujo canónico (`None` se conserva como `None`)."""
    if flow is None:
        return None
    canonico = json.dumps(flow, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonico.encode()).hexdigest()


def acciones_permitidas(acciones: Iterable[Any]) -> set[str]:
    """Acciones que el flujo puede nombrar: encendidas o concedidas por un procedimiento (D-67)."""
    return {a.tool for a in acciones if getattr(a, "enabled", False) or getattr(a, "por_superpoder", False)}


def validar_acciones(flow: Mapping[str, Any], permitidas: set[str]) -> list[Issue]:
    """Issues por cada acción que el agente no puede ejecutar. El flujo no se guarda a medias."""
    issues: list[Issue] = []
    for i, seccion in enumerate(flow.get("sections") or []):
        for j, tool in enumerate(seccion.get("tools") or []):
            if tool not in permitidas:
                issues.append(
                    Issue(
                        "accion_no_disponible",
                        f"sections[{i}].tools[{j}]",
                        f"El agente no puede ejecutar '{tool}'.",
                    )
                )
    return issues


def avisos_de_acciones(flow: Mapping[str, Any], permitidas: set[str]) -> list[str]:
    """Acciones nombradas por el flujo que hoy no están disponibles (aviso, no bloqueo de lectura)."""
    vistas: list[str] = []
    for seccion in flow.get("sections") or []:
        for tool in seccion.get("tools") or []:
            if tool not in permitidas and tool not in vistas:
                vistas.append(tool)
    return vistas


def _id_unico(base: str, usados: set[str]) -> str:
    limpio = re.sub(r"[^a-z0-9_]", "_", base.lower())[:40] or "seccion"
    candidato = limpio
    n = 2
    while candidato in usados:
        sufijo = f"_{n}"
        candidato = f"{limpio[: 40 - len(sufijo)]}{sufijo}"
        n += 1
    usados.add(candidato)
    return candidato


def semilla(packs: set[str], permitidas: set[str]) -> dict[str, Any]:
    """Flujo inicial del agente a partir de los `flow.json` de sus packs.

    Copia las secciones del pack una vez (no es herencia en vivo) y deja solo acciones que
    el agente puede ejecutar. Una sección que solo tenía acciones no disponibles se omite.
    """
    from core.agents.flow import CORE, all_pack_ids, compose_flow, load_core_flow, load_pack_flows

    secciones = compose_flow(
        load_core_flow(),
        load_pack_flows(sorted(packs)),
        [],
        all_defaults=load_pack_flows(all_pack_ids()),
    )
    usados: set[str] = set()
    salida: list[dict[str, Any]] = []
    for seccion in secciones:
        if seccion.pack == CORE:
            continue
        herramientas = [t for t in seccion.tools if t in permitidas]
        if seccion.tools and not herramientas:
            continue
        salida.append(
            {
                "id": _id_unico(seccion.id, usados),
                "label": seccion.label[:MAX_LABEL],
                "purpose": (seccion.purpose or "")[:MAX_PURPOSE],
                "tools": herramientas[:MAX_TOOLS_PER_SECTION],
            }
        )
    return {"sections": salida[:MAX_SECTIONS]}


__all__ = [
    "MAX_SECTIONS",
    "MAX_TOOLS_PER_SECTION",
    "AgentFlow",
    "AgentFlowSection",
    "FlowInvalido",
    "acciones_permitidas",
    "avisos_de_acciones",
    "flow_hash",
    "normalizar",
    "semilla",
    "validar_acciones",
]
