"""Requisitos de Meta de la app de la plataforma para Embedded Signup (HU-SH-05, ADR-0128).

"Plataforma → App de Meta" (solo master). Dos partes:

- **Automáticas**, con lo que ya está en el servidor, sin llamar a Meta: las variables de
  entorno de la app (`META_APP_ID`, `META_APP_SECRET`, `META_CONFIG_ID`), la URL pública en
  HTTPS y la suscripción del webhook de la app guardada por el cron de suscripciones.
- **Manuales**, que Meta no expone de forma fiable a la propia app y el master confirma con
  el enlace a cada pantalla: verificación del negocio, acceso de Tech Provider, App Review de
  los permisos, la configuración de Embedded Signup de producción (la del `config_id`), el
  dominio del panel en el SDK de JavaScript y la app en modo Live.

Punto de integración con HU-CONN-07 (#354, en `develop` pero no en la base apilada de esta
rama): su página "Plataforma → App de Meta" (`/admin/platform/meta-app`) corre los chequeos de
infraestructura de la app; esta lista es la parte de Embedded Signup y se enlaza desde ella. Al
rebasar, los tres datos automáticos de aquí salen (los corre su revisión) para que `ready` no
dé verde con su checklist en rojo (BACKLOG).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

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
class ChequeoAutomatico:
    key: str
    label: str
    ok: bool | None
    detail: str


@dataclass(frozen=True)
class RequisitoManual:
    key: str
    label: str
    help: str
    link: str
    done: bool
    notes: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class EstadoAppMeta:
    app_id: str
    config_id: str
    graph_api_version: str
    ready: bool
    automatic: list[ChequeoAutomatico]
    manual: list[RequisitoManual]


def _exigir_master(role: str) -> None:
    # Defensa en profundidad: la ruta ya lo exige.
    if role != "master":
        raise RequisitoError("Solo el admin de plataforma puede ver esto.", code="forbidden", status=403)


async def _automaticos() -> list[ChequeoAutomatico]:
    from core.gateway.meta_subscriptions import list_app_subscriptions

    settings = get_settings()
    app_ok = bool(settings.meta_app_id and settings.meta_app_secret)
    chequeos = [
        ChequeoAutomatico(
            "app_configured",
            "META_APP_ID y META_APP_SECRET",
            app_ok,
            "Configurados." if app_ok else "Faltan en el entorno del servidor.",
        ),
        ChequeoAutomatico(
            "config_id_configured",
            "META_CONFIG_ID",
            bool(settings.meta_config_id),
            "Configurado." if settings.meta_config_id else "Falta el config_id de Embedded Signup.",
        ),
    ]
    https = str(settings.public_base_url or "").startswith("https://")
    chequeos.append(
        ChequeoAutomatico(
            "public_https",
            "URL pública en HTTPS",
            https,
            "El webhook y el panel están en HTTPS." if https else "PUBLIC_BASE_URL no es HTTPS: Meta no la acepta.",
        )
    )
    if not settings.meta_app_id:
        chequeos.append(ChequeoAutomatico("platform_webhook", "Webhook de la app", None, "Sin app configurada."))
        return chequeos
    filas = await list_app_subscriptions(settings.meta_app_id)
    fila = next((f for f in filas if f.object == "whatsapp_business_account"), None)
    if fila is None or fila.active is None:
        chequeos.append(
            ChequeoAutomatico(
                "platform_webhook",
                "Webhook de la app",
                None,
                "Sin datos: regístralo en «Salud de Meta → Registrar webhook» (solo master) o espera el cron.",
            )
        )
    else:
        chequeos.append(
            ChequeoAutomatico(
                "platform_webhook",
                "Webhook de la app",
                fila.active,
                "Suscripción activa a whatsapp_business_account."
                if fila.active
                else (fila.last_error or "Meta desactivó la suscripción de la app.")[:200],
            )
        )
    return chequeos


async def estado_app_meta(role: str) -> EstadoAppMeta:
    _exigir_master(role)
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
    automaticos = await _automaticos()
    listo = all(c.ok is True for c in automaticos) and all(m.done for m in manuales)
    return EstadoAppMeta(
        app_id=app_id,
        config_id=settings.meta_config_id or "",
        graph_api_version=settings.graph_api_version,
        ready=listo,
        automatic=automaticos,
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
