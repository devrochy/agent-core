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
    import uuid

    from core.mcp.host import MCPHost
    from core.primeros_pasos.contrato import ContenidoCargado
    from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
    from core.qa.contrato_importacion import (
        ContextoDeImportacion,
        PlantillaDeImportacion,
        ResultadoDeImportacion,
    )

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"


class CobranzaModule(BaseModule):
    id = PLUGIN_ID
    name = "Cobranza"
    version = "0.6.0"
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
        {"label": "Promesas", "path": "/cobranza/promesas", "section": "General", "section_key": "pack_cobranza:promesas"},
        {"label": "Reportes", "path": "/cobranza/reportes", "section": "General", "section_key": "pack_cobranza:reportes"},
        {"label": "Reglas de negociación", "path": "/cobranza/reglas", "section": "Monitoreo", "section_key": "pack_cobranza:reglas"},
        {"label": "Monitoreo IA", "path": "/cobranza/monitoreo", "section": "Monitoreo", "section_key": "pack_cobranza:monitoreo"},
        {"label": "Dispositivos", "path": "/cobranza/dispositivos", "section": "Seguridad", "section_key": "pack_cobranza:dispositivos"},
        {"label": "Alertas de acceso", "path": "/cobranza/alertas", "section": "Seguridad", "section_key": "pack_cobranza:alertas"},
        {"label": "Auditoría", "path": "/cobranza/auditoria", "section": "Seguridad", "section_key": "pack_cobranza:auditoria"},
    ]
    # `pack_cobranza:cobros` ya no tiene item de menú (se retiró «Cobros al propietario»),
    # pero sigue siendo la sub-sección RBAC que guarda «Pagos» en /api/v1/pack/cobranza/payments.
    section_keys: ClassVar[list[str]] = ["pack_cobranza:cobros"]
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
        host.register_guardrail(
            "cob_promesa_check",
            promesa_guardrail,
            priority=6,
            etiqueta="Promesas de pago",
            explicacion="Evita que el asistente diga que registró una promesa o un acuerdo de pago si no lo guardó de verdad.",
        )
        host.register_guardrail(
            "cob_suscripcion_check",
            suscripcion_guardrail,
            priority=6,
            etiqueta="Suscripción en cobranza",
            explicacion="Evita que el asistente diga que cambió la suscripción de un cliente si la acción no se hizo.",
        )
        # Suscripción con tracking del host (ADR-0107): al desactivar el pack,
        # el core lo des-suscribe solo (antes era `event_bus.subscribe` directo).
        from core.shared.events import ON_PAYMENT_CONFIRMED
        from pack_cobranza.pagos import _on_payment_confirmed

        host.subscribe(ON_PAYMENT_CONFIRMED, _on_payment_confirmed)
        # HU-AOS-08: procedimiento `promesa_de_pago` y su resolver (solo los usa el motor, detrás
        # de `tenants.aos_procedures`).
        from pack_cobranza.procedimientos import registrar

        registrar(host, self.id)

    async def rol_de_acceso(self, tenant_id: Any, admin_id: Any) -> str | None:
        """`gestor` si la cuenta tiene un perfil de gestor activo en el negocio (ADR-0174): entra a
        la política de equipos más estricta, la de campo."""
        from core.db.session import get_session
        from pack_cobranza.api_gestor import _gestor_de

        async with get_session() as session:
            return "gestor" if await _gestor_de(session, tenant_id, admin_id) is not None else None

    async def collaborator_profiles(self, tenant_id: Any, admin_ids: list[Any]) -> dict[Any, list[Any]]:
        """Perfil de gestor de los colaboradores pedidos, para Equipo (ADR-0176).

        Tres consultas por lote (gestores, carteras y permisos), sin importar cuántos ids lleguen. El
        resumen lleva el estatus, las carteras `{id, nombre}` y el conteo de permisos de campo activos;
        editar los permisos se hace con el `PUT …/collectors/{id}/permissions` ya existente."""
        from sqlalchemy import select

        from core.db.session import get_session
        from core.mcp.types import PackProfile, PackProfileEdit
        from pack_cobranza.models import Cartera, Gestor, GestorPermiso
        from pack_cobranza.permisos import GESTOR_PERMISOS

        if not admin_ids:
            return {}
        async with get_session() as session:
            gestores = (
                (
                    await session.execute(
                        select(Gestor).where(
                            Gestor.tenant_id == tenant_id,
                            Gestor.admin_id.in_(admin_ids),
                            Gestor.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not gestores:
                return {}
            ids = [g.id for g in gestores]
            carteras = (
                await session.execute(
                    select(Cartera.gestor_id, Cartera.id, Cartera.nombre)
                    .where(
                        Cartera.tenant_id == tenant_id,
                        Cartera.gestor_id.in_(ids),
                        Cartera.deleted_at.is_(None),
                    )
                    .order_by(Cartera.nombre)
                )
            ).all()
            permisos = (
                await session.execute(
                    select(GestorPermiso.gestor_id, GestorPermiso.permiso).where(
                        GestorPermiso.tenant_id == tenant_id,
                        GestorPermiso.gestor_id.in_(ids),
                        GestorPermiso.habilitado.is_(True),
                    )
                )
            ).all()
        carteras_de: dict[Any, list[dict[str, str]]] = {}
        for gestor_id, cartera_id, nombre in carteras:
            carteras_de.setdefault(gestor_id, []).append({"id": str(cartera_id), "nombre": nombre})
        activos_de: dict[Any, int] = {}
        for gestor_id, permiso in permisos:
            if permiso in GESTOR_PERMISOS:
                activos_de[gestor_id] = activos_de.get(gestor_id, 0) + 1
        return {
            g.admin_id: [
                PackProfile(
                    pack=self.id,
                    role_id="gestor",
                    ref_id=str(g.id),
                    etiqueta="Gestor",
                    resumen={
                        "estatus": g.estatus,
                        "carteras": carteras_de.get(g.id, []),
                        "permisos_activos": activos_de.get(g.id, 0),
                        "permisos_total": len(GESTOR_PERMISOS),
                    },
                    edit=PackProfileEdit(kind="cobranza_gestor_permisos", ref_id=str(g.id)),
                )
            ]
            for g in gestores
        }

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

    async def seed_data(self, tenant_id: Any, data: dict[str, Any]) -> list[str]:
        """Datos de ejemplo de la cartera (ADR-0083): carteras, caja, gestor y clientes con
        préstamos al día y en mora. Idempotente (ver `pack_cobranza.seed`)."""
        import uuid as _uuid

        from pack_cobranza.seed import sembrar_cartera

        return await sembrar_cartera(_uuid.UUID(str(tenant_id)), data)

    def formas_de_datos_de_prueba(self) -> list[FormaDeDatos]:
        """Datos de prueba del agente (HU-QA-10, ADR-0146)."""
        from pack_cobranza.datos_de_prueba import formas

        return formas()

    async def sembrar_datos_de_prueba(
        self, tenant_id: uuid.UUID, modo: str, datos: Any, contexto: ContextoDeNegocio
    ) -> list[RegistroSembrado]:
        from pack_cobranza.datos_de_prueba import sembrar

        return await sembrar(tenant_id, modo, datos, contexto)

    async def sembrar_escenario(self, tenant_id: uuid.UUID, escenario: str, entrada: Any) -> Any:
        """Promesas, cobro al propietario, accesos, dispositivo, reglas y cierre (HU-QA-13)."""
        from core.qa.contrato_datos import EscenarioNoDisponible
        from pack_cobranza.escenarios import sembrar

        if escenario != "cobranza-completa":
            raise EscenarioNoDisponible(f"Cobranza no tiene el escenario {escenario}.")
        return await sembrar(tenant_id, entrada)

    async def retirar_datos_de_prueba(self, tenant_id: uuid.UUID, registros: list[RegistroSembrado]) -> int:
        from pack_cobranza.datos_de_prueba import retirar

        return await retirar(tenant_id, registros)

    def plantillas_de_importacion(self) -> list[PlantillaDeImportacion]:
        """Importar clientes y préstamos desde un CSV, sin teléfono (HU-QA-12, ADR-0180)."""
        from pack_cobranza.importacion import plantillas

        return plantillas()

    async def importar_filas(
        self,
        tenant_id: uuid.UUID,
        plantilla_id: str,
        filas: list[Any],
        contexto: ContextoDeImportacion,
        *,
        simular: bool,
    ) -> ResultadoDeImportacion:
        from pack_cobranza.importacion import importar

        return await importar(tenant_id, plantilla_id, filas, contexto, simular=simular)

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
