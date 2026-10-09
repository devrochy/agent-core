"""CobranzaModule: el BaseModule del pack (alineado a app-cobranza develop).

Registra la skill de cobranza, el guardrail de promesas, los cron de mora y el
panel propio (`admin.py`, `/cobranza/…`) con navegación por secciones.

Modelo (ADR-0060/0062): **el propietario es el tenant** → Cartera → Cliente →
Préstamo. Aislamiento multi-tenant (ADR-0059): 1 tenant = 1 propietario.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

# Importar `jobs` a nivel de módulo registra los `@register_cron` en Celery
# (worker y beat) apenas se escanea el pack -- no dentro de `on_activate()`.
import pack_cobranza.jobs

# Importar `providers` registra la contribución del pack a la vista global de
# leads (ADR-0056) al escanear el pack.
import pack_cobranza.providers  # noqa: F401
from core.mcp.types import BaseModule
from core.shared.logger import get_logger
from pack_cobranza.admin import router as admin_router
from pack_cobranza.admin_api import router as admin_api_router
from pack_cobranza.api import router as public_api_router
from pack_cobranza.api_gestor import router as gestor_api_router
from pack_cobranza.config import CobranzaConfig
from pack_cobranza.guardrails import promesa_guardrail, suscripcion_guardrail
from pack_cobranza.skills import CobranzaSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost
    from core.primeros_pasos.contrato import ContenidoCargado

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"


class CobranzaModule(BaseModule):
    id = PLUGIN_ID
    name = "Cobranza"
    version = "0.3.0"
    description = "Carteras, clientes, préstamos y negociación de cobranza por WhatsApp"
    display_name = "Cobranza"
    category = "Cobros"
    min_plan = "premium"
    action_summary: ClassVar[list[str]] = ["Consultar saldo y mora", "Registrar pagos y promesas", "Cobrar en campo"]
    setup_guide = "Crea tus carteras y clientes; los gestores usan la app de campo."
    requires_config = True
    config_schema = CobranzaConfig
    # Navegación por secciones (ADR-0061). El propietario ES el tenant (ADR-0062),
    # así que no hay item "Propietarios": su identidad y config viven en el core
    # (Tenants/Configuración del pack).
    # `section_key` = sub-sección RBAC (ADR-0063): un colaborador puede tener
    # write en unas y read en otras vía grants explícitos.
    admin_menu: list[dict[str, str]] = [  # noqa: RUF012
        {"label": "Resumen", "path": "/cobranza/", "section": "General", "section_key": "pack_cobranza:resumen"},
        {"label": "Gestores", "path": "/cobranza/gestores", "section": "General", "section_key": "pack_cobranza:gestores"},
        {"label": "Carteras", "path": "/cobranza/carteras", "section": "General", "section_key": "pack_cobranza:carteras"},
        {"label": "Clientes", "path": "/cobranza/clientes", "section": "General", "section_key": "pack_cobranza:clientes"},
        {"label": "Cobros", "path": "/cobranza/cobros", "section": "General", "section_key": "pack_cobranza:cobros"},
        {"label": "Propietario", "path": "/cobranza/propietario", "section": "General", "section_key": "pack_cobranza:propietario"},
        {"label": "Promesas", "path": "/cobranza/promesas", "section": "General", "section_key": "pack_cobranza:promesas"},
        {"label": "Reportes", "path": "/cobranza/reportes", "section": "General", "section_key": "pack_cobranza:reportes"},
        {"label": "Reglas de negociación", "path": "/cobranza/reglas", "section": "Monitoreo", "section_key": "pack_cobranza:reglas"},
        {"label": "Monitoreo IA", "path": "/cobranza/monitoreo", "section": "Monitoreo", "section_key": "pack_cobranza:monitoreo"},
        {"label": "Dispositivos", "path": "/cobranza/dispositivos", "section": "Seguridad", "section_key": "pack_cobranza:dispositivos"},
        {"label": "Alertas de acceso", "path": "/cobranza/alertas", "section": "Seguridad", "section_key": "pack_cobranza:alertas"},
        {"label": "Auditoría", "path": "/cobranza/auditoria", "section": "Seguridad", "section_key": "pack_cobranza:auditoria"},
    ]
    # Roles de colaborador del pack (ADR-0066): un colaborador con rol "gestor"
    # es un gestor de cobranza (perfil `Gestor` ligado a su `admin_id`).
    collaborator_roles: list[dict[str, str]] = [  # noqa: RUF012
        {"id": "gestor", "label": "Gestor de cobranza"},
    ]
    admin_router = admin_router

    def admin_api_routes(self) -> list[Any]:
        """API admin consumida por el panel nuevo (ADR-0079)."""
        return [admin_api_router]
    declared_skills: list[type] = [CobranzaSkill]  # noqa: RUF012

    def on_activate(self, host: MCPHost) -> None:
        host.register_skill(CobranzaSkill())
        host.register_guardrail("cob_promesa_check", promesa_guardrail, priority=6)
        host.register_guardrail("cob_suscripcion_check", suscripcion_guardrail, priority=6)
        # Suscripción con tracking del host (ADR-0107): al desactivar el pack,
        # el core lo des-suscribe solo (antes era `event_bus.subscribe` directo).
        from core.shared.events import ON_PAYMENT_CONFIRMED
        from pack_cobranza.pagos import _on_payment_confirmed

        host.subscribe(ON_PAYMENT_CONFIRMED, _on_payment_confirmed)

    async def on_collaborator_role(self, tenant_id: Any, admin_id: Any, role_id: str) -> None:
        """Rol de pack asignado a un colaborador (ADR-0066): crea su perfil `Gestor`.

        Idempotente por `(tenant_id, admin_id)`. El nombre sale del `Admin` (que no
        tiene nombre propio en el core) — editable luego en la pantalla de Gestores.
        """
        if role_id != "gestor":
            return
        import uuid as _uuid

        from sqlalchemy import select

        from core.db.models import Admin
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope
        from pack_cobranza.models import Gestor

        tid = _uuid.UUID(str(tenant_id))
        aid = _uuid.UUID(str(admin_id))
        async with tenant_scope(tid):
            async with get_session() as session:
                existe = (await session.execute(select(Gestor).where(Gestor.tenant_id == tid, Gestor.admin_id == aid))).scalar_one_or_none()
                if existe is not None:
                    return
                admin = (await session.execute(select(Admin).where(Admin.id == aid))).scalar_one_or_none()
                session.add(
                    Gestor(
                        tenant_id=tid,
                        admin_id=aid,
                        nombre=(admin.username or admin.email) if admin else "Gestor",
                        correo=admin.email if admin else None,
                    )
                )
        log.info("cobranza_gestor_creado", tenant_id=str(tid), admin_id=str(aid))

    async def contenido_cargado(self, tenant_id: Any) -> ContenidoCargado:
        """Clientes de cartera para el paso «catálogo o servicios» de los primeros pasos
        (HU-SH-06, ADR-0124). Solo lee."""
        from sqlalchemy import func, select

        from core.db.session import get_session
        from core.primeros_pasos.contrato import ContenidoCargado
        from core.shared.tenant import tenant_scope
        from pack_cobranza.models import Cliente

        async with tenant_scope(tenant_id):
            async with get_session() as session:
                cantidad, primero = (
                    await session.execute(
                        select(func.count(Cliente.id), func.min(Cliente.created_at)).where(
                            Cliente.tenant_id == tenant_id, Cliente.deleted_at.is_(None)
                        )
                    )
                ).one()
        return ContenidoCargado(
            tipo="cartera", cantidad=int(cantidad or 0), primero_en=primero, seccion="pack_cobranza:clientes"
        )

    def public_routes(self) -> list[Any]:
        """APIs públicas del pack: campo v1 (ADR-0057) y gestor/APK (ADR-0065)."""
        return [public_api_router, gestor_api_router]
