"""Habilitación de packs ("habilidades") por negocio: fuente única SSR + API + editor de agentes.

La habilitación vive en `AgentSpec.packs` (ADR-0019/0118). Antes estas funciones estaban en
`core.admin.views` (capa SSR) y el editor de agentes no podía importarlas sin cruzar capas; viven
aquí y `core.admin.views` las reexporta con sus nombres de siempre.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select

from core.agents.spec import (
    count_tenant_packs,
    leer_spec_activo,
    load_active_spec,
    save_spec_version,
)
from core.billing.enforcement import QuotaExceeded, quota_decision
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
    módulo) o `concurrent_edit` (otra edición del spec ganó la carrera dos veces seguidas)."""

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
        if module is not None and module.config_schema is not None:
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
    if module is None or module.config_schema is None:
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
    """Guarda la config del plugin, lo activa y lo habilita para el tenant actual.

    Punto ÚNICO compartido por el panel SSR y la API `/api/v1` (ADR-0076/0077): la
    lógica de cifrado, activación y cuota del pack no se duplica. Levanta
    `QuotaExceeded` si el plan no permite habilitar otro pack.
    """
    # La cuota se decide ANTES de persistir: al tope del plan no se guarda nada (P0 de
    # pruebas 2026-10-17; antes quedaba la config guardada con el pack deshabilitado).
    await verificar_cuota_si_no_habilitado(plugin_id)
    await plugin_manager.save_config(plugin_id, values)
    await plugin_manager.activate(plugin_id)
    await enable_pack_for_tenant(plugin_id)


async def verificar_cuota_si_no_habilitado(pack: str) -> None:
    spec = await load_active_spec()
    if pack in spec.packs:
        return
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    decision = await quota_decision(tenant_id, "plugins")
    if not decision.allowed:
        raise QuotaExceeded("plugins", count_tenant_packs(spec), decision.limit or 0)


async def enable_pack_for_tenant(pack: str) -> str | None:
    """Agrega `pack` a `AgentSpec.packs` del tenant actual (nueva versión).

    Aplica la cuota de plugins del plan (ADR-0045): si ya está en el límite y el
    pack no estaba habilitado, levanta `QuotaExceeded`.
    """
    spec = await load_active_spec()
    if pack in spec.packs:
        return None
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        decision = await quota_decision(tenant_id, "plugins")
        if not decision.allowed:
            raise QuotaExceeded("plugins", count_tenant_packs(spec), decision.limit or 0)
    new_spec = spec.model_copy(update={"packs": sorted({*spec.packs, pack})})
    version = await save_spec_version(new_spec)
    await seed_flow_for_current_tenant(pack)
    return version


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


async def disable_pack_for_tenant(pack: str) -> str | None:
    """Quita `pack` de `AgentSpec.packs` del tenant actual (nueva versión).

    NO desactiva el código globalmente — otro tenant puede seguir usándolo.
    """
    spec = await load_active_spec()
    if pack not in spec.packs:
        return None
    new_spec = spec.model_copy(update={"packs": sorted(set(spec.packs) - {pack})})
    return await save_spec_version(new_spec)


async def set_pack_for_tenant(pack: str, enabled: bool) -> str | None:
    """Habilita/deshabilita un pack para el tenant actual (fuente única SSR + API).

    Devuelve un mensaje de error, o `None` si quedó aplicado. Levanta
    `QuotaExceeded` si el plan no permite habilitar otro pack.
    """
    if enabled:
        error = await ensure_pack_activated(pack)
        if error is not None:
            return error
        await enable_pack_for_tenant(pack)
        return None
    await disable_pack_for_tenant(pack)
    return None


async def asegurar_habilidades(
    tenant_id: uuid.UUID, packs: Iterable[str], *, actor: uuid.UUID | None
) -> ResultadoHabilitacion:
    """Deja habilitados en `AgentSpec.packs` los `packs` dados (el alta del asistente los necesita).

    Habilitar es un conjunto: lo que ya estaba no se toca ni crea versión de spec. Todo lo nuevo entra en
    UNA sola versión de spec. Un pack que no activa (le falta configuración) se habilita igual y queda en
    `config_pendiente`: no aporta tools mientras no esté activo (el runtime intersecta con lo activo) y el
    dueño lo configura en Habilidades (`requirements[]`, ADR-0155). NO valida `min_plan` (decisión del
    dueño); sí la cuota `plugins`, que hoy el plan no define. Corre dentro del `tenant_scope` del negocio.
    """
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("habilitación de packs: fuera del tenant_scope del negocio")
    pedidos = sorted(set(packs))
    # Todo lo que puede fallar sin efectos va antes de escribir.
    sin_modulo = [p for p in pedidos if plugin_manager.get(p) is None]
    if sin_modulo:
        raise HabilitacionError(
            "pack_no_instalado", sin_modulo, "Esta habilidad no está instalada en este despliegue."
        )
    spec = await load_active_spec()
    ya = tuple(p for p in pedidos if p in spec.packs)
    nuevos = [p for p in pedidos if p not in spec.packs]
    if not nuevos:
        return ResultadoHabilitacion(ya_estaban=ya)
    decision = await quota_decision(tenant_id, "plugins")
    if not decision.allowed:
        raise QuotaExceeded("plugins", count_tenant_packs(spec), decision.limit or 0)

    config_pendiente: dict[str, str] = {}
    for pack in nuevos:
        error = await ensure_pack_activated(pack) or await _config_del_negocio(pack)
        if error is not None:
            config_pendiente[pack] = error
    habilitados: list[str] = []
    for intento in range(2):  # un reintento si otra edición del spec ganó la carrera
        # Fila e id en UNA lectura, y se guarda solo si esa fila sigue activa: sin eso, dos plantillas a la
        # vez leían el mismo spec, ambas confirmaban y la segunda borraba la habilidad de la primera.
        leido = await leer_spec_activo(tenant_id)
        if leido is None:
            await load_active_spec()  # negocio sin spec: se siembra el default y se relee
            continue
        base_id, actual = leido
        habilitados = [p for p in nuevos if p not in actual.packs]
        if not habilitados:
            break
        version = await save_spec_version(
            actual.model_copy(update={"packs": sorted({*actual.packs, *habilitados})}), tenant_id, base_id=base_id
        )
        if version is not None:
            for pack in habilitados:
                await seed_flow_for_current_tenant(pack)
            break
        log.warning("habilitacion_reintento_spec", tenant_id=str(tenant_id), packs=habilitados, intento=intento)
    else:
        log.warning("habilitacion_conflicto", tenant_id=str(tenant_id), packs=nuevos)
        raise HabilitacionError(
            "concurrent_edit", nuevos, "Otra edición del negocio ganó la carrera: vuelve a intentarlo."
        )
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
