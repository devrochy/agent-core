"""Lectura del estado real de un negocio para sus primeros pasos (HU-SH-06, ADR-0124).

Todo corre bajo el `tenant_scope` del negocio y cada consulta filtra además por `tenant_id`
(defensa en profundidad, invariante 1): el checklist de un negocio nunca ve datos de otro. Las
tablas de sistema sin RLS (`tenants`, `tenant_user`, `tenant_invitations`) se filtran solo por
`tenant_id`. Del contenido de las habilidades se encarga cada pack con
`BaseModule.contenido_cargado` (el core no lee sus tablas).

No escribe nada propio (los hitos los registra `hitos.py`). La única escritura posible es la de
`load_active_spec`, que crea el AgentSpec por defecto de un negocio que no lo tenga, como en
cualquier otra pantalla del panel.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select

from core.db.models import (
    AppSettings,
    ChannelConnection,
    Conversation,
    Message,
    MetaConnection,
    Tenant,
    TenantInvitation,
    TenantUser,
    UsageLedger,
    WaWebSession,
)
from core.db.session import get_session
from core.primeros_pasos import reglas
from core.primeros_pasos.contrato import ContenidoCargado
from core.primeros_pasos.reglas import Paso
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

Origen = Literal["aprovisionamiento", "manual"]


@dataclass(frozen=True)
class Evaluacion:
    """Estado real de los primeros pasos de un negocio."""

    tenant_id: uuid.UUID
    alta_en: datetime
    origen: Origen
    pasos: list[Paso]
    # Bordes del embudo que no son pasos del checklist (invitación aceptada, primer mensaje):
    # paso → cuándo ocurrió. Solo los que se pidieron y ya ocurrieron.
    hitos_del_embudo: dict[str, datetime] = field(default_factory=dict)


async def calcular(tenant_id: uuid.UUID, *, omitir_hitos: Collection[str] = ()) -> Evaluacion | None:
    """Pasos del negocio `tenant_id` calculados de su estado real, o None si no existe.

    `omitir_hitos`: bordes del embudo ya registrados, para no volver a buscarlos (el primer
    mensaje de un negocio con mucho historial es la consulta más cara)."""
    async with get_session() as session:
        negocio = await session.get(Tenant, tenant_id)
    if negocio is None or negocio.deleted_at is not None:
        return None
    settings: Mapping[str, Any] = negocio.settings or {}
    alta = settings.get("alta") if isinstance(settings.get("alta"), Mapping) else {}
    origen: Origen = "aprovisionamiento" if (alta or {}).get("origen") == "provisioning" else "manual"

    async with tenant_scope(tenant_id):
        habilitadas = await _habilidades_habilitadas(tenant_id)
        async with get_session() as session:
            datos = await _paso_datos_negocio(session, tenant_id)
            whatsapp = await _paso_whatsapp(session, tenant_id)
            probar = reglas.paso_probar_asistente(await _primer_turno(session, tenant_id))
            equipo = await _paso_equipo(session, tenant_id)
            hitos: dict[str, datetime] = {}
            if reglas.INVITACION_ACEPTADA not in omitir_hitos:
                aceptada = await _dueno_desde(session, tenant_id)
                if aceptada is not None:
                    hitos[reglas.INVITACION_ACEPTADA] = aceptada
            if reglas.PRIMER_MENSAJE not in omitir_hitos:
                mensaje = await _primer_mensaje(session, tenant_id)
                if mensaje is not None:
                    hitos[reglas.PRIMER_MENSAJE] = mensaje
        catalogo = reglas.paso_catalogo(await _contenidos(tenant_id, habilitadas))
        preset = await _paso_activar_preset(tenant_id, settings, alta or {})

    pasos = [datos, whatsapp, catalogo, probar, equipo]
    if preset is not None:
        pasos.append(preset)
    pasos.extend(_pasos_de_habilidades(settings, set(habilitadas)))
    return Evaluacion(
        tenant_id=tenant_id,
        alta_en=negocio.created_at,
        origen=origen,
        pasos=pasos,
        hitos_del_embudo=hitos,
    )


# ------------------------------------------------------------------ lecturas del core
async def _paso_datos_negocio(session, tenant_id: uuid.UUID) -> Paso:  # type: ignore[no-untyped-def]
    fila = (
        await session.execute(
            select(AppSettings.business_description, AppSettings.updated_at).where(
                AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
            )
        )
    ).first()
    return reglas.paso_datos_negocio(fila[0] if fila else None, fila[1] if fila else None)


async def _paso_whatsapp(session, tenant_id: uuid.UUID) -> Paso:  # type: ignore[no-untyped-def]
    meta = (
        await session.execute(
            select(MetaConnection.status, MetaConnection.health, MetaConnection.connected_at, MetaConnection.created_at)
            .where(
                MetaConnection.tenant_id == tenant_id,
                MetaConnection.channel == "whatsapp",
                MetaConnection.deleted_at.is_(None),
            )
        )
    ).all()
    sesiones = (
        await session.execute(
            select(WaWebSession.status, WaWebSession.last_connected_at).where(
                WaWebSession.tenant_id == tenant_id, WaWebSession.deleted_at.is_(None)
            )
        )
    ).all()
    manual_en = (
        await session.execute(
            select(func.min(ChannelConnection.created_at)).where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.channel == "whatsapp",
                ChannelConnection.provider == "meta",
                ChannelConnection.active.is_(True),
                ChannelConnection.deleted_at.is_(None),
                # Solo las manuales: la canónica de un número de Meta sigue a su cuenta (ADR-0115).
                ChannelConnection.meta_connection_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    return reglas.paso_whatsapp(
        [
            reglas.ConexionMeta(status=status, health=health, conectada_en=conectada_en or creada_en)
            for status, health, conectada_en, creada_en in meta
        ],
        waweb=[reglas.SesionWaweb(status=status, conectada_en=en) for status, en in sesiones],
        manual_en=manual_en,
    )


async def _primer_turno(session, tenant_id: uuid.UUID) -> datetime | None:  # type: ignore[no-untyped-def]
    """Primer turno que respondió el asistente (playground o cliente real): `usage_ledger`
    tiene una fila por turno (ADR-0008), con índice por `(tenant_id, created_at)`."""
    return (
        await session.execute(
            select(UsageLedger.created_at)
            # Subir un documento (fila `embedding`, ADR-0145 §3) no es responder un turno.
            .where(UsageLedger.tenant_id == tenant_id, UsageLedger.kind.not_in(("embedding", "generacion")))
            .order_by(UsageLedger.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _paso_equipo(session, tenant_id: uuid.UUID) -> Paso:  # type: ignore[no-untyped-def]
    invitado_en = (
        await session.execute(
            select(func.min(TenantInvitation.created_at)).where(
                TenantInvitation.tenant_id == tenant_id,
                TenantInvitation.role != "owner",
                TenantInvitation.estado != "revocada",
            )
        )
    ).scalar_one_or_none()
    colaborador_en = (
        await session.execute(
            select(func.min(TenantUser.created_at)).where(
                TenantUser.tenant_id == tenant_id, TenantUser.role != "owner"
            )
        )
    ).scalar_one_or_none()
    return reglas.paso_equipo(invitado_en, colaborador_en)


async def _dueno_desde(session, tenant_id: uuid.UUID) -> datetime | None:  # type: ignore[no-untyped-def]
    """Cuándo el dueño quedó dentro de su negocio (aceptó la invitación o lo vinculó el alta)."""
    return (
        await session.execute(
            select(func.min(TenantUser.created_at)).where(
                TenantUser.tenant_id == tenant_id, TenantUser.role == "owner"
            )
        )
    ).scalar_one_or_none()


async def _primer_mensaje(session, tenant_id: uuid.UUID) -> datetime | None:  # type: ignore[no-untyped-def]
    """Primer mensaje de un cliente real (no del playground). Busca la primera conversación por
    el índice `(tenant_id, started_at)` y después su primer mensaje entrante por
    `(tenant_id, conversation_id, created_at)`: no recorre todo el historial."""
    entrante = (
        select(Message.id)
        .where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == Conversation.id,
            Message.direction == "inbound",
            Message.deleted_at.is_(None),
        )
        .exists()
    )
    conversacion = (
        await session.execute(
            select(Conversation.id)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.channel != TEST_CHANNEL,
                Conversation.deleted_at.is_(None),
                entrante,
            )
            .order_by(Conversation.started_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversacion is None:
        return None
    return (
        await session.execute(
            select(func.min(Message.created_at)).where(
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversacion,
                Message.direction == "inbound",
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


# ------------------------------------------------------------------ habilidades
async def _habilidades_habilitadas(tenant_id: uuid.UUID) -> list[str]:
    """Packs de negocio habilitados en el AgentSpec, en su orden (sin el core)."""
    from core.agents.spec import CORE_PACK_ID, load_active_spec

    spec = await load_active_spec(tenant_id)
    vistas: list[str] = []
    for pack in spec.packs:
        if pack != CORE_PACK_ID and pack not in vistas:
            vistas.append(pack)
    return vistas


async def _contenidos(tenant_id: uuid.UUID, habilitadas: list[str]) -> list[tuple[str, ContenidoCargado]]:
    """Lo que cargó el negocio en cada habilidad habilitada. Un pack que falla no tumba el
    checklist: queda en el log y se sigue con los demás."""
    from core.plugin_manager import plugin_manager

    contenidos: list[tuple[str, ContenidoCargado]] = []
    for pack in habilitadas:
        modulo = plugin_manager.get(pack)
        if modulo is None:
            continue
        try:
            contenido = await modulo.contenido_cargado(tenant_id)
        except Exception:
            log.warning("primeros_pasos_contenido_fallido", tenant_id=str(tenant_id), pack=pack, exc_info=True)
            continue
        if isinstance(contenido, ContenidoCargado):
            contenidos.append((pack, contenido))
    return contenidos


def _pasos_de_habilidades(settings: Mapping[str, Any], habilitadas: set[str]) -> list[Paso]:
    """Las habilidades que el preset dejó pendientes de configurar (ADR-0110 § 3)."""
    from core.plugin_manager import plugin_manager

    preset = settings.get("preset")
    pendientes = preset.get("pendientes") if isinstance(preset, Mapping) else None
    if not isinstance(pendientes, list) or not pendientes:
        return []
    habilidades: dict[str, reglas.Habilidad] = {}
    for pendiente in pendientes:
        pack = pendiente.get("pack") if isinstance(pendiente, Mapping) else None
        if not isinstance(pack, str):
            continue
        modulo = plugin_manager.get(pack)
        if modulo is not None:
            habilidades[pack] = reglas.Habilidad(
                nombre=modulo.display_name or modulo.name or pack,
                guia=modulo.setup_guide or modulo.description or "",
            )
    return reglas.pasos_de_habilidades(pendientes, habilitadas=habilitadas, habilidades=habilidades)


# ------------------------------------------------------------------ tipo de negocio pendiente
def _requisito_del_preset(preset_id: str, plan_actual: str) -> reglas.RequisitoDePreset | None:
    """Nombre del preset y el plan que necesita (el mayor entre su `plan_minimo` y el
    `min_plan` de sus packs, igual que `apply_preset`), y si el plan actual alcanza."""
    from core.billing.plans import get_plan
    from core.plugin_manager import plugin_manager
    from core.presets import PresetError, load_preset

    try:
        manifest = load_preset(preset_id)
    except PresetError:
        return None
    pedidos = [manifest.plan_minimo]
    for pack in manifest.packs:
        modulo = plugin_manager.get(pack)
        if modulo is not None:
            pedidos.append(modulo.min_plan)
    planes = [plan for plan in (get_plan(p) for p in pedidos) if plan is not None]
    if not planes:
        return None
    requerido = max(planes, key=lambda plan: plan.sort_order)
    actual = get_plan(plan_actual)
    alcanza = actual is not None and actual.sort_order >= requerido.sort_order
    return reglas.RequisitoDePreset(nombre=manifest.nombre, plan_minimo=requerido.name, alcanza=alcanza)


def _fecha(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None


async def _paso_activar_preset(
    tenant_id: uuid.UUID, settings: Mapping[str, Any], alta: Mapping[str, Any]
) -> Paso | None:
    alta_preset = alta.get("preset")
    if not isinstance(alta_preset, Mapping) or alta_preset.get("estado") != "pendiente":
        return None
    preset_id = alta_preset.get("id")
    if not isinstance(preset_id, str) or not preset_id:
        return None
    aplicado = settings.get("preset") if isinstance(settings.get("preset"), Mapping) else {}
    from core.billing.service import get_active_subscription

    suscripcion = await get_active_subscription(tenant_id)
    requisito = _requisito_del_preset(preset_id, suscripcion.plan_id if suscripcion else "free")
    return reglas.paso_activar_preset(
        alta_preset,
        preset_aplicado_id=(aplicado or {}).get("id"),
        preset_aplicado_en=_fecha((aplicado or {}).get("aplicado_en")),
        requisito=requisito,
    )
