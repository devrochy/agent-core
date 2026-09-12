"""Plugin Manager: detección, ciclo de vida y aislamiento de plugins.

Detección: entry_points de setuptools, grupo "agent_core.plugins".
Cada plugin declara en su pyproject.toml:

    [project.entry-points."agent_core.plugins"]
    mod_crm_engine = "mod_crm_engine.module:CRMModule"

Ciclo de vida:  Install (pip) -> Detect (scan) -> Configure (form) -> Activate -> Run
Estados en BD:  detected -> configured -> active | disabled | error

Aislamiento (regla de oro): si un plugin lanza PluginError en runtime, el core
lo desactiva automáticamente, registra el error y el asistente sigue vivo.

Migraciones por plugin: cada plugin puede traer su propio árbol Alembic
(plugins/<mod>/alembic.ini). Se ejecutan al activar (docs/PLUGIN-CONTRACT.md).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from core.db.models import PluginConfig, PluginRecord, PluginTenantState, Tenant
from core.db.session import get_session
from core.mcp.host import mcp_host
from core.mcp.types import BaseModule, BasePluginConfig
from core.shared.audit import record_audit
from core.shared.crypto import get_secret_box
from core.shared.events import ON_PLUGIN_ACTIVATED, ON_PLUGIN_DISABLED, event_bus
from core.shared.exceptions import PluginConfigError, PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

ENTRY_POINT_GROUP = "agent_core.plugins"


class PluginManager:
    def __init__(self) -> None:
        self._modules: dict[str, BaseModule] = {}   # instancias detectadas
        self._active: set[str] = set()
        self._activation_locks: dict[str, asyncio.Lock] = {}

    def _activation_lock(self, plugin_id: str) -> asyncio.Lock:
        """Un lock por plugin_id (no por tenant): serializa la PRIMERA
        activación de cada plugin. Sin esto, dos tenants activando el mismo
        plugin por primera vez casi al mismo tiempo pasan ambos el chequeo
        `plugin_id in self._active` (todavía False para los dos) y compiten
        por `mcp_host.register_skill()` -- el segundo revienta con "Skill
        duplicada" y su except llama `mcp_host.remove_plugin()`, que borra
        TAMBIÉN las tools que el primer tenant acababa de registrar con
        éxito, y pisa el status global a "error" por un problema de timing
        ajeno a la config del primer tenant."""
        lock = self._activation_locks.get(plugin_id)
        if lock is None:
            lock = asyncio.Lock()
            self._activation_locks[plugin_id] = lock
        return lock

    # ------------------------------------------------------------ detección
    def _ensure_plugin_paths(self) -> None:
        """Añade los directorios de plugins locales a sys.path.

        Cuando `pip install -e` corre en un subproceso durante `plugin install`,
        el proceso actual no re-procesa los .pth files (que registran el finder
        del editable install). Este método garantiza que el paquete sea importable
        añadiendo la carpeta fuente directamente a sys.path como fallback.
        """
        from core.db.migrations import project_root

        for base in (project_root() / "packages", project_root() / "plugins"):
            if not base.exists():
                continue
            for plugin_dir in sorted(base.iterdir()):
                if plugin_dir.is_dir() and (plugin_dir / "pyproject.toml").exists():
                    src = str(plugin_dir.resolve())
                    if src not in sys.path:
                        sys.path.insert(0, src)
        # Invalida la caché de importación para detectar paquetes recién instalados
        importlib.invalidate_caches()

    def scan(self) -> list[BaseModule]:
        """Escanea entry_points e instancia cada BaseModule encontrado."""
        self._ensure_plugin_paths()
        discovered: list[BaseModule] = []
        for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
            try:
                module_class = ep.load()
                if not (isinstance(module_class, type) and issubclass(module_class, BaseModule)):
                    log.error("plugin_invalid_class", entry_point=ep.name)
                    continue
                instance = module_class()
                if not instance.id:
                    instance.id = ep.name
                self._modules[instance.id] = instance
                discovered.append(instance)
                log.info("plugin_detected", plugin=instance.id, version=instance.version)
            except Exception:
                log.exception("plugin_load_failed", entry_point=ep.name)
        return discovered

    async def sync_registry(self) -> None:
        """Sincroniza los plugins detectados con la tabla `plugins`."""
        async with get_session() as session:
            for module in self._modules.values():
                record = await session.get(PluginRecord, module.id)
                if record is None:
                    session.add(
                        PluginRecord(
                            id=module.id,
                            name=module.name or module.id,
                            version=module.version,
                            status="detected" if module.requires_config else "configured",
                        )
                    )
                else:
                    record.name = module.name or module.id
                    record.version = module.version

    # ------------------------------------------------------------ configuración
    async def save_config(self, plugin_id: str, raw_values: dict[str, Any]) -> None:
        """Valida contra el schema Pydantic del plugin y persiste cifrado (Fernet)."""
        module = self._require(plugin_id)
        if module.config_schema is not None:
            try:
                module.config_schema.model_validate(raw_values)
            except ValidationError as exc:
                raise PluginConfigError(plugin_id, f"Configuración inválida: {exc}") from exc

        encrypted = get_secret_box().encrypt_json(raw_values)
        tenant_id = get_current_tenant_id()
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(PluginConfig).where(
                        PluginConfig.plugin_id == plugin_id,
                        PluginConfig.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    PluginConfig(plugin_id=plugin_id, tenant_id=tenant_id, config_encrypted=encrypted)
                )
            else:
                existing.config_encrypted = encrypted
            record = await session.get(PluginRecord, plugin_id)
            if record and record.status == "detected":
                record.status = "configured"

    async def load_config(self, plugin_id: str) -> BasePluginConfig | None:
        module = self._require(plugin_id)
        if module.config_schema is None:
            return None
        tenant_id = get_current_tenant_id()
        async with get_session() as session:
            stored = (
                await session.execute(
                    select(PluginConfig).where(
                        PluginConfig.plugin_id == plugin_id,
                        PluginConfig.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        if stored is None:
            if module.requires_config:
                raise PluginConfigError(plugin_id, "El plugin requiere configuración previa")
            return None
        values = get_secret_box().decrypt_json(stored.config_encrypted)
        return module.config_schema.model_validate(values)

    # ------------------------------------------------------------ activación
    async def activate(self, plugin_id: str) -> None:
        """Registra las tools/skills del plugin en `mcp_host` (una sola vez por
        proceso) y valida la config del tenant actual.

        `mcp_host` es un registro GLOBAL al proceso (`register_skill` revienta
        con "Skill duplicada" si se llama dos veces para el mismo plugin) — el
        registro de tools solo puede correr una vez, sin importar cuántos
        tenants lo activen. El enablement real por-tenant ya vive en
        `AgentSpec.packs` (ver `_enable_pack_for_tenant`), no acá.

        Lo que SÍ es por-tenant y antes se saltaba en cualquier activación
        posterior a la primera: `load_config()` valida la config de ESTE
        tenant. Sin esto, si el tenant B guardaba una config inválida, nunca
        se enteraba al activar — recién le fallaba a mitad de una conversación
        real, cuando una tool intentaba cargarla.

        Todo el método corre bajo `_activation_lock(plugin_id)`: sin eso, dos
        tenants activando el MISMO plugin por primera vez casi a la vez
        pasarían ambos el chequeo `plugin_id in self._active` y competirían
        por registrar en `mcp_host` -- el perdedor de la carrera rompía con
        "Skill duplicada" y su cleanup borraba también las tools que el
        ganador acababa de registrar con éxito.
        """
        module = self._require(plugin_id)
        async with self._activation_lock(plugin_id):
            if plugin_id in self._active:
                # ya registrado en mcp_host para otro tenant; acá solo se
                # valida la config de ESTE tenant, sin tocar el status
                # global del plugin ni mcp_host -- un error de config de B
                # no puede marcar como roto lo que a A le funciona bien.
                try:
                    module.config = await self.load_config(plugin_id)
                    await self._set_status(plugin_id, "active", activated=True)
                except Exception as exc:
                    raise PluginError(plugin_id, f"Configuración inválida: {exc}") from exc
                return
            try:
                module.config = await self.load_config(plugin_id)
                self._run_plugin_migrations(plugin_id)
                mcp_host.begin_activation(plugin_id)
                try:
                    module.on_activate(mcp_host)
                finally:
                    mcp_host.end_activation()
                self._active.add(plugin_id)
                await self._set_status(plugin_id, "active", activated=True)
                await event_bus.emit(ON_PLUGIN_ACTIVATED, plugin_id=plugin_id)
                log.info("plugin_activated", plugin=plugin_id)
            except Exception as exc:
                mcp_host.remove_plugin(plugin_id)
                await self._set_status(plugin_id, "error", error=str(exc))
                log.exception("plugin_activation_failed", plugin=plugin_id)
                raise PluginError(plugin_id, f"Activación falló: {exc}") from exc

    async def deactivate(self, plugin_id: str, status: str = "disabled", reason: str = "") -> None:
        module = self._modules.get(plugin_id)
        if module is not None:
            try:
                module.on_deactivate()
            except Exception:
                log.exception("plugin_deactivate_hook_failed", plugin=plugin_id)
        mcp_host.remove_plugin(plugin_id)
        self._active.discard(plugin_id)
        await self._set_status(plugin_id, status, error=reason or None)
        await event_bus.emit(ON_PLUGIN_DISABLED, plugin_id=plugin_id, reason=reason)
        log.info("plugin_deactivated", plugin=plugin_id, reason=reason)

    async def auto_disable(self, plugin_id: str, reason: str) -> None:
        """Desactivación automática GLOBAL ante un PluginError en runtime,
        sin contexto de tenant resuelto (ver `auto_disable_for_tenant` --
        el camino que usan `loop.py`/`webhooks.py` en la práctica).

        El asistente NUNCA se cae por un plugin roto. El panel admin muestra
        el error y el master puede reactivarlo tras corregir la causa.
        """
        await self.deactivate(plugin_id, status="error", reason=reason)

    async def auto_disable_for_tenant(
        self, tenant_id: uuid.UUID | None, plugin_id: str, reason: str
    ) -> None:
        """Desactivación automática ante un PluginError en runtime, ACOTADA
        al tenant que lo disparó (ADR-0024).

        A diferencia de `auto_disable`/`deactivate`, esto NO toca `mcp_host`
        (el registro de tools sigue intacto para todos los tenants) ni
        `PluginRecord.status` (el estado de sistema no cambia) -- solo
        marca, en `plugin_tenant_state`, que ESTE tenant no puede usar el
        plugin hasta que un admin lo reactive. Otro tenant con el mismo
        plugin, que nunca causó el error, sigue funcionando con normalidad.

        Sin `tenant_id` resuelto (contexto de sistema sin tenant claro, ej.
        un cron), cae al `auto_disable()` global de siempre -- no hay dónde
        acotar el daño.
        """
        if tenant_id is None:
            await self.auto_disable(plugin_id, reason)
            return
        await self._write_tenant_error(tenant_id, plugin_id, reason)
        log.warning(
            "plugin_auto_disabled_for_tenant", plugin=plugin_id, tenant_id=str(tenant_id), reason=reason
        )
        await record_audit(
            actor=f"plugin:{plugin_id}",
            action="auto_disabled_for_tenant",
            output_data={"reason": reason},
        )

    async def _write_tenant_error(self, tenant_id: uuid.UUID, plugin_id: str, reason: str) -> None:
        """Upsert en `plugin_tenant_state`. Separado de `auto_disable_for_tenant`
        para poder monkeypatchear la escritura a BD en tests unitarios (mismo
        criterio que `_set_status`).

        Sin borrado físico (invariante #8): si la fila ya existía pero estaba
        `deleted_at` (reactivada por un admin) y el plugin vuelve a fallar
        para este mismo tenant, se reutiliza la misma fila y se limpia
        `deleted_at` -- no se crea una segunda."""
        from sqlalchemy.exc import IntegrityError

        now = datetime.now(UTC)
        try:
            async with get_session() as session:
                existing = await session.get(PluginTenantState, (plugin_id, tenant_id))
                if existing is None:
                    session.add(
                        PluginTenantState(
                            plugin_id=plugin_id,
                            tenant_id=tenant_id,
                            status="error",
                            error_message=reason,
                            disabled_at=now,
                            deleted_at=None,
                            updated_at=now,
                        )
                    )
                else:
                    existing.status = "error"
                    existing.error_message = reason
                    existing.disabled_at = now
                    existing.deleted_at = None
                    existing.updated_at = now
        except IntegrityError:
            # Carrera: dos tool-calls del mismo tenant fallaron casi a la vez
            # y ambas intentaron crear la fila. La que ganó ya dejó el
            # estado en "error" -- no hace falta reintentar como UPDATE.
            pass

    async def is_disabled_for_tenant(self, tenant_id: uuid.UUID, plugin_id: str) -> bool:
        async with get_session() as session:
            row = await session.get(PluginTenantState, (plugin_id, tenant_id))
        return row is not None and row.deleted_at is None

    async def disabled_plugins_for_tenant(self, tenant_id: uuid.UUID) -> set[str]:
        async with get_session() as session:
            result = await session.execute(
                select(PluginTenantState.plugin_id).where(
                    PluginTenantState.tenant_id == tenant_id,
                    PluginTenantState.deleted_at.is_(None),
                )
            )
            return set(result.scalars().all())

    async def reactivate_for_tenant(self, tenant_id: uuid.UUID | None, plugin_id: str) -> None:
        """Marca `deleted_at` en la fila de error de este tenant (acción de
        admin, tras corregir la causa) -- sin borrado físico (invariante #8).
        Sin fila activa = sano, no hace falta distinguir "nunca falló" de
        "falló y se reactivó"."""
        if tenant_id is None:
            log.warning("plugin_reactivate_sin_tenant", plugin=plugin_id)
            return
        async with get_session() as session:
            row = await session.get(PluginTenantState, (plugin_id, tenant_id))
            if row is not None and row.deleted_at is None:
                row.deleted_at = datetime.now(UTC)
        log.info("plugin_reactivated_for_tenant", plugin=plugin_id, tenant_id=str(tenant_id))

    async def activate_all_configured(self) -> None:
        """Al arrancar: activa todo plugin con estado configured/active en BD.

        El registro de tools en `mcp_host` es una acción GLOBAL de una sola vez
        por proceso (ver docstring de `activate()`) -- qué tenant se use para esa
        primera activación no importa funcionalmente (`self.config` solo se lee
        dentro de `on_activate()` para validar el schema hoy). Lo que SÍ importa:
        `load_config()` exige que ESE tenant tenga una fila de `plugin_configs`
        guardada, o la activación falla.

        Antes esto usaba SIEMPRE `default_tenant()` para todos los plugins por
        igual -- un plugin cuyo config solo existe para OTRO tenant (ej.
        `pack_payments`: credenciales de Wompi específicas de un negocio, nunca
        del tenant "por defecto") fallaba con "El plugin requiere configuración
        previa" en CADA reinicio, aunque ese tenant tuviera una config
        perfectamente válida -- confirmado 3 veces en el entorno de test.
        """
        from core.shared.tenant import default_tenant, tenant_scope

        async with get_session() as session:
            result = await session.execute(
                select(PluginRecord).where(PluginRecord.status.in_(["configured", "active"]))
            )
            records = result.scalars().all()
        if not records:
            return
        fallback_tenant_id = await default_tenant()

        for record in records:
            if record.id not in self._modules:
                log.warning("plugin_in_db_not_installed", plugin=record.id)
                continue
            tenant_id = await self._tenant_with_config(record.id) or fallback_tenant_id
            if tenant_id is None:
                continue
            async with tenant_scope(tenant_id):
                try:
                    await self.activate(record.id)
                except PluginError:
                    pass  # ya quedó en estado error; el asistente sigue

    async def _tenant_with_config(self, plugin_id: str) -> uuid.UUID | None:
        """Un tenant real con config guardada para este plugin, para el arranque.

        `plugin_configs` tiene RLS: no se puede preguntar "qué tenant tiene esto"
        sin resolver el tenant primero. Se itera `tenants` (tabla de sistema, sin
        RLS -- ver su docstring) probando cada uno dentro de su propio
        `tenant_scope`. Cantidad de tenants esperada: chica (este framework es de
        un solo operador por despliegue), el costo de iterar es despreciable y
        solo pasa una vez, al arrancar."""
        from core.shared.tenant import tenant_scope

        async with get_session() as session:
            tenant_ids = (
                await session.execute(select(Tenant.id).where(Tenant.status == "active"))
            ).scalars().all()
        for tenant_id in tenant_ids:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    exists = (
                        await session.execute(
                            select(PluginConfig.tenant_id).where(
                                PluginConfig.plugin_id == plugin_id
                            ).limit(1)
                        )
                    ).scalar_one_or_none()
                if exists is not None:
                    return tenant_id
        return None

    # ------------------------------------------------------------ migraciones por plugin
    def _run_plugin_migrations(self, plugin_id: str) -> None:
        """Aplica el árbol Alembic propio del plugin, si tiene uno.

        Delega en core.db.migrations, el camino único compartido con el CLI y
        el entrypoint del contenedor (antes cada entrada tenía el suyo).
        """
        from core.db.migrations import upgrade_plugin

        upgrade_plugin(plugin_id)

    # ------------------------------------------------------------ helpers
    def get(self, plugin_id: str) -> BaseModule | None:
        return self._modules.get(plugin_id)

    def is_active(self, plugin_id: str) -> bool:
        return plugin_id in self._active

    def list_modules(self) -> list[BaseModule]:
        return list(self._modules.values())

    def active_admin_menu(self, enabled: set[str] | None = None) -> list[dict[str, Any]]:
        """Entradas de menú del panel agrupadas por plugin ACTIVO.

        Devuelve una lista de grupos:
            [{"name": "CRM", "items": [{"label": "Leads", "path": "..."}, ...]}]

        `self._active` es global al proceso (código cargado en el servidor, no
        por tenant). Sin `enabled`, se listan todos los activos — comportamiento
        histórico, para quien llame sin conocer el tenant actual. Pasando
        `enabled` (el `AgentSpec.packs` del tenant, vía `enabled_packs()`) se
        filtra a los que además están habilitados para ESE tenant — si no, el
        menú de un pack activo globalmente por otro tenant aparecía igual para
        cualquiera, aunque `/admin/plugins` ya dijera "no habilitado".
        """
        active = self._active if enabled is None else self._active & enabled
        groups: list[dict[str, Any]] = []
        for plugin_id in active:
            module = self._modules.get(plugin_id)
            if module and module.admin_menu:
                groups.append(
                    {
                        "name": module.name or plugin_id,
                        "items": list(module.admin_menu),
                    }
                )
        return groups

    def _require(self, plugin_id: str) -> BaseModule:
        module = self._modules.get(plugin_id)
        if module is None:
            raise PluginError(plugin_id, "Plugin no instalado (no aparece en entry_points)")
        return module

    async def _set_status(
        self, plugin_id: str, status: str, error: str | None = None, activated: bool = False
    ) -> None:
        async with get_session() as session:
            record = await session.get(PluginRecord, plugin_id)
            if record is None:
                return
            record.status = status
            record.error_message = error
            if activated:
                record.activated_at = datetime.now(UTC)


plugin_manager = PluginManager()
