"""Habilidades ("packs") del negocio: activación, configuración y chequeo previo a instalarlas en un agente.

Una habilidad es del negocio solo si algún agente activo la tiene instalada (`pack_installations` con
`scope = 'agent'`, ADR-0206, HU-AOS-91): ya no hay interruptor a nivel negocio ni se escribe en `AgentSpec.packs`.
Lo que queda aquí es lo previo a instalar (despliegue, cuota y activación del código) y la configuración del
negocio. `core.admin.views` reexporta estas funciones con sus nombres de siempre.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select

from core.db.models import PluginConfig
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.audit import record_audit
from core.shared.exceptions import PluginConfigError, PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


class HabilitacionError(RuntimeError):
    """Un pack no se pudo habilitar. `code` estable: `pack_no_instalado` (el despliegue no trae el
    módulo)."""

    def __init__(self, code: str, packs: Iterable[str], mensaje: str) -> None:
        super().__init__(mensaje)
        self.code = code
        self.packs = tuple(packs)


@dataclass(frozen=True)
class ResultadoHabilitacion:
    habilitados_ahora: tuple[str, ...] = ()
    ya_estaban: tuple[str, ...] = ()
    # pack -> por qué no quedó activo (falta configuración): queda habilitado igual y se avisa.
    config_pendiente: dict[str, str] = field(default_factory=dict)


async def ensure_pack_activated(pack: str) -> str | None:
    """Activa un pack si su código todavía no estaba cargado. Devuelve el error
    (str) o None si quedó activo (o ya lo estaba).

    Un pack con config obligatoria y sin fila previa para el tenant actual (ej.
    nunca pasó por `tenant create --packs` ni por `/admin/plugins/<id>/configurar`)
    no activa: `load_config` exige una fila existente. Se intenta una config
    vacía por default (mismo patrón que ya usa `onboard_tenant` en
    core/onboarding.py) para los packs cuyo schema no tiene campos obligatorios.

    Si el schema SÍ exige campos reales (ej. credenciales de una pasarela de
    pago), antes esto dejaba pasar el `ValidationError` crudo de pydantic tal
    cual al flash del admin ("3 validation errors for PaymentsConfig...") --
    inentendible para quien solo apretó el botón "Activar" en vez de
    "Configurar" primero. Ahora se traduce a un mensaje accionable, sin pisar
    una configuración real si ya existía.
    """
    if plugin_manager.is_active(pack):
        return None
    try:
        module = plugin_manager.get(pack)
        # Un pack que no exige config del negocio (`requires_config = False`, p. ej. Pagos con cuentas por agente,
        # ADR-0190 §6) se activa sin fila.
        if module is not None and module.config_schema is not None and module.requires_config:
            async with get_session() as session:
                has_config = (
                    await session.execute(
                        select(PluginConfig).where(
                            PluginConfig.plugin_id == pack,
                            PluginConfig.tenant_id == get_current_tenant_id(),
                        )
                    )
                ).scalar_one_or_none() is not None
            if not has_config:
                try:
                    await plugin_manager.save_config(pack, {})
                except PluginConfigError:
                    return f'{pack} necesita configuración -- usá el botón "Configurar" (⚙️) primero'
        await plugin_manager.activate(pack)
        return None
    except PluginError as exc:
        return f"{pack}: {exc}"


async def _config_del_negocio(pack: str) -> str | None:
    """`ensure_pack_activated` no mira nada si el pack ya está activo en el proceso (lo activó otro
    negocio): este negocio puede no tener su fila de configuración. Se completa con la config vacía
    si el schema lo permite; si exige campos reales, se devuelve el aviso."""
    module = plugin_manager.get(pack)
    if module is None or module.config_schema is None or not module.requires_config:
        return None
    async with get_session() as session:
        tiene = (
            await session.execute(
                select(PluginConfig).where(
                    PluginConfig.plugin_id == pack, PluginConfig.tenant_id == get_current_tenant_id()
                )
            )
        ).scalar_one_or_none() is not None
    if tiene:
        return None
    try:
        await plugin_manager.save_config(pack, {})
    except PluginConfigError:
        return f'{pack} necesita configuración -- usa el botón "Configurar" en Habilidades'
    return None


async def configure_plugin_for_tenant(plugin_id: str, values: dict[str, object]) -> None:
    """Guarda la config del plugin en el negocio y lo activa.

    Punto ÚNICO compartido por el panel SSR y la API `/api/v1` (ADR-0076/0077): la lógica de cifrado y
    activación no se duplica. Ya no habilita nada: una habilidad es del negocio solo si algún agente activo la
    tiene instalada (ADR-0206, HU-AOS-91).
    """
    await plugin_manager.save_config(plugin_id, values)
    await plugin_manager.activate(plugin_id)


async def seed_flow_for_current_tenant(pack: str) -> None:
    """Siembra/actualiza el flujo del pack en el `flow.json` del tenant (ADR-0054).

    Best-effort: un fallo de filesystem no puede tumbar la activación (el runtime
    cae al default del pack si el archivo no está). No pisa las secciones propias
    del tenant ni las de otros packs.
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    try:
        from core.agents.flow import seed_tenant_flow
        from core.shared.tenant import tenant_slug

        slug = await tenant_slug(tenant_id)
        if slug:
            seed_tenant_flow(slug, [pack])
    except Exception:
        log.warning("tenant_flow_seed_failed", plugin=pack, exc_info=True)


async def asegurar_habilidades(
    tenant_id: uuid.UUID, packs: Iterable[str], *, actor: uuid.UUID | None
) -> ResultadoHabilitacion:
    """Deja listos para instalar en un agente los `packs` dados: el despliegue los trae y su código queda activo
    (ya no hay cuota `plugins`: ADR-0208 §1). NO escribe la habilitación: una habilidad es del negocio solo si algún
    agente activo la tiene instalada (ADR-0206, HU-AOS-91), así que quien llama la instala en el agente después.

    Un pack que no activa (le falta configuración) sigue y queda en `config_pendiente`: no aporta tools mientras
    no esté activo (el runtime intersecta con lo activo) y el dueño lo configura en Habilidades (`requirements[]`,
    ADR-0155). NO valida `min_plan` (decisión del dueño). Corre dentro del `tenant_scope` del negocio.
    """
    from core.agents.instalaciones_agente import packs_instalados

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("habilitación de packs: fuera del tenant_scope del negocio")
    pedidos = sorted(set(packs))
    sin_modulo = [p for p in pedidos if plugin_manager.get(p) is None]
    if sin_modulo:
        raise HabilitacionError(
            "pack_no_instalado", sin_modulo, "Esta habilidad no está instalada en este despliegue."
        )
    instalados = await packs_instalados(tenant_id, con_core=False)
    ya = tuple(p for p in pedidos if p in instalados)
    nuevos = [p for p in pedidos if p not in instalados]
    if not nuevos:
        return ResultadoHabilitacion(ya_estaban=ya)
    config_pendiente: dict[str, str] = {}
    for pack in nuevos:
        error = await ensure_pack_activated(pack) or await _config_del_negocio(pack)
        if error is not None:
            config_pendiente[pack] = error
        await seed_flow_for_current_tenant(pack)
    log.info(
        "pack_habilitado_por_plantilla",
        tenant_id=str(tenant_id),
        packs=nuevos,
        actor=str(actor) if actor else None,
        config_pendiente=sorted(config_pendiente),
    )
    await record_audit(
        str(actor) if actor else "sistema",
        "skills_enabled_by_template",
        input_data={"packs": nuevos},
        output_data={"config_pendiente": sorted(config_pendiente)},
    )
    return ResultadoHabilitacion(
        habilitados_ahora=tuple(nuevos), ya_estaban=ya, config_pendiente=config_pendiente
    )
