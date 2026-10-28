"""Plataforma (master): `GET /api/v1/admin/platform/tenants` y la App de Meta.

Listado **de todos** los tenants para gestión de plataforma, con su plan. Solo
para el admin `master`. Paginado (la vista SSR recorre todos y calcula el plan de
cada uno; aquí solo de la página) y con búsqueda por nombre/slug.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, or_, select

from core.admin.plan_view import subscription_overview_or_none
from core.admin.roles import actua_como_master, es_master, es_tester
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from core.api.routers.plantillas_opciones import OpcionesErrorOut
from core.api.schemas import ReadinessRun, readiness_run_out
from core.db.models import Tenant
from core.db.session import get_session

router = APIRouter(prefix="/admin/platform", tags=["admin · plataforma"])


def _require_master(principal: Principal) -> None:
    # Master, o tester en test (ADR-0134): operar negocios no es gestionar cuentas.
    if not actua_como_master(principal.admin):
        raise ApiError.forbidden("Solo el admin de plataforma puede gestionar negocios.")


class PlatformTenant(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    plan_name: str
    plan_status: str
    operational: bool


@router.get("/tenants", response_model=Page[PlatformTenant])
async def list_platform_tenants(
    principal: Annotated[Principal, Depends(get_principal)],
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(description="Busca por nombre o slug.")] = None,
) -> Page[PlatformTenant]:
    _require_master(principal)

    query = select(Tenant).where(Tenant.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        query = query.where(or_(Tenant.name.ilike(like), Tenant.slug.ilike(like)))

    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0
        rows = (
            await session.execute(
                query.order_by(Tenant.created_at)
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).scalars().all()

    items: list[PlatformTenant] = []
    for t in rows:
        overview = await subscription_overview_or_none(t.id)
        items.append(
            PlatformTenant(
                id=t.id,
                name=t.name,
                slug=t.slug,
                status=t.status,
                created_at=t.created_at,
                plan_name=overview.plan_name if overview else "—",
                plan_status=overview.status if overview else "sin_suscripcion",
                operational=overview.operational if overview else True,
            )
        )
    return build_page(items, int(total), params)


class TenantCreate(BaseModel):
    name: str
    slug: str = ""
    business: str = ""
    language: str = "es"
    tone: str = "cercano"
    packs: list[str] = []
    escalation_triggers: list[str] = []
    admin_email: str = ""
    # Ya no se define una contraseña al crear el negocio (ADR-0175): el dueño la define con el
    # enlace que le llega por correo. Se conserva el campo para dar un error claro (400) a quien
    # todavía lo mande; vacío es válido.
    admin_password: str = Field(default="", json_schema_extra={"deprecated": True})


class TenantPlanUpdate(BaseModel):
    plan_id: str
    # Días que se suman al período; negativo no tiene sentido (422).
    extend_days: int = Field(default=0, ge=0, le=3660)


class PurgeRequest(BaseModel):
    confirm_phrase: str


class TenantOwnerInvitation(BaseModel):
    """Qué pasó con el acceso del dueño (ADR-0175). Nunca lleva el enlace ni el token."""

    estado: Literal["enviado", "sin_smtp", "fallido", "ya_emitida", "ya_es_miembro"]
    # `enlace`: define su contraseña con el enlace del correo; `app`: acepta la invitación en la app.
    tipo: Literal["enlace", "app"]
    expires_at: datetime | None = None


class TenantCreated(BaseModel):
    tenant_id: uuid.UUID
    slug: str
    steps: list[str]
    # None si no se pasó `admin_email`.
    invitation: TenantOwnerInvitation | None = None


@router.post("/tenants", response_model=TenantCreated, status_code=201)
async def create_tenant(
    payload: TenantCreate,
    principal: Annotated[Principal, Depends(get_principal)],
) -> TenantCreated:
    """Alta de un negocio (master): tenant + asistente + acceso del dueño por correo.

    Con `admin_email` la cuenta del dueño nace sin contraseña y le llega un enlace de un solo
    uso para definirla (ADR-0175). Si el correo no sale, el negocio se crea igual y la
    respuesta lo dice (`invitation.estado`); se reenvía con `owner-invitation/resend`."""
    _require_master(principal)

    # Import local: la lógica de alta vive en el core; el slug se normaliza igual.
    from sqlalchemy.exc import IntegrityError

    from core.admin.views import _slugify
    from core.alta_acceso import (
        AccesoRechazado,
        asegurar_cuenta_sin_contrasena,
        correo_valido,
        emitir_y_enviar_acceso,
        motivo_para_no_dar_acceso,
    )
    from core.alta_negocio import onboard_tenant
    from core.onboarding import _validate_slug

    slug = payload.slug.strip() or _slugify(payload.name)
    if not payload.name.strip() or not slug:
        raise ApiError.bad_request("Nombre y slug son obligatorios.")
    if payload.admin_password:
        raise ApiError.bad_request(
            "El acceso del dueño llega por correo: ya no se define una contraseña al crear el negocio.",
            "admin_password_no_soportado",
        )
    admin_email = payload.admin_email.strip().lower()
    try:
        _validate_slug(slug)
    except ValueError as exc:
        raise ApiError.bad_request(str(exc)[:180], "invalid_tenant") from exc
    if es_tester(principal.admin):
        from core.alta_negocio import motivo_para_rechazar_alta_de_tester

        motivo = await motivo_para_rechazar_alta_de_tester(slug, admin_email or None)
        if motivo:
            raise ApiError(409, "conflict", motivo)
    admin_id: uuid.UUID | None = None
    if admin_email:
        # Antes de crear nada: un correo que no se puede invitar no deja un negocio sin dueño.
        motivo = await motivo_para_no_dar_acceso(admin_email)
        if motivo:
            raise ApiError(409, "owner_email_rejected", motivo[:1].upper() + motivo[1:] + ".")
        # La forma se valida después: una cuenta que ya existe (p. ej. `admin@local`) no pasa por acá.
        if correo_valido(admin_email) is None:
            raise ApiError.bad_request("El correo del dueño no es válido.", "invalid_owner_email")
        admin_id, creada = await asegurar_cuenta_sin_contrasena(admin_email)
        if not creada and es_tester(principal.admin):
            # El pre-check de arriba es previo a crear la cuenta: se repite sobre el resultado.
            raise ApiError(
                409, "conflict", "Ese correo ya tiene una cuenta. Un tester solo da de alta negocios con una cuenta nueva."
            )

    try:
        # Sin `admin_email`: el dueño NO se vincula acá. Entra por la invitación (enlace o app)
        # y `canjear_invitacion` crea su membresía; vincularlo antes la haría imposible.
        tenant_id, steps = await onboard_tenant(
            name=payload.name.strip(),
            slug=slug,
            business_description=payload.business,
            language=payload.language,
            persona={"tone": payload.tone},
            escalation_triggers=payload.escalation_triggers,
            packs=payload.packs,
        )
    except ValueError as exc:
        raise ApiError.bad_request(str(exc)[:180], "invalid_tenant") from exc
    except IntegrityError as exc:
        # Otra petición creó el mismo slug a la vez.
        raise ApiError(409, "slug_en_uso", "Ya existe un negocio con ese slug.") from exc

    invitation: TenantOwnerInvitation | None = None
    if admin_id is not None and es_tester(principal.admin) and any(p.startswith("tenant ya existía") for p in steps):
        # El pre-check del slug es previo y no es atómico: si otro lo creó en medio, el tester no
        # emite una invitación `owner` sobre un negocio que no es suyo.
        raise ApiError(409, "conflict", "Ya existe un negocio con ese slug. Un tester solo da de alta negocios nuevos.")
    if admin_id is not None:
        try:
            acceso = await emitir_y_enviar_acceso(
                tenant_id=tenant_id,
                admin_id=admin_id,
                email=admin_email,
                negocio=payload.name.strip(),
                actor_email=principal.admin.email,
            )
        except AccesoRechazado as exc:
            raise ApiError(409, exc.codigo, f"El negocio se creó, pero {exc.mensaje}") from exc
        invitation = TenantOwnerInvitation(estado=acceso.estado, tipo=acceso.tipo, expires_at=acceso.expires_at)
        # `onboard_tenant` no vinculó a nadie (el dueño entra por la invitación): el paso suyo lo dice.
        steps = [s for s in steps if not s.startswith("sin admin")]
        steps.append(f"acceso del dueño: invitación {acceso.tipo}, correo {acceso.estado}")
    return TenantCreated(tenant_id=tenant_id, slug=slug, steps=steps, invitation=invitation)


@router.post(
    "/tenants/{tenant_id}/owner-invitation/resend",
    response_model=TenantOwnerInvitation,
    summary="Reenvía el acceso al dueño del negocio",
)
async def resend_owner_invitation(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> TenantOwnerInvitation:
    """Reenvía el correo de acceso a la cuenta de la invitación `owner` pendiente del negocio
    (ADR-0175). El destinatario no se recibe: es el de la invitación. Reusa el enlace mientras
    vigente y lo renueva si venció."""
    _require_master(principal)

    from core.alta_acceso import AccesoRechazado, reenviar_acceso

    try:
        acceso = await reenviar_acceso(tenant_id=tenant_id, actor_email=principal.admin.email)
    except AccesoRechazado as exc:
        status = 404 if exc.codigo == "tenant_no_encontrado" else 409
        raise ApiError(status, exc.codigo, exc.mensaje) from exc
    return TenantOwnerInvitation(estado=acceso.estado, tipo=acceso.tipo, expires_at=acceso.expires_at)


@router.post("/tenants/{tenant_id}/plan", status_code=204)
async def set_tenant_plan(
    tenant_id: uuid.UUID,
    payload: TenantPlanUpdate,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    """Cambia el plan de un negocio (soporte, master) y lo audita (ADR-0045)."""
    _require_master(principal)

    from datetime import timedelta

    from core.billing.plans import get_plan
    from core.billing.service import ensure_free_subscription
    from core.db.models import AuditLog, TenantSubscription
    from core.shared.tenant import tenant_scope

    if get_plan(payload.plan_id) is None:
        raise ApiError.bad_request("Plan desconocido.", "unknown_plan")

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tenant_id,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if sub is None:
                sub = await ensure_free_subscription(session, tenant_id)
            sub.plan_id = payload.plan_id
            sub.status = "active"
            if payload.extend_days:
                from core.db.models import utcnow

                base = sub.period_end if sub.period_end and sub.period_end > utcnow() else utcnow()
                sub.period_end = base + timedelta(days=payload.extend_days)
                sub.trial_ends_at = None
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action="billing.set_plan",
                    input={"plan_id": payload.plan_id, "extend_days": payload.extend_days},
                )
            )
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/activate", status_code=204)
async def activate_tenant_endpoint(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    _require_master(principal)
    from core.tenants import activate_tenant

    await activate_tenant(str(tenant_id))
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/deactivate", status_code=204)
async def deactivate_tenant_endpoint(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    _require_master(principal)
    from core.tenants import deactivate_tenant

    await deactivate_tenant(str(tenant_id))
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/purge", status_code=204)
async def purge_tenant_endpoint(
    tenant_id: uuid.UUID,
    payload: PurgeRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    """Eliminación física IRREVERSIBLE (invariante 8).

    Exige el tenant suspendido (409 `tenant_not_suspended`) y la frase `ELIMINAR <slug>`.
    Solo el master, también en test (invariante 8: el tester no purga).
    """
    if not es_master(principal.admin):
        raise ApiError.forbidden("Solo el admin de plataforma puede eliminar un negocio.")
    from core.tenants import TenantNotSuspendedError, purge_tenant

    try:
        await purge_tenant(str(tenant_id), payload.confirm_phrase)
    except TenantNotSuspendedError as exc:
        raise ApiError(409, "tenant_not_suspended", str(exc)[:240]) from exc
    except ValueError as exc:
        raise ApiError.bad_request(str(exc)[:180], "purge_confirmation") from exc
    except Exception as exc:
        raise ApiError(502, "purge_failed", f"No se pudo eliminar: {str(exc)[:140]}") from exc
    return Response(status_code=204)


# ------------------------------------------------- App de Meta de la plataforma (HU-CONN-07)
class PlatformMetaApp(ReadinessRun):
    """Checklist de la app de Meta de la plataforma y la infraestructura (solo master)."""

    meta_app_id: str = ""


async def _platform_meta_app(principal: Principal) -> PlatformMetaApp:
    from core.admin.production_readiness_service import latest_platform_run
    from core.config import get_settings

    run = await latest_platform_run(principal.admin)
    return PlatformMetaApp(meta_app_id=get_settings().meta_app_id or "", **readiness_run_out(run))


@router.get("/meta-app", response_model=PlatformMetaApp)
async def get_platform_meta_app(
    principal: Annotated[Principal, Depends(get_principal)],
) -> PlatformMetaApp:
    """Último resultado guardado (con fecha) de los chequeos de la app de Meta de la
    plataforma e infraestructura: webhook, suscripciones de la app, App Secret, claves de
    cifrado, worker, RLS. No corre nada: para revisar, `POST /meta-app/run`."""
    _require_master(principal)
    return await _platform_meta_app(principal)


@router.post("/meta-app/run", response_model=PlatformMetaApp, status_code=202)
async def run_platform_meta_app(
    principal: Annotated[Principal, Depends(get_principal)],
) -> PlatformMetaApp:
    """Encola la revisión de la plataforma (job de Celery, sin pytest) y responde al instante
    con la corrida `queued` (o la que ya estaba en curso)."""
    from core.admin import production_readiness_service as readiness

    _require_master(principal)
    try:
        await readiness.request_platform_run(principal.admin)
    except readiness.EnqueueFailed as exc:
        raise ApiError(503, "enqueue_failed", str(exc)) from exc
    return await _platform_meta_app(principal)


# ------------------------------------------------- Agent OS por negocio (HU-AOS-15)
class AgentOSBanderas(BaseModel):
    """Estado de las banderas del Agent OS de un negocio (ADR-0112, ADR-0138)."""

    runtime: Literal["legacy", "v3"]
    procedures: bool
    routing: Literal["off", "shadow", "active"]


class AgentOSCambio(BaseModel):
    actor: str
    fecha: datetime


class AgentOSEstado(AgentOSBanderas):
    # `AOS_CONNECTION_ROUTING` del entorno: v3 la exige (se cambia con un despliegue).
    aos_connection_routing: bool
    ultimo_cambio: AgentOSCambio | None = None


class AgentOSObjetivo(AgentOSBanderas):
    # Construir y promover una release nueva si la activa está desactualizada: cambia la release
    # activa del negocio, por eso es opt-in explícito del master.
    construir: bool = False


class AgentOSResultado(BaseModel):
    # sin_cambios | listo | por_preparar | bloqueado | aplicado
    accion: str
    antes: AgentOSBanderas
    despues: AgentOSBanderas
    aos_connection_routing: bool
    pasos: list[str]
    bloqueos: list[str]
    avisos: list[str]


def _require_master_agent_os(principal: Principal) -> None:
    # Solo el master, también en test (decisión del dueño, 2026-10-20; excepción a ADR-0134 §2 como
    # la purga): ni el tester ni un colaborador.
    if not es_master(principal.admin):
        raise ApiError.forbidden("Solo el admin master cambia el Agent OS de un negocio.")


def _banderas_out(b: Any) -> AgentOSBanderas:
    return AgentOSBanderas(runtime=b.runtime, procedures=b.procedimientos, routing=b.ruteo)


def _resultado_out(r: Any) -> AgentOSResultado:
    return AgentOSResultado(
        accion=r.accion,
        antes=_banderas_out(r.antes),
        despues=_banderas_out(r.despues),
        aos_connection_routing=r.aos_connection_routing,
        pasos=r.pasos,
        bloqueos=r.bloqueos,
        avisos=r.avisos,
    )


@router.get("/tenants/{tenant_id}/agent-os", response_model=AgentOSEstado)
async def get_agent_os(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AgentOSEstado:
    """Banderas del Agent OS del negocio, la global del entorno y el último cambio (quién y cuándo)."""
    _require_master_agent_os(principal)
    from core.admin import agent_os
    from core.config import get_settings

    banderas = await agent_os.estado(tenant_id)
    if banderas is None:
        raise ApiError.not_found("Negocio no encontrado.")
    cambio = await agent_os.ultimo_cambio(tenant_id)
    return AgentOSEstado(
        **_banderas_out(banderas).model_dump(),
        aos_connection_routing=bool(get_settings().aos_connection_routing),
        ultimo_cambio=AgentOSCambio(actor=cambio["actor"], fecha=cambio["fecha"]) if cambio else None,
    )


async def _agent_os(tenant_id: uuid.UUID, payload: AgentOSObjetivo, principal: Principal, *, aplicar: bool) -> AgentOSResultado:
    _require_master_agent_os(principal)
    from core.admin import agent_os
    from core.kernel.banderas import BanderasAOS

    objetivo = BanderasAOS(runtime=payload.runtime, procedimientos=payload.procedures, ruteo=payload.routing)
    try:
        if aplicar:
            resultado = await agent_os.aplicar(
                tenant_id,
                objetivo,
                construir=payload.construir,
                admin_id=principal.admin.id,
                actor=f"admin:{principal.admin.email}",
            )
        else:
            resultado = await agent_os.simular(tenant_id, objetivo, construir=payload.construir, admin_id=principal.admin.id)
    except agent_os.ObjetivoInvalido as exc:
        raise ApiError(422, "agent_os_orden", str(exc)) from None
    except agent_os.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    except agent_os.CambioEnCurso as exc:
        raise ApiError(409, "agent_os_en_curso", str(exc)) from None
    except agent_os.BanderasRechazadas as exc:
        if exc.codigo == "permiso":
            raise ApiError.forbidden(str(exc)) from None
        raise ApiError(409, f"agent_os_{exc.codigo}", str(exc)) from None
    salida = _resultado_out(resultado)
    if aplicar and resultado.accion == "bloqueado":
        # Fail-closed: nada se escribió en las banderas. El panel muestra los bloqueos.
        raise ApiError(
            409, "agent_os_bloqueado", "El negocio no está listo para ese cambio.", details=salida.model_dump()
        )
    return salida


@router.post("/tenants/{tenant_id}/agent-os/simular", response_model=AgentOSResultado)
async def simular_agent_os(
    tenant_id: uuid.UUID,
    payload: AgentOSObjetivo,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AgentOSResultado:
    """Dry-run del cambio: bloqueos, avisos y los pasos que aplicaría. No escribe nada (ni
    backfills, ni releases, ni banderas). 422 `agent_os_orden` si el objetivo viola el orden."""
    return await _agent_os(tenant_id, payload, principal, aplicar=False)


@router.put("/tenants/{tenant_id}/agent-os", response_model=AgentOSResultado)
async def put_agent_os(
    tenant_id: uuid.UUID,
    payload: AgentOSObjetivo,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AgentOSResultado:
    """Lleva el negocio al estado pedido (HU-AOS-15, orden de ADR-0112): al pasar a v3 corre los
    backfills del negocio y `migrar-a-v3` (con `construir` si se pide); al volver a legacy apaga
    todo. Escribe con `aos_fijar_banderas` (auditada). 422 `agent_os_orden` (combinación fuera de
    orden), 409 `agent_os_bloqueado` (precondiciones; `details` trae bloqueos y avisos) o
    `agent_os_en_curso` (otro cambio en curso)."""
    return await _agent_os(tenant_id, payload, principal, aplicar=True)


# ---------------------------------------------------------------- gate de pass^k (HU-AOS-21 parte 4)
class GateBandera(BaseModel):
    cerrado: bool = Field(description="Con el gate cerrado, una versión sin veredicto no se activa (409 `gate_pendiente`).")
    tope_usd_mensual: float | None = Field(
        default=None, description="Tope mensual de las mediciones (USD, lo paga la plataforma); null = el del entorno."
    )


class GateBanderaIn(GateBandera):
    model_config = ConfigDict(extra="forbid")


@router.get("/tenants/{tenant_id}/gate", response_model=GateBandera)
async def get_gate(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> GateBandera:
    """Bandera del gate de pass^k del negocio y su tope mensual. Solo el master."""
    _require_master_agent_os(principal)
    from core.releases import gate
    from core.shared.tenant import tenant_scope

    try:
        async with tenant_scope(tenant_id):
            bandera = await gate.leer(tenant_id)
    except gate.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return GateBandera(cerrado=bandera.cerrado, tope_usd_mensual=bandera.tope_usd_mensual)


@router.put("/tenants/{tenant_id}/gate", response_model=GateBandera)
async def put_gate(
    tenant_id: uuid.UUID,
    payload: GateBanderaIn,
    principal: Annotated[Principal, Depends(get_principal)],
) -> GateBandera:
    """Enciende o apaga el gate del negocio (auditado como `release_gate_bandera`). 409
    `sin_base_de_eval` si se intenta cerrar sin la base de evaluación configurada en el entorno."""
    _require_master_agent_os(principal)
    from core.releases import gate
    from core.shared.tenant import tenant_scope

    try:
        async with tenant_scope(tenant_id):
            bandera = await gate.fijar(
                tenant_id,
                cerrado=payload.cerrado,
                tope_usd_mensual=payload.tope_usd_mensual,
                actor=f"admin:{principal.admin.email}",
            )
    except gate.SinBaseDeEval as exc:
        raise ApiError(409, "sin_base_de_eval", str(exc)) from None
    except gate.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    except ValueError as exc:
        raise ApiError(422, "tope_invalido", str(exc)) from None
    return GateBandera(cerrado=bandera.cerrado, tope_usd_mensual=bandera.tope_usd_mensual)


# ---------------------------------------------------------------- colaboración con pila (HU-AOS-20)
ModoPila = Literal["off", "shadow", "active"]
ModoVerificacion = Literal["shadow", "active"]


class ColaboracionBanderas(BaseModel):
    pila: ModoPila = Field(
        description="Pila de tareas: «off», «shadow» (calcula y anota lo que abriría) o «active» (la abre)."
    )
    verificar_horarios: ModoVerificacion = Field(
        description="Horarios sin respaldo: «shadow» solo cuenta; «active» rechaza y reintenta."
    )
    verificar_preguntas: ModoVerificacion = Field(
        description="Más preguntas que `composition.maxQuestions`: «shadow» solo cuenta; «active» rechaza."
    )
    disponibles: dict[str, list[str]] = Field(
        default_factory=dict, description="Los valores que este despliegue ya aplica (la HU se entrega por partes)."
    )


class ColaboracionBanderasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pila: ModoPila | None = None
    verificar_horarios: ModoVerificacion | None = None
    verificar_preguntas: ModoVerificacion | None = None


def _colaboracion_out(bandera: Any) -> ColaboracionBanderas:
    from core.kernel.banderas_pila import DISPONIBLES

    return ColaboracionBanderas(
        pila=bandera.pila,
        verificar_horarios=bandera.verificar_horarios,
        verificar_preguntas=bandera.verificar_preguntas,
        disponibles={k: list(v) for k, v in DISPONIBLES.items()},
    )


@router.get("/tenants/{tenant_id}/colaboracion", response_model=ColaboracionBanderas)
async def get_colaboracion(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> ColaboracionBanderas:
    """Banderas de la colaboración con pila de tareas del negocio. Solo el master."""
    _require_master_agent_os(principal)
    from core.kernel import banderas_pila as colaboracion
    from core.shared.tenant import tenant_scope

    try:
        async with tenant_scope(tenant_id):
            bandera = await colaboracion.leer(tenant_id)
    except colaboracion.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return _colaboracion_out(bandera)


@router.put("/tenants/{tenant_id}/colaboracion", response_model=ColaboracionBanderas)
async def put_colaboracion(
    tenant_id: uuid.UUID,
    payload: ColaboracionBanderasIn,
    principal: Annotated[Principal, Depends(get_principal)],
) -> ColaboracionBanderas:
    """Cambia las banderas que vengan (auditado como `agent_os_colaboracion`). 409 `no_disponible`
    con un valor que este despliegue todavía no aplica."""
    _require_master_agent_os(principal)
    from core.kernel import banderas_pila as colaboracion
    from core.shared.tenant import tenant_scope

    cambios = payload.model_dump(exclude_none=True)
    try:
        async with tenant_scope(tenant_id):
            bandera = (
                await colaboracion.fijar(tenant_id, cambios, actor=f"admin:{principal.admin.email}")
                if cambios
                else await colaboracion.leer(tenant_id)
            )
    except colaboracion.NoDisponible as exc:
        raise ApiError(409, "no_disponible", str(exc)) from None
    except colaboracion.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return _colaboracion_out(bandera)



# ---------------------------------------------------------------- opciones directas (SPEC-40, ADR-0172 §5)
class OpcionesDirectasBandera(BaseModel):
    habilitada: bool = Field(
        description="Si las plantillas con opciones que ejecutan una tool están encendidas para el negocio. "
        "Apagada: toda la API `/plantillas-opciones` responde 404 `opciones_directas_no_habilitadas`."
    )


class OpcionesDirectasBanderaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Estricto: solo `true`/`false` (no «1», «yes» ni «true» como texto) para una bandera que enciende envíos reales.
    habilitada: StrictBool


_ERRORES_OPCIONES_DIRECTAS: dict[int | str, dict[str, Any]] = {
    403: {"model": OpcionesErrorOut, "description": "No es el admin master (tampoco el tester ni un colaborador)."},
    404: {"model": OpcionesErrorOut, "description": "El negocio no existe o está borrado."},
}


@router.get(
    "/tenants/{tenant_id}/opciones-directas",
    response_model=OpcionesDirectasBandera,
    responses=_ERRORES_OPCIONES_DIRECTAS,
)
async def get_opciones_directas(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> OpcionesDirectasBandera:
    """Bandera de las opciones directas del negocio (apagada por defecto). Solo el master."""
    _require_master_agent_os(principal)
    from core.kernel.opciones import bandera
    from core.shared.tenant import tenant_scope

    try:
        async with tenant_scope(tenant_id):
            encendida = await bandera.leer(tenant_id)
    except bandera.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return OpcionesDirectasBandera(habilitada=encendida)


@router.put(
    "/tenants/{tenant_id}/opciones-directas",
    response_model=OpcionesDirectasBandera,
    responses=_ERRORES_OPCIONES_DIRECTAS,
)
async def put_opciones_directas(
    tenant_id: uuid.UUID,
    payload: OpcionesDirectasBanderaIn,
    principal: Annotated[Principal, Depends(get_principal)],
) -> OpcionesDirectasBandera:
    """Enciende o apaga las opciones directas del negocio (auditado como `opciones_directas_bandera`). Idempotente:
    repetir el mismo valor deja el mismo estado. Solo el master; es por negocio y no toca ningún otro."""
    _require_master_agent_os(principal)
    from core.kernel.opciones import bandera
    from core.shared.tenant import tenant_scope

    try:
        async with tenant_scope(tenant_id):
            encendida = await bandera.fijar(tenant_id, payload.habilitada, actor=f"admin:{principal.admin.email}")
    except bandera.NegocioNoEncontrado:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return OpcionesDirectasBandera(habilitada=encendida)
