"""Alta de un negocio: tenant + asistente + cuenta del admin (ADR-0001, ADR-0048, ADR-0110).

Dar de alta un cliente en agent-core es 100% configuración: tenant + AgentSpec +
AppSettings + conexión de canal + enablement de packs + preset. Cero código por cliente.

Vive aparte de `core/onboarding.py` (utilidades de los artefactos del tenant, que también
usan los packs) para que el contrato `alta-fuera-del-llm` de `.importlinter` pueda
prohibírsela a las tools y a los packs (ADR-0121): solo la usan el master (panel, API de
plataforma y CLI) y el consumidor de aprovisionamiento del core.
"""

from __future__ import annotations

import uuid
from copy import deepcopy

from agentcore_contracts import AgentSpec
from sqlalchemy import select

from core import onboarding
from core.agents.flow import seed_tenant_flow
from core.agents.spec import DEFAULT_SPEC
from core.db.models import (
    Admin,
    AgentSpecRecord,
    AppSettings,
    ChannelConnection,
    Tenant,
    TenantUser,
)
from core.db.session import get_session
from core.onboarding import _pack_playbook_refs, _validate_slug
from core.plugin_manager import plugin_manager
from core.shared.exceptions import PluginConfigError, PluginError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


def _default_spec_for(
    slug: str,
    tenant_id: uuid.UUID,
    persona: dict | None,
    packs: list[str] | None,
) -> dict:
    spec = deepcopy(DEFAULT_SPEC)
    spec["tenantId"] = str(tenant_id)
    if persona:
        spec["persona"].update(persona)
    if packs:
        spec["packs"] = list(packs)
        existing = {p["name"] for p in spec.get("playbooks", [])}
        for ref in _pack_playbook_refs(packs):
            if ref["name"] not in existing:
                spec["playbooks"].append(ref)
    # El contrato congelado es rey: un spec inválido NO llega a la BD (evita que
    # /admin/spec reviente al renderizar con model_validate).
    try:
        AgentSpec.model_validate(spec)
    except Exception as exc:
        raise ValueError(f"AgentSpec inválido para {slug}: {exc}") from exc
    return spec


async def _resolve_admin(email: str | None) -> Admin | None:
    if not email:
        return None
    email = email.strip().lower()
    async with get_session() as session:
        return (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one_or_none()


async def _phone_claimed_by_other(phone_number_id: str, tenant_id: uuid.UUID | None) -> bool:
    """¿Otro tenant ya tiene este número? (SECURITY DEFINER, exento de RLS; ADR-0095).

    Evita que dos tenants reclamen el mismo número. No usa el resolvedor de ruteo:
    ese devuelve NULL si el número ya es ambiguo, y un número ambiguo quedaría libre
    para un tercero. `tenant_id=None` (tenant nuevo): cualquier dueño cuenta como otro.
    """
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant

    return await asset_claimed_by_other_tenant(tenant_id, phone_number_id=phone_number_id)




async def motivo_para_rechazar_alta_de_tester(slug: str, admin_email: str | None) -> str | None:
    """Un tester (ADR-0134) da de alta negocios nuevos, pero no gestiona cuentas: no puede
    reusar el slug de un negocio que ya existe (el alta es idempotente y vincularía al owner
    que mande) ni vincular como owner una cuenta que ya existe. `None` = puede seguir."""
    async with get_session() as session:
        existe = (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one_or_none()
        if existe is not None:
            return "Ya existe un negocio con ese slug. Un tester solo da de alta negocios nuevos."
        correo = (admin_email or "").strip().lower()
        if correo:
            cuenta = (await session.execute(select(Admin.id).where(Admin.email == correo))).scalar_one_or_none()
            if cuenta is not None:
                return "Ese correo ya tiene una cuenta. Un tester solo da de alta negocios con una cuenta nueva."
    return None


async def onboard_tenant(
    *,
    name: str,
    slug: str,
    business_description: str = "",
    language: str = "es",
    persona: dict | None = None,
    escalation_triggers: list[str] | None = None,
    packs: list[str] | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
    whatsapp: dict | None = None,
    tenant_id: uuid.UUID | None = None,
    preset: str | None = None,
    plan: str | None = None,
    sembrar_ejemplos: bool = True,
) -> tuple[uuid.UUID, list[str]]:
    """Da de alta un tenant con su asistente. Idempotente.

    `whatsapp`: {"label", "phone_number_id", "app_secret", "access_token",
    "verify_token"}. Si se pasa, las credenciales se cifran y se persisten
    (se espera que el llamador las haya verificado contra Meta antes). El
    `phone_number_id` no puede pertenecer ya a OTRO tenant (evita ruteo
    cross-tenant del webhook). `preset`: id de `core/presets/` (ADR-0110), se aplica
    después de los `packs` sueltos; `plan` asigna el plan de la suscripción inicial
    (sin él queda free y un preset de plan mayor falla). Devuelve (tenant_id, pasos).
    """
    _validate_slug(slug)

    if whatsapp and whatsapp.get("phone_number_id"):
        if await _phone_claimed_by_other(whatsapp.get("phone_number_id", ""), tenant_id):
            raise ValueError(
                f"phone_number_id {whatsapp.get('phone_number_id')} ya pertenece a otro tenant"
            )

    tenant_id = tenant_id or uuid.uuid4()
    steps: list[str] = []

    plugin_manager.scan()
    await plugin_manager.sync_registry()

    admin_email = admin_email.strip().lower() if admin_email else None
    if admin_password is not None and len(admin_password) < 8:
        raise ValueError("La contraseña del administrador necesita 8+ caracteres")
    admin = await _resolve_admin(admin_email)
    if admin is None and admin_email and admin_password:
        # Alta del administrador del tenant (ADR-0048): si el email no existe y
        # se dio contraseña, se crea. Sin contraseña, se mantiene el
        # comportamiento histórico (solo vincula un admin preexistente).
        from core.admin.auth import hash_password

        async with get_session() as session:
            admin = Admin(
                email=admin_email,
                password_hash=hash_password(admin_password),
                role="colaborador",
            )
            session.add(admin)
            await session.flush()
        steps.append("admin del tenant creado")

    async with get_session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                id=tenant_id, name=name, slug=slug, status="active", settings={"language": language}
            )
            session.add(tenant)
            await session.flush()
            steps.append(f"tenant creado ({slug})")
        else:
            tenant_id = tenant.id
            steps.append(f"tenant ya existía ({slug})")

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            settings_row = (
                await session.execute(
                    select(AppSettings).where(
                        AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if settings_row is None:
                session.add(
                    AppSettings(
                        tenant_id=tenant_id,
                        assistant_name=name,
                        business_description=business_description,
                        language=language,
                        behavior_config={"tono": persona.get("tone", "calido") if persona else "calido"},
                        # `escalation.triggers` del AgentSpec nunca se lee en runtime
                        # (core/orchestrator/consumer.py lee este campo) -- ver
                        # `_default_spec_for`, que ya NO escribe ahí.
                        escalation_config={"keywords": list(escalation_triggers)}
                        if escalation_triggers
                        else {},
                    )
                )
                steps.append("app_settings creados")
            else:
                steps.append("app_settings ya existían")

            spec_row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tenant_id,
                        AgentSpecRecord.is_active.is_(True),
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if spec_row is None:
                session.add(
                    AgentSpecRecord(
                        tenant_id=tenant_id,
                        version="1.0.0",
                        is_active=True,
                        spec=_default_spec_for(slug, tenant_id, persona, packs),
                    )
                )
                steps.append("agent_spec creado")
            else:
                steps.append("agent_spec ya existía")

            if admin is not None:
                link = (
                    await session.execute(
                        select(TenantUser).where(
                            TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin.id
                        )
                    )
                ).scalar_one_or_none()
                if link is None:
                    session.add(TenantUser(tenant_id=tenant_id, admin_id=admin.id, role="owner"))
                    steps.append("tenant_user (admin → tenant) creado")
                else:
                    steps.append("tenant_user ya existía")
            else:
                steps.append("sin admin (se omite tenant_user)")

            if whatsapp:
                conn = (
                    await session.execute(
                        select(ChannelConnection).where(
                            ChannelConnection.tenant_id == tenant_id,
                            ChannelConnection.external_id == whatsapp.get("phone_number_id", ""),
                            # Solo las manuales (la canónica de un número de Meta, ADR-0115).
                            ChannelConnection.meta_connection_id.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if conn is None:
                    from core.gateway.connections import save_connection

                    await save_connection(
                        whatsapp.get("label") or name,
                        {
                            "phone_number_id": whatsapp.get("phone_number_id", ""),
                            "app_secret": whatsapp.get("app_secret", ""),
                            "access_token": whatsapp.get("access_token", ""),
                            "verify_token": whatsapp.get("verify_token", ""),
                        },
                    )
                    steps.append("conexión WhatsApp creada")
                else:
                    steps.append("conexión WhatsApp ya existía")
            from core.billing.service import ensure_free_subscription

            sub = await ensure_free_subscription(session, tenant_id)
            steps.append("suscripción free creada")
            if plan and sub.plan_id != plan:
                from core.billing.plans import get_plan

                if get_plan(plan) is None:
                    raise ValueError(f"Plan desconocido: {plan}")
                # Lo decide el master (CLI) o el aprovisionamiento con el plan comprado.
                sub.plan_id = plan
                sub.status = "active"
                steps.append(f"plan {plan} asignado")
            await session.commit()

    for pack in packs or []:
        async with tenant_scope(tenant_id):
            try:
                await plugin_manager.save_config(pack, {})
                await plugin_manager.activate(pack)
            except (PluginConfigError, PluginError) as exc:
                # Un pack que exige claves (p. ej. pack_payments) no rompe el alta:
                # queda pendiente de configurar desde el panel (ADR-0110).
                steps.append(f"pack {pack} pendiente de configurar ({exc})")
                continue
        steps.append(f"pack {pack} configurado y activo")

    if preset:
        from core.presets.apply import apply_preset

        resultado = await apply_preset(tenant_id, preset, sembrar_ejemplos=sembrar_ejemplos)
        steps.extend(resultado.pasos)

    # Flujo de atención (ADR-0054, ADR-0109): el flujo de los packs se compone en
    # runtime; ya no se copia al flow.json del negocio. Esto solo normaliza un
    # archivo existente (quita copias viejas). Best-effort: no tumba el alta.
    if packs:
        try:
            steps.extend(seed_tenant_flow(slug, list(packs)))
        except Exception:
            log.warning("tenant_flow_seed_failed", slug=slug, exc_info=True)

    onboarding.ensure_tenant_docs(slug, name, business_description)
    await onboarding.ensure_brand_voice(tenant_id, slug)
    return tenant_id, steps
