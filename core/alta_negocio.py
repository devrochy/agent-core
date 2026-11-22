"""Alta de un negocio: tenant + asistente + cuenta del admin (ADR-0001, ADR-0048, ADR-0110).

Dar de alta un cliente en agent-core es 100% configuración: tenant + AgentSpec +
AppSettings (con la persona inicial en `agent_defaults`, ADR-0206 §6) + conexión de canal +
preset (solo configuración). Las habilidades no se eligen en el alta: se instalan en el agente que
las usa (ADR-0206, HU-AOS-91). Cero código por cliente.

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
from core.agents import defaults_negocio
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
from core.onboarding import _validate_slug
from core.plugin_manager import plugin_manager
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


def _default_spec_for(slug: str, tenant_id: uuid.UUID) -> dict:
    """El spec v2 del negocio nuevo, sin packs: el alta no elige habilidades (ADR-0206, HU-AOS-91), se instalan en
    el agente que las usa. La persona ya no va aquí: es un valor por defecto de los agentes
    (`app_settings.agent_defaults`, ADR-0206 §6) y la escribe `onboard_tenant`."""
    spec = deepcopy(DEFAULT_SPEC)
    spec["tenantId"] = str(tenant_id)
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
        admin = (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one_or_none()
    if admin is not None and admin.deleted_at is not None:
        # Baja lógica de equipo (ADR-0198): se reactiva la misma cuenta, no se reasigna.
        raise ValueError(
            "la cuenta de ese correo fue eliminada: reactívala antes de dar de alta el negocio"
        )
    return admin


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


async def _agentes_base_al_alta(tenant_id: uuid.UUID, *, nuevo: bool) -> list[str]:
    """El Coordinador nace con el negocio (ADR-0192 §1), antes del preset: sin General. Sus
    instancias en las conexiones conversacionales que ya existan; cada una pide su release 1, que lleva solo al
    Coordinador: el alta no crea especialistas (ADR-0205 §1) y el dueño crea el primero en la bienvenida. Frontera deliberada: un
    fallo se registra y el alta sigue. `nuevo` queda por compatibilidad de firma (los tests la parchean)."""
    del nuevo
    from core.agents.base import asegurar_base_del_negocio

    try:
        async with tenant_scope(tenant_id):
            await asegurar_base_del_negocio(tenant_id)
    except PermissionError:  # un error de scope es un bug, no un fallo tolerable del alta
        raise
    except Exception:  # frontera deliberada: el alta no depende del Agent OS
        log.warning("alta_agentes_base_fallo", tenant_id=str(tenant_id), exc_info=True)
        return ["Coordinador pendiente (reintentar el alta)"]
    return ["Coordinador listo"]


async def onboard_tenant(
    *,
    name: str,
    slug: str,
    business_description: str = "",
    language: str = "es",
    persona: dict | None = None,
    escalation_triggers: list[str] | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
    whatsapp: dict | None = None,
    tenant_id: uuid.UUID | None = None,
    preset: str | None = None,
    plan: str | None = None,
    sembrar_ejemplos: bool = True,
    agentes_base: bool = True,
) -> tuple[uuid.UUID, list[str]]:
    """Da de alta un tenant con su asistente. Idempotente.

    `whatsapp`: {"label", "phone_number_id", "app_secret", "access_token",
    "verify_token"}. Si se pasa, las credenciales se cifran y se persisten
    (se espera que el llamador las haya verificado contra Meta antes). El
    `phone_number_id` no puede pertenecer ya a OTRO tenant (evita ruteo
    cross-tenant del webhook). `preset`: id de `core/presets/` (ADR-0110), solo siembra configuración;
    el alta no elige habilidades (ADR-0206, HU-AOS-91). `plan` asigna el plan de la suscripción inicial
    (sin él queda free y un preset de plan mayor falla). `agentes_base=False` no crea el General ni el
    Coordinador: lo usan los journeys, que montan su propia release.
    Devuelve (tenant_id, pasos).
    """
    _validate_slug(slug)

    if whatsapp and whatsapp.get("phone_number_id"):
        if await _phone_claimed_by_other(whatsapp.get("phone_number_id", ""), tenant_id):
            raise ValueError(
                f"phone_number_id {whatsapp.get('phone_number_id')} ya pertenece a otro tenant"
            )

    tenant_id = tenant_id or uuid.uuid4()
    steps: list[str] = []
    negocio_nuevo = False

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
            # Negocio nuevo: los datos de clientes en disco con este slug son de otro (purgado).
            onboarding.cuarentena_de_datos_huerfanos(slug)
            tenant = Tenant(
                id=tenant_id, name=name, slug=slug, status="active", settings={"language": language}
            )
            session.add(tenant)
            await session.flush()
            negocio_nuevo = True
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
                settings_row = AppSettings(
                    tenant_id=tenant_id,
                    assistant_name=name,
                    business_description=business_description,
                    language=language,
                    # `escalation.triggers` del AgentSpec nunca se lee en runtime
                    # (core/orchestrator/consumer.py lee este campo) -- ver
                    # `_default_spec_for`, que ya NO escribe ahí.
                    escalation_config={"keywords": list(escalation_triggers)}
                    if escalation_triggers
                    else {},
                )
                session.add(settings_row)
                steps.append("app_settings creados")
            else:
                steps.append("app_settings ya existían")
            # La persona inicial va a los valores por defecto de los agentes (ADR-0206 §6), solo si el negocio
            # todavía no tiene (alta idempotente: un re-alta no pisa lo que se cambió en Configuración).
            if persona and not settings_row.agent_defaults:
                try:
                    defaults_negocio.escribir(settings_row, {"persona": persona})
                except ValueError as exc:
                    campos = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()}) if hasattr(exc, "errors") else []
                    raise ValueError(f"Persona inválida para {slug}: {', '.join(campos) or 'revisa los valores'}") from None
                steps.append("persona inicial guardada")

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
                        spec=_default_spec_for(slug, tenant_id),
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

    # El Coordinador antes del preset (ADR-0192 §1): los agentes del preset lo necesitan.
    if agentes_base:
        steps.extend(await _agentes_base_al_alta(tenant_id, nuevo=negocio_nuevo))

    if preset:
        from core.presets.apply import apply_preset

        resultado = await apply_preset(tenant_id, preset, sembrar_ejemplos=sembrar_ejemplos)
        steps.extend(resultado.pasos)

    onboarding.ensure_tenant_docs(slug, name, business_description)
    await onboarding.ensure_brand_voice(tenant_id, slug)
    return tenant_id, steps
