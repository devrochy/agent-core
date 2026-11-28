"""El lead en la vista global de Clientes (ADR-0056, ADR-0193): provider del core, siempre activo.

Aporta el estado, el puntaje, el interés, el seguimiento, el motivo de pérdida, el valor y el origen del lead vivo del
cliente, sus filtros, sus sumas y su historia (cambios de etapa y seguimientos cerrados), y su edición desde la ficha
(ADR-0149). Vivía en `pack_commerce.providers`; commerce conserva lo suyo (clasificación, pedidos, cotizaciones).

`PROVIDER_ID` ya no es un pack: `providers_for` lo incluye siempre (`lead_registry.SIEMPRE`). El panel y la APK
instalados editan por `PATCH /admin/leads/{id}/pack/pack_commerce`: el router lo atiende como alias de este provider.

Importar este módulo registra el provider (lo hace `core.tools.core_skill.register_core_skills`).
"""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any

from sqlalchemy import Select, case, func, literal, or_, select

from core.admin.lead_registry import (
    Cursor,
    LeadCampoEditable,
    LeadColumn,
    LeadEdicion,
    LeadFilter,
    LeadRelacion,
    TimelineEvent,
    antes_del_cursor,
    lead_registry,
)
from core.db.models import Customer, Lead, LeadFollowup, LeadStageChange
from core.db.session import get_session
from core.leads import seguimiento as _seg
from core.leads import valor as _valor
from core.leads.etapas import (
    ETAPA_DE_PERDIDA,
    ETAPAS,
    MOTIVOS_DE_PERDIDA,
    SIN_MOTIVO,
    MotivoDePerdidaInvalido,
    anotar_motivo,
    cambiar_etapa,
    nota_de_perdida,
)
from core.shared.tenant import get_current_tenant_id

PROVIDER_ID = "leads"
# El id con que el panel y la APK instalados editan el lead (antes era el provider de commerce).
ALIAS_DE_EDICION = "pack_commerce"

ESTADOS_LEAD = (
    ("nuevo", "Nuevo"),
    ("calificado", "Calificado"),
    ("contactado", "Contactado"),
    ("convertido", "Convertido"),
    ("perdido", "Perdido"),
)
assert tuple(v for v, _ in ESTADOS_LEAD) == ETAPAS  # una sola lista de etapas (HU-CRM-18)
ORIGENES = (
    ("landing", "Página web"),
    ("chat_web", "Chat web"),
    ("whatsapp", "WhatsApp"),
    ("instagram", "Instagram"),
    ("telegram", "Telegram"),
    ("manual", "Manual"),
)
# Filtro «Motivo de pérdida» (HU-CRM-23): los cinco motivos y los perdidos de antes de la HU, sin motivo.
SIN_MOTIVO_CODIGO = "sin_motivo"
MOTIVOS_FILTRO = (*MOTIVOS_DE_PERDIDA, (SIN_MOTIVO_CODIGO, SIN_MOTIVO))


def lead_vivo(tid: uuid.UUID) -> Select[Any]:
    """El lead vivo de cada cliente (hay a lo sumo uno: `uq_leads_tenant_cliente`)."""
    return select(
        Lead.customer_id,
        Lead.status,
        Lead.score,
        Lead.interest,
        Lead.origen,
        # El negocio del cliente, si lo dijo (`actualizar_lead`, HU-AOS-70): vive en `custom`, sin columna propia.
        Lead.custom["negocio"].astext.label("negocio"),
        Lead.stage_changed_at,
        Lead.last_activity_at,
        Lead.next_action_at,
        Lead.next_action_note,
        Lead.loss_reason,
        Lead.loss_note,
        Lead.estimated_value,
    ).where(Lead.tenant_id == tid, Lead.customer_id.is_not(None), Lead.deleted_at.is_(None))


async def _limites_de_hoy(tid: uuid.UUID) -> _seg.Limites:
    """Los cortes de «hoy» y «próximos 7 días» en la zona del negocio (HU-CRM-21)."""
    async with get_session() as session:
        zona = await _seg.zona_del_negocio(session, tid)
    return _seg.limites(zona)


async def _filter_query(key: str, value: str) -> Select[Any] | None:
    tid = get_current_tenant_id()
    vivo = lead_vivo(tid).subquery()
    if key == "estado_lead":
        return select(vivo.c.customer_id).where(vivo.c.status == value)
    if key == "origen":
        return select(vivo.c.customer_id).where(vivo.c.origen == value)
    if key == "seguimiento":
        lim = await _limites_de_hoy(tid)
        return select(vivo.c.customer_id).where(_seg.condicion(value, vivo.c.next_action_at, lim))
    if key == "motivo_perdida":
        motivo = vivo.c.loss_reason.is_(None) if value == SIN_MOTIVO_CODIGO else vivo.c.loss_reason == value
        return select(vivo.c.customer_id).where(vivo.c.status == ETAPA_DE_PERDIDA, motivo)
    if key == "valor":
        return select(vivo.c.customer_id).where(_valor.condicion(value, vivo.c.estimated_value))
    if key == "lead_datos":
        con_datos = await _con_datos(None)
        if value == "con":
            return con_datos
        if value == "sin":
            return select(Customer.id).where(
                Customer.tenant_id == tid,
                Customer.deleted_at.is_(None),
                Customer.id.not_in(con_datos),
            )
    return None


async def _facet_query(key: str) -> Select[Any] | None:
    """`(customer_id, valor)` de cada cliente para el filtro `key`, a lo sumo una fila por cliente (HU-CRM-17)."""
    tid = get_current_tenant_id()
    vivo = lead_vivo(tid).subquery()
    if key == "estado_lead":
        return select(vivo.c.customer_id.label("customer_id"), vivo.c.status.label("valor"))
    if key == "origen":
        return select(vivo.c.customer_id.label("customer_id"), vivo.c.origen.label("valor"))
    if key == "seguimiento":
        lim = await _limites_de_hoy(tid)
        en = vivo.c.next_action_at
        return select(
            vivo.c.customer_id.label("customer_id"),
            case(
                *[(_seg.condicion(v, en, lim), literal(v)) for v, _ in _seg.OPCIONES],
                else_=literal("sin_seguimiento"),
            ).label("valor"),
        )
    if key == "motivo_perdida":
        return select(
            vivo.c.customer_id.label("customer_id"),
            case(
                (vivo.c.status != ETAPA_DE_PERDIDA, literal("no_perdido")),
                (vivo.c.loss_reason.is_(None), literal(SIN_MOTIVO_CODIGO)),
                else_=vivo.c.loss_reason,
            ).label("valor"),
        )
    if key == "valor":
        return select(vivo.c.customer_id.label("customer_id"), _valor.tramo_sql(vivo.c.estimated_value).label("valor"))
    if key == "lead_datos":
        con_datos = await _con_datos(None)
        return (
            select(
                Customer.id.label("customer_id"),
                case(
                    (Customer.id.in_(con_datos), literal("con")),
                    else_=literal("sin"),
                ).label("valor"),
            )
            .where(Customer.tenant_id == tid, Customer.deleted_at.is_(None))
        )
    return None


async def _medida_query(medida: str) -> Select[Any] | None:
    """`(customer_id, monto)` para la suma `valor` (HU-CRM-24): el valor estimado del lead vivo."""
    if medida == "valor":
        vivo = lead_vivo(get_current_tenant_id()).subquery()
        return select(vivo.c.customer_id.label("customer_id"), vivo.c.estimated_value.label("monto"))
    return None


async def _sort_query(orden: str) -> Select[Any] | None:
    """`(customer_id, valor)` para ordenar por seguimiento o por valor estimado (`valor`, `valor_desc`)."""
    if orden not in ("seguimiento", "valor", "valor_desc"):
        return None
    vivo = lead_vivo(get_current_tenant_id()).subquery()
    columna = vivo.c.next_action_at if orden == "seguimiento" else vivo.c.estimated_value
    return select(vivo.c.customer_id.label("customer_id"), columna.label("valor"))


def _desde_cuando(lead: Any) -> dict[str, Any]:
    """Desde cuándo está el lead en su etapa (HU-CRM-18). Sin `stage_changed_at`, la última actividad conocida."""
    if lead is None:
        return {"etapa_desde": None, "etapa_desde_aproximada": None}
    exacta = lead.stage_changed_at
    desde = exacta or lead.last_activity_at
    return {
        "etapa_desde": desde.astimezone(UTC).isoformat() if desde else None,
        "etapa_desde_aproximada": None if desde is None else exacta is None,
    }


def _del_seguimiento(lead: Any, lim: _seg.Limites) -> dict[str, Any]:
    """Próxima acción del lead (HU-CRM-21), con la opción en que cae calculada aquí con la zona del negocio."""
    en = lead.next_action_at if lead is not None else None
    return {
        "seguimiento_en": en.astimezone(UTC).isoformat() if en else None,
        "seguimiento_nota": (lead.next_action_note or None) if lead is not None else None,
        "seguimiento_estado": _seg.estado(en, lim),
    }


def _del_cierre(lead: Any) -> dict[str, Any]:
    """Motivo de pérdida (solo mientras está perdido) y valor estimado (HU-CRM-23)."""
    perdido = lead is not None and lead.status == ETAPA_DE_PERDIDA
    etiquetas = dict(MOTIVOS_DE_PERDIDA)
    codigo = lead.loss_reason if perdido else None
    return {
        "motivo_perdida": (etiquetas.get(codigo, codigo) if codigo else SIN_MOTIVO) if perdido else None,
        "motivo_perdida_codigo": codigo,
        "motivo_perdida_nota": (lead.loss_note or None) if perdido else None,
        "valor_estimado": float(lead.estimated_value) if lead is not None and lead.estimated_value is not None else None,
    }


async def _resolver(customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not customer_ids:
        return {}
    tid = get_current_tenant_id()
    vivo = lead_vivo(tid).subquery()
    async with get_session() as session:
        zona = await _seg.zona_del_negocio(session, tid)
        leads = {
            row.customer_id: row
            for row in (await session.execute(select(vivo).where(vivo.c.customer_id.in_(customer_ids)))).all()
        }
    estados = dict(ESTADOS_LEAD)
    origenes = dict(ORIGENES)
    lim = _seg.limites(zona)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for cid in customer_ids:
        lead = leads.get(cid)
        out[cid] = {
            "estado_lead": estados.get(lead.status, lead.status) if lead else None,
            "puntaje": lead.score if lead else None,
            **_desde_cuando(lead),
            **_del_seguimiento(lead, lim),
            **_del_cierre(lead),
            "interes": (lead.interest or None) if lead else None,
            "origen": origenes.get(lead.origen, lead.origen) if lead and lead.origen else None,
            "negocio": (lead.negocio or None) if lead else None,
        }
    return out


async def _leer_lead(customer_id: uuid.UUID) -> dict[str, Any]:
    """Estado (código), interés, seguimiento, motivo y valor del lead vivo, como los edita la ficha."""
    tid = get_current_tenant_id()
    vivo = lead_vivo(tid).subquery()
    async with get_session() as session:
        zona = await _seg.zona_del_negocio(session, tid)
        lead = (await session.execute(select(vivo).where(vivo.c.customer_id == customer_id))).first()
    if lead is None:
        return {}
    return {
        "estado_lead": lead.status,
        "interes": lead.interest or "",
        # Hora del negocio, sin zona, lista para el selector de fecha y hora.
        "seguimiento_en": _seg.texto_local(lead.next_action_at, zona),
        "seguimiento_nota": lead.next_action_note or "",
        # Solo vigentes mientras está perdido (HU-CRM-23); el valor es del lead en cualquier etapa.
        "motivo_perdida": (lead.loss_reason or "") if lead.status == ETAPA_DE_PERDIDA else "",
        "nota_perdida": (lead.loss_note or "") if lead.status == ETAPA_DE_PERDIDA else "",
        "valor_estimado": _valor.texto(lead.estimated_value),
    }


async def _programar_seguimiento(session: Any, tid: uuid.UUID, lead: Lead, valores: dict[str, str]) -> None:
    """Programa, mueve o quita el seguimiento (HU-CRM-21). La fecha sin zona es hora del negocio; vacía, quita el
    seguimiento y su nota. No mueve `last_activity_at` (ADR-0149)."""
    # El lead bloqueado: un PATCH y un «hecho» a la vez no cierran dos veces el mismo seguimiento.
    await session.flush()
    await session.refresh(lead, with_for_update=True)
    zona = await _seg.zona_del_negocio(session, tid)
    if "seguimiento_en" in valores:
        texto = valores["seguimiento_en"]
        en = _seg.parsear_fecha(texto, zona) if texto else None
        nota = valores.get("seguimiento_nota")  # sin fecha, la nota vieja se va con ella
    else:
        en, nota = lead.next_action_at, valores["seguimiento_nota"]
    _seg.programar(session, lead, fecha=en, nota=nota)


async def _escribir_lead(customer_id: uuid.UUID, valores: dict[str, str]) -> None:
    """Cambia lo que edita la ficha en el lead vivo del cliente; si no tiene, lo crea (origen `manual`) sin copiar
    datos de contacto. Solo un cambio de estado mueve `last_activity_at` (ADR-0149)."""
    from core.leads import servicio

    tid = get_current_tenant_id()
    async with get_session() as session:
        lead = await servicio.vivo_o_nuevo(session, tid, customer_id, origen="manual")
        if lead is None:
            return
        perdido_ahora = False
        if "estado_lead" in valores:
            # Una persona (el hook solo lo llama el panel/APK): puede reabrir un perdido (HU-CRM-18). Perder un lead
            # pide motivo (HU-CRM-23): sin él, un `ValueError` que el core devuelve como 400 y no cambia nada.
            try:
                perdido_ahora = await cambiar_etapa(
                    session,
                    lead,
                    valores["estado_lead"],
                    actor="persona",
                    loss_reason=valores.get("motivo_perdida"),
                    loss_note=valores.get("nota_perdida"),
                ) and lead.status == ETAPA_DE_PERDIDA
            except MotivoDePerdidaInvalido as exc:
                motivos = ", ".join(etiqueta for _, etiqueta in MOTIVOS_DE_PERDIDA)
                raise ValueError(f"Elige el motivo de la pérdida para marcar el lead como perdido ({motivos}).") from exc
        if not perdido_ahora and ("motivo_perdida" in valores or "nota_perdida" in valores):
            # El lead ya estaba perdido (p. ej. uno de antes de la HU, sin motivo): se anota o corrige.
            if "motivo_perdida" in valores:
                nota = valores["nota_perdida"] if "nota_perdida" in valores else lead.loss_note
                anotar_motivo(lead, valores["motivo_perdida"], nota)
            elif lead.status == ETAPA_DE_PERDIDA:
                lead.loss_note = nota_de_perdida(valores["nota_perdida"])
            else:
                raise ValueError(f"La nota de la pérdida solo se anota en un lead «{ETAPA_DE_PERDIDA}».")
        if "valor_estimado" in valores:
            lead.estimated_value = _valor.parsear(valores["valor_estimado"])
        if "interes" in valores:
            lead.interest = valores["interes"]
        if "seguimiento_en" in valores or "seguimiento_nota" in valores:
            await _programar_seguimiento(session, tid, lead, valores)


def _con_texto(columna: Any) -> Any:
    """Columna de texto con algo más que espacios."""
    return func.length(func.trim(func.coalesce(columna, ""))) > 0


async def _con_datos(ids: list[uuid.UUID] | None) -> Select[Any]:
    """Clientes con un lead con contenido: nombre, teléfono, correo, interés o negocio. El lead que se asegura al
    empezar la conversación nace vacío y no es un dato del cliente hasta que el cliente da algo."""
    tid = get_current_tenant_id()
    q = select(Lead.customer_id).where(
        Lead.tenant_id == tid,
        Lead.customer_id.is_not(None),
        Lead.deleted_at.is_(None),
        or_(
            _con_texto(Lead.name),
            _con_texto(Lead.phone),
            _con_texto(Lead.email),
            _con_texto(Lead.interest),
            _con_texto(Lead.custom["negocio"].astext),
        ),
    )
    if ids is not None:
        q = q.where(Lead.customer_id.in_(ids))
    return q


def _detalle_de_etapa(cambio: Any) -> str:
    """Quién hizo el cambio y, si fue a «perdido», con qué motivo (HU-CRM-23)."""
    quien = _QUIEN_CAMBIO.get(cambio.actor, cambio.actor)
    if cambio.to_stage != ETAPA_DE_PERDIDA or not cambio.loss_reason:
        return quien
    motivo = dict(MOTIVOS_DE_PERDIDA).get(cambio.loss_reason, cambio.loss_reason)
    nota = f" — {cambio.loss_note}" if cambio.loss_note else ""
    return f"Motivo: {motivo}{nota} · {quien}"


# Fuentes de la línea de tiempo: conservan el nombre de commerce porque el cursor de la API las lleva (ADR-0193).
FUENTE_ETAPA = "pack_commerce.lead_etapa"
FUENTE_SEGUIMIENTO = "pack_commerce.lead_seguimiento"
_SEGUIMIENTO_CIERRE = {
    "hecho": "Seguimiento hecho",
    "reprogramado": "Seguimiento reprogramado",
    "cancelado": "Seguimiento cancelado",
}
_QUIEN_CAMBIO = {"agente": "Lo hizo el agente", "persona": "Lo hizo una persona", "sistema": "Lo hizo el sistema"}


async def _timeline(customer_id: uuid.UUID, cursor: Cursor | None, limit: int) -> list[TimelineEvent]:
    """Cambios de etapa (HU-CRM-18) y seguimientos cerrados (HU-CRM-21) del cliente, de todos sus leads."""
    tid = get_current_tenant_id()
    eventos: list[TimelineEvent] = []
    async with get_session() as session:
        etapas = (
            await session.execute(
                select(LeadStageChange)
                .where(
                    LeadStageChange.tenant_id == tid,
                    LeadStageChange.customer_id == customer_id,
                    LeadStageChange.deleted_at.is_(None),
                    antes_del_cursor(LeadStageChange.changed_at, LeadStageChange.id, FUENTE_ETAPA, cursor),
                )
                .order_by(LeadStageChange.changed_at.desc(), LeadStageChange.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        etiquetas = dict(ESTADOS_LEAD)
        for c in etapas:
            eventos.append(
                TimelineEvent(
                    at=c.changed_at, source=FUENTE_ETAPA, id=str(c.id), kind="etapa",
                    title=f"Etapa: {etiquetas.get(c.from_stage, c.from_stage)} → {etiquetas.get(c.to_stage, c.to_stage)}",
                    detail=_detalle_de_etapa(c),
                )
            )
        zona = await _seg.zona_del_negocio(session, tid)
        cierres = (
            await session.execute(
                select(LeadFollowup)
                .where(
                    LeadFollowup.tenant_id == tid,
                    LeadFollowup.customer_id == customer_id,
                    LeadFollowup.deleted_at.is_(None),
                    antes_del_cursor(LeadFollowup.closed_at, LeadFollowup.id, FUENTE_SEGUIMIENTO, cursor),
                )
                .order_by(LeadFollowup.closed_at.desc(), LeadFollowup.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for f in cierres:
            titulo = _SEGUIMIENTO_CIERRE.get(f.outcome, f.outcome)
            para = f.due_at.astimezone(zona).strftime("%d/%m/%Y %H:%M")
            eventos.append(
                TimelineEvent(
                    at=f.closed_at, source=FUENTE_SEGUIMIENTO, id=str(f.id), kind="seguimiento",
                    title=f"{titulo}: {f.note}" if f.note else titulo,
                    detail=f"Estaba para el {para} · {_QUIEN_CAMBIO.get(f.actor, f.actor)}",
                )
            )
    return eventos


lead_registry.register(
    PROVIDER_ID,
    columns=[
        LeadColumn(key="estado_lead", label="Estado del lead"),
        LeadColumn(key="puntaje", label="Puntaje"),
        LeadColumn(key="seguimiento_nota", label="Próxima acción"),
        LeadColumn(key="motivo_perdida", label="Motivo de pérdida"),
        LeadColumn(key="valor_estimado", label="Valor estimado"),
        LeadColumn(key="interes", label="Interés"),
        LeadColumn(key="origen", label="Origen"),
        LeadColumn(key="negocio", label="Negocio"),
    ],
    resolver=_resolver,
    filters=[
        LeadFilter(key="estado_lead", label="Estado del lead", options=ESTADOS_LEAD),
        LeadFilter(key="seguimiento", label="Seguimiento", options=_seg.OPCIONES),
        LeadFilter(key="motivo_perdida", label="Motivo de pérdida", options=MOTIVOS_FILTRO),
        LeadFilter(key="valor", label="Valor estimado", options=_valor.OPCIONES),
        LeadFilter(key="origen", label="Origen", options=ORIGENES),
        LeadFilter(key="lead_datos", label="Datos del lead", options=(("con", "Con datos"), ("sin", "Sin datos"))),
    ],
    filter_query=_filter_query,
    facet_query=_facet_query,
    sort_query=_sort_query,
    medida_query=_medida_query,
    timeline=_timeline,
    edicion=LeadEdicion(
        campos=(
            LeadCampoEditable(key="estado_lead", label="Estado del lead", tipo="opciones", options=ESTADOS_LEAD),
            LeadCampoEditable(key="motivo_perdida", label="Motivo de pérdida", tipo="opciones", options=MOTIVOS_DE_PERDIDA),
            LeadCampoEditable(key="nota_perdida", label="Nota de la pérdida", tipo="texto", max_length=300),
            LeadCampoEditable(key="valor_estimado", label="Valor estimado", tipo="numero", max_length=14),
            LeadCampoEditable(key="interes", label="Interés", tipo="texto", max_length=500),
            LeadCampoEditable(key="seguimiento_nota", label="Próxima acción", tipo="texto", max_length=_seg.NOTA_MAX),
            LeadCampoEditable(key="seguimiento_en", label="Fecha de seguimiento", tipo="fecha", max_length=32),
        ),
        leer=_leer_lead,
        escribir=_escribir_lead,
    ),
    agent_key=PROVIDER_ID,
    agent_name="Lead",
    icon="user-plus",
    relacion=LeadRelacion(con_datos=_con_datos),
)
