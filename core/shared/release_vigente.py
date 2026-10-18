"""Release del turno en curso (ADR-0117 §2 y §4, HU-AOS-14).

Con `tenants.aos_runtime = 'v3'` el kernel asigna a la conversación una release de su conexión
(`core.kernel.release_del_turno`) y deja aquí lo que el runtime lee de ella en vez del disco y de
`AppSettings`: los overrides de playbooks (ADR-0026), las secciones `tenant` del flujo
(ADR-0109), los campos de `AppSettings` que entran al prompt y el `episode.idleMinutes` del
KernelSpec. Con `legacy` la ranura queda vacía y todo se lee como siempre.

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
class ArtefactosDeRelease:
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    release_id: uuid.UUID
    lane: str
    idle_minutes: int
    # Como estaban al construir la release (también NULL): el prompt los recibe igual que hoy.
    app_settings: Mapping[str, str | None] = field(default_factory=dict)
    # Overrides de playbooks congelados (nombre → texto ya sin frontmatter). Un nombre que no
    # está aquí no tiene override: se usa el default del pack o del core.
    playbooks: Mapping[str, str] = field(default_factory=dict)
    # Secciones crudas del `flow.json` del negocio (lo que lee `load_tenant_flow`), o None.
    flow_tenant: Any = None
    # HU-AOS-16: habilidades de la release (plataforma + plugins del negocio), los procedimientos
    # congelados de TODA la release por id (`Procedure`; incluye los abiertos `0.0.0`, que solo
    # sirven a la revalidación), los agentes por `instance_id` (`AgenteDeRelease`) y, por agente,
    # sus procedimientos publicados resueltos (sin los abiertos). `agente_en_foco` es el
    # `instance_id` del agente del turno (por defecto el `is_default` de la release; el router
    # de AOS-15/17 lo cambia con `fijar_agente`). None en `procedimientos_por_agente` para una
    # release `release/1`: rige la regla transitoria de HU-AOS-05 (procedimientos del binario).
    skills: tuple[str, ...] = ()
    procedimientos: Mapping[str, Any] = field(default_factory=dict)
    agentes: Mapping[str, Any] = field(default_factory=dict)
    procedimientos_por_agente: Mapping[str, Mapping[str, Any]] | None = None
    agente_en_foco: str | None = None


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


def fijar_agente(instance_id: str) -> bool:
    """Fija el agente en foco del turno (enganche del router, HU-AOS-15/17). False si no hay
    release en el turno o la instancia no es de la release."""
    ranura = _ranura.get()
    if ranura is None or ranura.valor is None or instance_id not in ranura.valor.agentes:
        return False
    ranura.valor = dataclasses.replace(ranura.valor, agente_en_foco=instance_id)
    return True


def agente_en_foco() -> Any | None:
    """El `AgenteDeRelease` en foco, o None sin release (o sin agente por defecto)."""
    actual = vigente()
    if actual is None or actual.agente_en_foco is None:
        return None
    return actual.agentes.get(actual.agente_en_foco)


def procedimientos_del_agente() -> Mapping[str, Any] | None:
    """Los procedimientos publicados del agente en foco (id → `Procedure`). None sin release,
    con una release `release/1` (regla transitoria); vacío con `release/2` sin agente en foco."""
    actual = vigente()
    if actual is None or actual.procedimientos_por_agente is None:
        return None
    if actual.agente_en_foco is None:
        return {}  # release sin especialista: fail-closed, ningún procedimiento
    return actual.procedimientos_por_agente.get(actual.agente_en_foco, {})


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
    "ArtefactosDeRelease",
    "abrir",
    "agente_en_foco",
    "cerrar",
    "con_valores_de_release",
    "fijar",
    "fijar_agente",
    "idle_minutes",
    "procedimientos_del_agente",
    "vigente",
]
