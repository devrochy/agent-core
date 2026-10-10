"""Requisitos de Meta de la app de la plataforma para Embedded Signup (HU-SH-05, ADR-0128).

"Plataforma → App de Meta → Embedded Signup" (solo master). Son los pasos **manuales**, que
Meta no expone de forma fiable a la propia app y el master confirma con el enlace a cada
pantalla: verificación del negocio, acceso de Tech Provider, App Review de los permisos, la
configuración de Embedded Signup de producción (la del `config_id`), el dominio del panel en el
SDK de JavaScript y la app en modo Live.

Lo que se revisa en el servidor (las variables `META_APP_ID`, `META_APP_SECRET` y
`META_CONFIG_ID`, el webhook en HTTPS y la suscripción de la app) no se repite aquí: es la
revisión de la plataforma de HU-CONN-07 ("Plataforma → App de Meta", que enlaza a esta lista).
`ready` exige su última corrida terminada, sin ningún ítem en rojo y con esos tres en verde: la
lista nunca queda "lista" con el checklist de la app en rojo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.db.models import PlatformMetaAppRequirement, utcnow
from core.db.session import get_session


class RequisitoError(Exception):
    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True)
class _Manual:
    key: str
    label: str
    help: str
    link: str  # `{app_id}` se reemplaza por el de la plataforma


MANUALES: tuple[_Manual, ...] = (
    _Manual(
        "business_verification",
        "Verificación del negocio",
        "El portafolio de negocio dueño de la app está verificado (Centro de seguridad).",
        "https://business.facebook.com/settings/security",
    ),
    _Manual(
        "tech_provider",
        "Acceso de Tech Provider",
        "La app completó la verificación de acceso como Tech Provider de WhatsApp.",
        "https://developers.facebook.com/apps/{app_id}/",
    ),
    _Manual(
        "app_review",
        "App Review de los permisos",
        "Acceso avanzado aprobado para whatsapp_business_management y whatsapp_business_messaging.",
        "https://developers.facebook.com/apps/{app_id}/app-review/permissions/",
    ),
    _Manual(
        "es_configuration",
        "Configuración de Embedded Signup de producción",
        "La configuración de Facebook Login for Business con el config_id de META_CONFIG_ID existe y "
        "es la de producción (WhatsApp Embedded Signup).",
        "https://developers.facebook.com/apps/{app_id}/",
    ),
    _Manual(
        "sdk_domain",
        "Dominio del panel en el SDK de JavaScript",
        "El dominio del panel está en «Allowed domains for the JavaScript SDK» y «Login with the "
        "JavaScript SDK» está activado.",
        "https://developers.facebook.com/apps/{app_id}/",
    ),
    _Manual(
        "app_live",
        "App en modo Live",
        "La app está publicada (modo Live): en modo desarrollo solo la usan sus administradores.",
        "https://developers.facebook.com/apps/{app_id}/",
    ),
)
_CLAVES = {m.key for m in MANUALES}


@dataclass(frozen=True)
class RequisitoManual:
    key: str
    label: str
    help: str
    link: str
    done: bool
    notes: str | None
    updated_at: datetime | None


# Ítems de la revisión de la plataforma (HU-CONN-07) que Embedded Signup necesita en verde.
CHEQUEOS_DE_PLATAFORMA = ("meta_app_configured", "webhook_handshake", "app_subscriptions_complete")


@dataclass(frozen=True)
class RevisionPlataforma:
    """La última revisión de "Plataforma → App de Meta", vista desde Embedded Signup. `ok`: la
    corrida terminó, ningún ítem está en rojo y los de `CHEQUEOS_DE_PLATAFORMA` están en verde.
    `pending`: las etiquetas de los ítems que lo impiden."""

    status: str
    finished_at: datetime | None
    ok: bool
    pending: list[str]


@dataclass(frozen=True)
class EstadoAppMeta:
    app_id: str
    config_id: str
    graph_api_version: str
    ready: bool
    platform_check: RevisionPlataforma
    manual: list[RequisitoManual]


def _exigir_master(role: str) -> None:
    # Defensa en profundidad: la ruta ya lo exige.
    if role != "master":
        raise RequisitoError("Solo el admin de plataforma puede ver esto.", code="forbidden", status=403)


async def _revision_de_plataforma(admin: Any) -> RevisionPlataforma:
    from core.admin.production_readiness_service import effective_status, latest_platform_run
    from core.gateway.meta_production_readiness import PLATFORM_CHECK_LABELS

    run = await latest_platform_run(admin)
    if run is None:
        return RevisionPlataforma(status="never", finished_at=None, ok=False, pending=[])
    status, _error = effective_status(run.status, run.requested_at, run.error)
    if status != "done":
        return RevisionPlataforma(status=status, finished_at=run.finished_at, ok=False, pending=[])
    items = [item for item in (run.checks or []) if isinstance(item, dict)]
    por_clave = {item.get("key"): item for item in items}
    pendientes = [
        str(item.get("label") or item.get("key"))
        for item in items
        if item.get("ok") is False or (item.get("key") in CHEQUEOS_DE_PLATAFORMA and item.get("ok") is not True)
    ]
    # Una corrida anterior a uno de los ítems que se necesitan no alcanza: hay que revisar de nuevo.
    pendientes += [PLATFORM_CHECK_LABELS.get(c, c) for c in CHEQUEOS_DE_PLATAFORMA if c not in por_clave]
    return RevisionPlataforma(status=status, finished_at=run.finished_at, ok=not pendientes, pending=pendientes)


async def estado_app_meta(admin: Any) -> EstadoAppMeta:
    _exigir_master(getattr(admin, "role", ""))
    settings = get_settings()
    async with get_session() as session:
        filas = {
            f.key: f
            for f in (await session.execute(select(PlatformMetaAppRequirement))).scalars().all()
            if f.key in _CLAVES
        }
    app_id = settings.meta_app_id or ""
    manuales = [
        RequisitoManual(
            key=m.key,
            label=m.label,
            help=m.help,
            link=m.link.replace("{app_id}", app_id) if app_id else "https://developers.facebook.com/apps/",
            done=bool(filas[m.key].done) if m.key in filas else False,
            notes=filas[m.key].notes if m.key in filas else None,
            updated_at=filas[m.key].updated_at if m.key in filas else None,
        )
        for m in MANUALES
    ]
    revision = await _revision_de_plataforma(admin)
    return EstadoAppMeta(
        app_id=app_id,
        config_id=settings.meta_config_id or "",
        graph_api_version=settings.graph_api_version,
        ready=revision.ok and all(m.done for m in manuales),
        platform_check=revision,
        manual=manuales,
    )


async def marcar_requisito(role: str, admin_id: uuid.UUID, key: str, *, done: bool, notes: str | None) -> None:
    _exigir_master(role)
    if key not in _CLAVES:
        raise RequisitoError("Ese requisito no existe.", code="unknown_requirement", status=404)
    nota = (notes or "").strip()[:500] or None
    async with get_session() as session:
        fila = await session.get(PlatformMetaAppRequirement, key, with_for_update=True)
        if fila is None:
            fila = PlatformMetaAppRequirement(key=key)
            session.add(fila)
        fila.done = done
        fila.notes = nota
        fila.updated_by = admin_id
        fila.updated_at = utcnow()
