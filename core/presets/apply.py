"""`apply_preset`: aplica un preset a un negocio (ADR-0110 § 3, HU-FWK-03).

Idempotente: volver a aplicarlo no duplica nada y nunca pisa lo que el negocio ya
cambió. Un pack que exige claves que no tiene queda **pendiente de configurar**: el
alta no falla.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from core.agents.spec import DEFAULT_SPEC, load_active_spec, save_spec_if_changed
from core.db.models import AppSettings, Tenant
from core.db.session import get_session
from core.presets import PresetError, PresetManifest, load_fragment, load_preset
from core.shared.audit import record_audit
from core.shared.exceptions import PluginConfigError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

# Defaults de columnas de `AppSettings`: el preset solo cambia lo que siga así.
_APP_SETTINGS_DEFAULTS = {"language": "es", "currency": "COP", "timezone": "America/Bogota"}


@dataclass
class PresetResult:
    preset: str
    version: int
    habilitados: list[str] = field(default_factory=list)
    pendientes: list[dict[str, str]] = field(default_factory=list)
    spec_version: str | None = None
    pasos: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ plan
async def _plan_del_negocio(tenant_id: uuid.UUID) -> Any:
    from core.billing.plans import get_plan
    from core.billing.service import get_active_subscription

    sub = await get_active_subscription(tenant_id)
    return get_plan(sub.plan_id if sub else "free")


def _rango(plan_id: str) -> int:
    from core.billing.plans import get_plan

    plan = get_plan(plan_id)
    if plan is None:
        raise PresetError(f"Plan desconocido: {plan_id!r}")
    return plan.sort_order


def _validar_plan(manifest: PresetManifest, plan: Any, modules: dict[str, Any]) -> None:
    requerido = max(
        [_rango(manifest.plan_minimo), *(_rango(getattr(m, "min_plan", "free")) for m in modules.values())]
    )
    if plan is None or plan.sort_order < requerido:
        nombre = plan.id if plan else "sin plan"
        raise PresetError(
            f"El preset {manifest.id} necesita un plan más alto (plan actual: {nombre})."
        )
    limite = (plan.limits or {}).get("plugins")
    if limite is not None and len(manifest.packs) > limite:
        raise PresetError(f"El plan {plan.id} permite {limite} habilidades; el preset usa {len(manifest.packs)}.")


# ------------------------------------------------------------------ config
def _crudo(config: Any) -> dict[str, Any]:
    """Valores ya guardados por el negocio (solo los que puso él: `model_fields_set`)."""
    if config is None:
        return {}
    valores = {}
    for name in config.model_fields_set:
        valor = getattr(config, name)
        valores[name] = valor.get_secret_value() if isinstance(valor, SecretStr) else valor
    return valores


async def _configurar_pack(pack_id: str, module: Any, vertical: str) -> tuple[bool, str]:
    """Completa la config del pack sin pisar lo existente. (habilitable, detalle)."""
    from core.plugin_manager import plugin_manager

    fragmento = load_fragment(pack_id, vertical, module.config_schema)
    try:
        existente = _crudo(await plugin_manager.load_config(pack_id))
    except PluginConfigError:
        existente = {}
    merged = {**fragmento.config, **existente}
    if module.config_schema is not None:
        try:
            module.config_schema.model_validate(merged)
        except ValidationError as exc:
            faltan = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
            return False, f"faltan datos: {', '.join(faltan)}"
    await plugin_manager.save_config(pack_id, merged)
    await plugin_manager.activate(pack_id)
    return True, "configurado"


# ------------------------------------------------------------------ spec y settings
def _playbook_refs(packs: list[str], nombres: list[str]) -> list[dict[str, str]]:
    from core.onboarding import _pack_playbook_refs

    refs = _pack_playbook_refs(packs)
    if nombres:
        refs = [r for r in refs if r["name"] in nombres]
    return refs


async def _actualizar_spec(manifest: PresetManifest, habilitados: list[str], tenant_id: uuid.UUID) -> str | None:
    spec = await load_active_spec()
    data: dict[str, Any] = spec.model_dump(mode="json")
    data["packs"] = sorted({*data.get("packs", []), *habilitados})
    existentes = {p["name"] for p in data.get("playbooks", [])}
    for ref in _playbook_refs(habilitados, manifest.playbooks):
        if ref["name"] not in existentes:
            data["playbooks"].append(ref)
    defaults = DEFAULT_SPEC["persona"]
    for clave, valor in manifest.persona.model_dump(exclude_none=True).items():
        # Solo si el negocio no lo cambió (sigue en el default del spec).
        if data["persona"].get(clave) == defaults.get(clave):
            data["persona"][clave] = valor
    return await save_spec_if_changed(spec, data, tenant_id)


async def _actualizar_app_settings(manifest: PresetManifest, tenant_id: uuid.UUID) -> None:
    cambios = manifest.app_settings.model_dump(exclude_none=True)
    if not cambios:
        return
    async with get_session() as session:
        fila = (
            await session.execute(
                select(AppSettings).where(AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if fila is None:
            fila = AppSettings(tenant_id=tenant_id)
            session.add(fila)
        behavior = dict(fila.behavior_config or {})
        if "split_style" in cambios and "split_style" not in behavior:
            behavior["split_style"] = cambios["split_style"]
            fila.behavior_config = behavior
        for columna, default in _APP_SETTINGS_DEFAULTS.items():
            if columna in cambios and getattr(fila, columna, None) in (None, default):
                setattr(fila, columna, cambios[columna])


async def _registrar(tenant_id: uuid.UUID, resultado: PresetResult) -> None:
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is not None:
            settings = dict(tenant.settings or {})
            settings["preset"] = {
                "id": resultado.preset,
                "version": resultado.version,
                "aplicado_en": datetime.now(UTC).isoformat(),
                "pendientes": resultado.pendientes,
            }
            tenant.settings = settings
    await record_audit(
        actor="preset",
        action="preset_aplicado",
        output_data={
            "preset": resultado.preset,
            "version": resultado.version,
            "habilitados": resultado.habilitados,
            "pendientes": resultado.pendientes,
        },
    )


# ------------------------------------------------------------------ entrada
async def apply_preset(tenant_id: uuid.UUID, preset_id: str) -> PresetResult:
    """Aplica `preset_id` al negocio (ver ADR-0110 § 3). Lanza `PresetError` sin
    cambiar nada si el preset no es válido o el plan no alcanza."""
    from core.plugin_manager import plugin_manager

    manifest = load_preset(preset_id)
    plugin_manager.scan()
    modules = {}
    for pack_id in manifest.packs:
        module = plugin_manager.get(pack_id)
        if module is None:
            raise PresetError(f"El preset {preset_id} usa {pack_id}, que no está instalado")
        modules[pack_id] = module
        load_fragment(pack_id, preset_id, module.config_schema)  # valida antes de tocar nada

    resultado = PresetResult(preset=manifest.id, version=manifest.version)
    async with tenant_scope(tenant_id):
        _validar_plan(manifest, await _plan_del_negocio(tenant_id), modules)

        for pack_id, module in modules.items():
            habilitable, detalle = await _configurar_pack(pack_id, module, preset_id)
            if habilitable:
                resultado.habilitados.append(pack_id)
            else:
                resultado.pendientes.append({"pack": pack_id, "paso": f"configurar_{pack_id}", "detalle": detalle})
                resultado.pasos.append(f"{pack_id} pendiente de configurar ({detalle})")

        resultado.spec_version = await _actualizar_spec(manifest, resultado.habilitados, tenant_id)
        await _actualizar_app_settings(manifest, tenant_id)

        for pack_id in resultado.habilitados:
            fragmento = load_fragment(pack_id, preset_id, modules[pack_id].config_schema)
            if fragmento.seed:
                resultado.pasos.extend(await modules[pack_id].seed_data(tenant_id, fragmento.seed))

        await _registrar(tenant_id, resultado)

    resultado.pasos.insert(0, f"preset {manifest.id} v{manifest.version} aplicado")
    log.info(
        "preset_aplicado",
        tenant_id=str(tenant_id),
        preset=manifest.id,
        habilitados=resultado.habilitados,
        pendientes=[p["pack"] for p in resultado.pendientes],
    )
    return resultado
