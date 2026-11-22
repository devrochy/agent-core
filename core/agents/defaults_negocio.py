"""Los valores por defecto de los agentes del negocio (ADR-0206 §6, HU-AOS-91 parte C).

Voz, tope de largo, frases prohibidas, modelo, memoria, políticas y playbooks base del negocio: lo que ADR-0191 §4 dejaba
en `agent_specs` como «datos del negocio». No mandan en ningún turno (cada agente tiene su versión publicada): siembran el
`KernelSpec` del Coordinador y el contenido inicial de un agente nuevo, y los edita Configuración.

Viven en `app_settings.agent_defaults` (JSONB). La forma es la misma que leía `contenido_base` del v2 (`persona`,
`model`, `memory`, `policies`, `playbooks`), así que `contenido_base` y `kernel_del_negocio` los reciben sin cambiar su
lógica. Un negocio sin valores guardados usa `POR_DEFECTO` (los mismos de `DEFAULT_SPEC`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.shared.logger import get_logger

if TYPE_CHECKING:
    from core.db.models import AppSettings

log = get_logger(__name__)


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PersonaDelNegocio(_Modelo):
    role: str = Field(default="asistente", max_length=200)
    tone: Literal["formal", "neutral", "cercano", "informal"] = "cercano"
    warmth: int = Field(default=3, ge=0, le=5)
    emojis: Literal["none", "sparse", "free"] = "sparse"
    maxLength: int = Field(default=500, ge=0, le=10_000)
    forbiddenPhrases: list[str] = Field(default_factory=list, max_length=100)


class ModeloDelNegocio(_Modelo):
    primary: str = Field(default="deepseek-flash", min_length=1, max_length=100)
    fallback: str | None = Field(default=None, max_length=100)
    router: str | None = Field(default=None, max_length=100)
    temperature: float = Field(default=0.3, ge=0, le=2)


class RagDelNegocio(_Modelo):
    topK: int = Field(ge=1, le=100)
    minScore: float = Field(ge=0, le=1)


class MemoriaDelNegocio(_Modelo):
    shortTermTurns: int = Field(default=30, ge=1, le=500)
    rag: RagDelNegocio | None = None


class PoliticasDelNegocio(_Modelo):
    maxToolCallsPerTurn: int = Field(default=10, ge=1, le=100)
    budgetUsdPerConversation: float | None = Field(default=0.5, ge=0, le=1000)


class PlaybookDelNegocio(_Modelo):
    name: str
    description: str = ""


def _playbooks_base() -> list[PlaybookDelNegocio]:
    return [
        PlaybookDelNegocio(name="identidad", description="Identidad del asistente"),
        PlaybookDelNegocio(name="tono", description="Voz y tono del asistente"),
        PlaybookDelNegocio(name="reglas-operacion", description="Reglas de operación del asistente"),
        PlaybookDelNegocio(name="respuestas", description="Mensajes de cierre y avisos del asistente"),
    ]


class DefaultsDelNegocio(_Modelo):
    persona: PersonaDelNegocio = Field(default_factory=PersonaDelNegocio)
    model: ModeloDelNegocio = Field(default_factory=ModeloDelNegocio)
    memory: MemoriaDelNegocio = Field(default_factory=MemoriaDelNegocio)
    policies: PoliticasDelNegocio = Field(default_factory=PoliticasDelNegocio)
    playbooks: list[PlaybookDelNegocio] = Field(default_factory=_playbooks_base, max_length=60)


POR_DEFECTO = DefaultsDelNegocio()


def desde_dict(datos: Any) -> DefaultsDelNegocio:
    """Los valores guardados (o los por defecto si no hay o no validan: un valor roto no tumba Configuración)."""
    if not isinstance(datos, dict) or not datos:
        return DefaultsDelNegocio()
    try:
        return DefaultsDelNegocio.model_validate(datos)
    except ValueError:
        log.warning("agent_defaults_invalidos")
        return DefaultsDelNegocio()


async def leer(tenant_id: uuid.UUID) -> DefaultsDelNegocio:
    """Los valores por defecto del negocio, en el `tenant_scope` del negocio."""
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("defaults_negocio: fuera del tenant_scope del negocio")
    async with get_session() as session:
        datos = (
            await session.execute(
                select(AppSettings.agent_defaults).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    return desde_dict(datos)


def a_guardar(defaults: DefaultsDelNegocio) -> dict[str, Any]:
    return defaults.model_dump(mode="json")


def fusionar(guardados: Any, cambios: Mapping[str, Any]) -> DefaultsDelNegocio:
    """Los guardados con `cambios` encima, validados. Por sección (`persona`, `model`, `memory`, `policies`) solo
    cambian las claves que vienen, aunque traigan `None` (`memory.rag = None` apaga el RAG): un PUT parcial nunca pisa
    con los por defecto lo que no vino. `playbooks` se reemplaza entero. Lanza `ValidationError` (un `ValueError`).

    El merge es sobre lo guardado tal cual, no sobre `desde_dict`: un valor guardado que ya no valida (el backfill
    copia el v2, que no tenía topes) hace fallar el guardado en vez de cambiar todo lo demás por los por defecto."""
    datos: dict[str, Any] = dict(guardados) if isinstance(guardados, dict) else {}
    for seccion, valores in cambios.items():
        if isinstance(valores, Mapping) and isinstance(datos.get(seccion), dict):
            datos[seccion] = {**datos[seccion], **valores}
        else:
            datos[seccion] = dict(valores) if isinstance(valores, Mapping) else valores
    return DefaultsDelNegocio.model_validate(datos)


def escribir(fila: AppSettings, cambios: Mapping[str, Any]) -> DefaultsDelNegocio:
    """Aplica `cambios` sobre los valores de la fila de `app_settings` ya cargada en la sesión de quien llama (bajo el
    `tenant_scope` del negocio): se reasigna la columna, no se muta el JSONB, para que SQLAlchemy la guarde."""
    nuevos = fusionar(fila.agent_defaults, cambios)
    fila.agent_defaults = a_guardar(nuevos)
    return nuevos


async def guardar(tenant_id: uuid.UUID, cambios: Mapping[str, Any]) -> DefaultsDelNegocio:
    """`escribir` en una transacción propia: bloquea la fila de `app_settings` del negocio (la crea si no existe) para
    que dos guardados parciales a la vez no se pisen."""
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("defaults_negocio: fuera del tenant_scope del negocio")
    async with get_session() as session:
        fila = (
            await session.execute(
                select(AppSettings)
                .where(AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if fila is None:
            fila = AppSettings(tenant_id=tenant_id)
            session.add(fila)
        return escribir(fila, cambios)


__all__ = [
    "POR_DEFECTO",
    "DefaultsDelNegocio",
    "MemoriaDelNegocio",
    "ModeloDelNegocio",
    "PersonaDelNegocio",
    "PlaybookDelNegocio",
    "PoliticasDelNegocio",
    "RagDelNegocio",
    "a_guardar",
    "desde_dict",
    "escribir",
    "fusionar",
    "guardar",
    "leer",
]
