"""Configuración de Captar leads del negocio (ADR-0193 §6, HU-AOS-70).

Antes vivía en la config de commerce (`CRMConfig`); la lee la API pública del intake, que no conoce al agente, así
que es del negocio y no de cada agente (criterio de ADR-0190 §3). Se guarda entera cifrada en `lead_settings` (una
fila por negocio): lleva el secreto del intake y la llave de Turnstile.

Cambios frente a `CRMConfig`, decididos al mover (ADR-0193):
- Sin `auto_create_lead`: asegurar el lead en cada conversación lo reemplaza (❓3 de HU-AOS-70).
- Sin `lead_intake_source`: el origen es un catálogo cerrado y el intake fija `landing`.
- Sin `lead_intake_default_status`: un lead nace en `nuevo`; la etapa la cambia el embudo.

Sin fila, Captar leads no está configurado: los crons de seguimiento no hacen nada con el negocio (la misma conducta
que hoy tiene un negocio sin commerce).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core.db.models import LeadSettings
from core.db.session import get_session
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Reenganche de leads: solo se escribe a quien habló en las últimas 24 h (ventana de WhatsApp), así que la espera
# tiene que ser menor que 24 (ver `pack_commerce.config`, de donde viene).
FOLLOW_UP_HORAS_POR_DEFECTO = 4
FOLLOW_UP_HORAS_MAX = 23

_SECRETOS = ("lead_intake_secret", "lead_intake_turnstile")


class ConfigCaptarLeads(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # ------------------------------------------------------------ intake público
    lead_intake_enabled: bool = Field(
        default=False, description="Aceptar leads desde el formulario de tu página (/api/public/leads/<slug>)"
    )
    lead_intake_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Secreto compartido del intake (encabezado X-Intake-Secret). Vacío = sin configurar (503)",
    )
    lead_intake_allowed_origins: list[str] = Field(
        default_factory=list, description="Orígenes permitidos para el formulario alojado. Vacío = sin restricción"
    )
    lead_intake_turnstile: SecretStr = Field(
        default=SecretStr(""),
        description="Secret key de Cloudflare Turnstile del negocio. Si está, el intake exige y valida el token",
    )
    lead_intake_default_interest: str = Field(
        default="", description="Interés por defecto si el formulario no lo envía"
    )

    # ------------------------------------------------------------ seguimiento (HU-CRM-21/22)
    # `le=8760` como en commerce: un valor viejo tiene que seguir cargando; fuera de 1..23 el cron usa el defecto.
    follow_up_hours: int = Field(
        default=FOLLOW_UP_HORAS_POR_DEFECTO,
        ge=0,
        le=8760,
        description=f"Horas sin actividad antes de reenganchar a un lead calificado (1 a {FOLLOW_UP_HORAS_MAX})",
    )
    follow_up_lead_message: str = Field(
        default=(
            "¡Hola{name}! 👋 Quedamos pendientes de tu interés en {interest}. "
            "¿Sigues interesado? Estoy aquí para ayudarte con lo que necesites."
        ),
        description="Mensaje de reenganche de leads ({name}, {interest})",
        json_schema_extra={"mensaje": "whatsapp"},
    )
    seguimiento_recordatorios: bool = Field(
        default=True, description="Avisar cuando llega la hora de un seguimiento programado"
    )
    seguimiento_resumen_diario: bool = Field(
        default=True, description="Mandar cada mañana «Tu día de ventas» con los seguimientos y leads nuevos"
    )
    seguimiento_resumen_hora: int = Field(
        default=8, ge=0, le=19, description="Hora del negocio (0-19) desde la que sale el resumen diario"
    )


def a_guardar(config: ConfigCaptarLeads) -> dict[str, Any]:
    """El dict que se cifra: los secretos en claro (van cifrados enteros), nunca a un log."""
    datos = config.model_dump(mode="python")
    for clave in _SECRETOS:
        datos[clave] = getattr(config, clave).get_secret_value()
    return datos


def desde_guardado(datos: dict[str, Any]) -> ConfigCaptarLeads:
    """Lee lo guardado; ignora claves desconocidas (dato, nunca instrucción: invariante 7)."""
    return ConfigCaptarLeads.model_validate(datos)


def _del_negocio_en_curso(tenant_id: uuid.UUID) -> None:
    """La config solo se lee o escribe dentro del `tenant_scope` de su negocio (además de la RLS)."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("La config de Captar leads solo se usa dentro del negocio en curso")


async def cargar(session: Any, tenant_id: uuid.UUID) -> ConfigCaptarLeads | None:
    """La config del negocio en la sesión del llamador, o None si no hay fila (Captar leads sin configurar). Una
    fila ilegible (clave rotada, esquema roto) también da None y queda en el log: falla cerrado."""
    _del_negocio_en_curso(tenant_id)
    cifrada = (
        await session.execute(
            select(LeadSettings.config_encrypted).where(
                LeadSettings.tenant_id == tenant_id, LeadSettings.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if cifrada is None:
        return None
    try:
        return desde_guardado(get_secret_box().decrypt_json(cifrada))
    except (ValueError, RuntimeError, ValidationError):  # RuntimeError: clave rotada o AAD distinto
        log.warning("leads_config_ilegible", tenant_id=str(tenant_id))
        return None


async def cargar_del_negocio(tenant_id: uuid.UUID) -> ConfigCaptarLeads | None:
    """Como `cargar`, en su propia sesión (dentro del `tenant_scope` del negocio)."""
    async with get_session() as session:
        return await cargar(session, tenant_id)


async def guardar(session: Any, tenant_id: uuid.UUID, config: ConfigCaptarLeads) -> None:
    """Crea o reemplaza la config del negocio en la sesión del llamador."""
    _del_negocio_en_curso(tenant_id)
    cifrada = get_secret_box().encrypt_json(a_guardar(config))
    stmt = insert(LeadSettings).values(tenant_id=tenant_id, config_encrypted=cifrada)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[LeadSettings.tenant_id],
            set_={
                "config_encrypted": stmt.excluded.config_encrypted,
                "updated_at": stmt.excluded.updated_at,
                "deleted_at": None,
            },
        )
    )
