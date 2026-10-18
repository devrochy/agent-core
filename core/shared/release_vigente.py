"""Release del turno en curso (ADR-0117 §2 y §4, HU-AOS-14).

Con `tenants.aos_runtime = 'v3'` el kernel asigna a la conversación una release de su conexión
(`core.kernel.release_del_turno`) y deja aquí lo que el runtime lee de ella en vez del disco y de
`AppSettings`: los overrides de playbooks (ADR-0026), las secciones `tenant` del flujo
(ADR-0109), los campos de `AppSettings` que entran al prompt y el `episode.idleMinutes` del
KernelSpec. Con `legacy` la ranura queda vacía y todo se lee como siempre.
HU-AOS-15: también las versiones congeladas de los agentes (`agentes`, `foco`): el loop
ejecuta la del especialista en foco y el KernelSpec del Coordinador en vez del v2.

Módulo hoja (solo importa `core.shared.tenant`): lo leen `core.agents.playbooks`, `core.agents.flow` y
`core.kernel.tarjeta`, y así no se arma el ciclo agents → kernel → releases → agents.

La ranura la abre `con_release` (decorador del turno) y la cierra al terminar, también si el
turno falla: lo que fija un turno nunca lo ve el siguiente. Guarda de negocio: solo se devuelve
la release si es del negocio del `tenant_scope` activo.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from core.shared.tenant import get_current_tenant_id

# Campos de `AppSettings` que entran al prompt y congela la release (ADR-0117 §2). Lo
# operativo (horarios, pausa, debounce, `guardrail_fail_action`) sigue en caliente.
CAMPOS_DE_PROMPT = ("assistant_name", "business_description", "language", "brand_voice")


@dataclass(frozen=True)
class AgenteCongelado:
    """Un componente de la release: la instancia y la versión exacta de su agente, con el
    contenido congelado de la versión (`agentspec/3` o `kernelspec/1`). Datos planos: este
    módulo es hoja y no importa los contratos; `core/agents/spec_de_release.py` los valida."""

    instance_id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    role: str  # core | specialist | backoffice
    version_id: uuid.UUID
    version: int
    contract: str
    content: Mapping[str, Any]

    @property
    def write_namespaces(self) -> frozenset[str]:
        """`memory.writeNamespaces` de la versión (vacío para el Coordinador)."""
        memoria = self.content.get("memory")
        valores = memoria.get("writeNamespaces") if isinstance(memoria, Mapping) else None
        return frozenset(str(v) for v in valores) if isinstance(valores, list) else frozenset()


@dataclass(frozen=True)
class ArtefactosDeRelease:
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    release_id: uuid.UUID
    lane: str
    idle_minutes: int
    # HU-AOS-15: las versiones congeladas de los agentes de la release y el especialista en
    # foco del turno (None = la release no trae agentes cargados: el loop sigue con el v2).
    agentes: tuple[AgenteCongelado, ...] = ()
    foco: AgenteCongelado | None = None
    # Habilidades de la plataforma y plugins del negocio congelados en el manifiesto.
    skills: tuple[str, ...] = ()
    tenant_plugins: tuple[str, ...] = ()
    # Como estaban al construir la release (también NULL): el prompt los recibe igual que hoy.
    app_settings: Mapping[str, str | None] = field(default_factory=dict)
    # Overrides de playbooks congelados (nombre → texto ya sin frontmatter). Un nombre que no
    # está aquí no tiene override: se usa el default del pack o del core.
    playbooks: Mapping[str, str] = field(default_factory=dict)
    # Secciones crudas del `flow.json` del negocio (lo que lee `load_tenant_flow`), o None.
    flow_tenant: Any = None


@dataclass
class _Ranura:
    valor: ArtefactosDeRelease | None = None


_ranura: ContextVar[_Ranura | None] = ContextVar("release_vigente", default=None)


def abrir() -> Token[_Ranura | None]:
    return _ranura.set(_Ranura())


def cerrar(token: Token[_Ranura | None]) -> None:
    # Se vacía antes del reset: una tarea creada durante el turno copió la referencia a la ranura
    # y no debe seguir viendo la release cuando el turno terminó.
    ranura = _ranura.get()
    if ranura is not None:
        ranura.valor = None
    _ranura.reset(token)


def fijar(valor: ArtefactosDeRelease) -> bool:
    """Fija la release del turno. False si no hay ranura abierta (fuera de un turno)."""
    ranura = _ranura.get()
    if ranura is None:
        return False
    ranura.valor = valor
    return True


def vigente() -> ArtefactosDeRelease | None:
    """La release del turno, solo si es del negocio del `tenant_scope` activo."""
    ranura = _ranura.get()
    if ranura is None or ranura.valor is None:
        return None
    actual = get_current_tenant_id()
    if actual is None or actual != ranura.valor.tenant_id:
        return None
    return ranura.valor


def agente_del_turno() -> AgenteCongelado | None:
    """El especialista en foco de la release del turno (HU-AOS-15); None sin release."""
    actual = vigente()
    return actual.foco if actual is not None else None


def kernel_del_turno() -> AgenteCongelado | None:
    """El Coordinador (`kernelspec/1`) de la release del turno; None sin release."""
    actual = vigente()
    if actual is None:
        return None
    return next((a for a in actual.agentes if a.role == "core"), None)


def write_namespaces_del_turno() -> frozenset[str] | None:
    """`memory.writeNamespaces` del agente en foco (ADR-0114 §3). None = sin release
    (runtime legacy): `ToolContext.write_namespaces` queda None y no se chequea nada."""
    agente = agente_del_turno()
    return agente.write_namespaces if agente is not None else None


def idle_minutes(defecto: int) -> int:
    """`episode.idleMinutes` de la release del turno; `defecto` sin release."""
    actual = vigente()
    return actual.idle_minutes if actual is not None else defecto


def con_valores_de_release[T](ctx: T) -> T:
    """`ctx` (un `PromptContext`) con los campos de `AppSettings` congelados en la release del
    turno. Sin release, el mismo objeto (conducta de siempre)."""
    actual = vigente()
    if actual is None or not dataclasses.is_dataclass(ctx) or isinstance(ctx, type):
        return ctx
    cambios = {
        k: v
        for k, v in actual.app_settings.items()
        if k in CAMPOS_DE_PROMPT and (v is None or isinstance(v, str))
    }
    return dataclasses.replace(ctx, **cambios) if cambios else ctx  # type: ignore[type-var]


__all__ = [
    "CAMPOS_DE_PROMPT",
    "AgenteCongelado",
    "ArtefactosDeRelease",
    "abrir",
    "agente_del_turno",
    "cerrar",
    "con_valores_de_release",
    "fijar",
    "idle_minutes",
    "kernel_del_turno",
    "vigente",
    "write_namespaces_del_turno",
]
