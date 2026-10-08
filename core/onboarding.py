"""Onboarding de tenants (multi-tenant, ADR-0001).

Dar de alta un cliente en agent-core es 100% configuración: tenant + AgentSpec +
AppSettings + conexión de canal + enablement de packs. Cero código por cliente.

Los artefactos de cada tenant (catálogo, evals, documentación) viven en
`TENANTS_ROOT/<slug>/` (default `./tenants`, gitignored) — fuera del código del
framework, para que agent-core no mezcle documentación ni datos de sus clientes.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from pathlib import Path

from agentcore_contracts import AgentSpec
from sqlalchemy import select

from core.agents.flow import seed_tenant_flow
from core.agents.spec import DEFAULT_SPEC
from core.config import get_settings
from core.db.models import (
    Admin,
    AgentSpecRecord,
    AppSettings,
    ChannelConnection,
    Tenant,
    TenantUser,
)
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.exceptions import PluginConfigError, PluginError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def tenants_root() -> Path:
    """Directorio de tenants (TENANTS_ROOT). Relativo al repo si no es absoluto."""
    raw = get_settings().tenants_root
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def tenant_dir(slug: str) -> Path:
    """Carpeta de artefactos del tenant: `TENANTS_ROOT/<slug>/`."""
    return tenants_root() / slug


def ensure_tenant_docs(slug: str, name: str, business_description: str) -> Path:
    """Crea `TENANTS_ROOT/<slug>/docs/` con un README stub (idempotente).

    La documentación específica del tenant vive aquí, no en docs/ del framework.
    """
    docs = tenant_dir(slug) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    readme = docs / "README.md"
    if not readme.exists():
        readme.write_text(
            "\n".join(
                [
                    f"# {name}",
                    "",
                    "Documentación específica de este tenant. Aquí vive la identidad, "
                    "decisiones de configuración y changelog del asistente — separada "
                    "de la documentación del framework (docs/).",
                    "",
                    f"Negocio: {business_description or '—'}",
                    "",
                    "## Estructura",
                    "",
                    "- `catalog.json` — datos del tenant importables a la BD "
                    "(`agent-core tenant seed <slug>`).",
                    "- `docs/` — documentación específica (este README).",
                    "- `evals/` — golden set de conversaciones del tenant.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return docs


def load_tenant_catalog(slug: str) -> dict | None:
    """Lee `TENANTS_ROOT/<slug>/catalog.json`. None si no existe o es inválido."""
    catalog = tenant_dir(slug) / "catalog.json"
    if not catalog.exists():
        return None
    try:
        return json.loads(catalog.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("tenant_catalog_invalid", slug=slug, error=str(exc))
        return None


def _pack_playbook_refs(packs: list[str]) -> list[dict[str, str]]:
    """Playbooks de cada pack habilitado (allowlist del spec).

    Al habilitar un pack, sus playbooks (packages/pack-*/playbooks o
    plugins/*/playbooks) entran al índice `AgentSpec.playbooks` — genérico y por
    tenant (antes se hacía con una migración por cliente, ej. 0005).
    """
    from core.agents.playbooks import playbook_roots

    refs: list[dict[str, str]] = []
    for root in playbook_roots():
        folder = root.parent.name.replace("-", "_")
        if folder not in packs:
            continue
        for file in sorted(root.glob("*.md")):
            refs.append({"name": file.stem, "description": file.stem})
    return refs


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


def _validate_slug(slug: str) -> None:
    """Slug seguro para usar como path (TENANTS_ROOT/<slug>/). Evita traversal."""
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        raise ValueError(f"Slug inválido (solo minúsculas, números y guiones): {slug!r}")


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

        resultado = await apply_preset(tenant_id, preset)
        steps.extend(resultado.pasos)

    # Flujo de atención (ADR-0054, ADR-0109): el flujo de los packs se compone en
    # runtime; ya no se copia al flow.json del negocio. Esto solo normaliza un
    # archivo existente (quita copias viejas). Best-effort: no tumba el alta.
    if packs:
        try:
            steps.extend(seed_tenant_flow(slug, list(packs)))
        except Exception:
            log.warning("tenant_flow_seed_failed", slug=slug, exc_info=True)

    ensure_tenant_docs(slug, name, business_description)
    await ensure_brand_voice(tenant_id, slug)
    return tenant_id, steps


async def ensure_brand_voice(tenant_id: uuid.UUID, slug: str, *, sobrescribir: bool = False) -> bool:
    """Siembra `AppSettings.brand_voice` desde `TENANTS_ROOT/<slug>/docs/voz-de-marca.md`
    si está vacío (#158). La voz de marca es la fuente de verdad de políticas; un
    asistente sin ella alucina. Devuelve True si la sembró.

    `sobrescribir=True` (HU-SH-01) la reemplaza aunque ya tenga texto: el archivo
    versionado del negocio manda y el runbook lo reaplica tras corregirlo.
    """
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _validate_slug(slug)  # nunca salir de TENANTS_ROOT con un slug armado (auditoría HU-SH-01)
    voice_path = tenant_dir(slug) / "docs" / "voz-de-marca.md"
    if not voice_path.exists():
        return False
    content = voice_path.read_text(encoding="utf-8").strip()
    if not content:
        return False
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            settings = (
                await session.execute(
                    select(AppSettings).where(
                        AppSettings.tenant_id == tenant_id,
                        AppSettings.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if settings is None:
                settings = AppSettings(tenant_id=tenant_id)
                session.add(settings)
                await session.flush()
            if settings.brand_voice and settings.brand_voice.strip():
                if not sobrescribir or settings.brand_voice.strip() == content:
                    return False
            settings.brand_voice = content
    return True