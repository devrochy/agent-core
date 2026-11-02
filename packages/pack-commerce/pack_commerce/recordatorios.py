"""Recordatorios de seguimiento y resumen diario de ventas (HU-CRM-22).

Todo es determinista y sin LLM: una persona programó un seguimiento (HU-CRM-21) y el sistema
le avisa a ella cuando llega la hora, y cada mañana le resume su día. No escribe al cliente ni
gasta créditos: el aviso va al centro de notificaciones (ADR-0167) y de ahí al push.

Idempotencia sin tabla nueva: la clave de deduplicación del aviso lo es todo.
- Recordatorio: `seguimiento:<lead>:<instante de la fecha>`. Correr el job diez veces, o reiniciarlo,
  no repite el aviso; si la fecha cambia, la clave cambia y vuelve a avisar por la fecha nueva.
- Resumen: `resumen_ventas:<día local>`: un aviso por negocio y por día.

El agente no crea seguimientos (la HU-CRM-21 no le dio tools), así que un lead que nace del agente
no genera recordatorios por sí solo: sin `next_action_at` no hay nada que vencer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from core.db.models import Customer, Notification
from core.notifications.service import notify
from core.notifications.spec import NotificationSpec
from core.shared.logger import get_logger
from pack_commerce import seguimiento as seg
from pack_commerce.models import Lead

log = get_logger(__name__)

EVENTO_RECORDATORIO = "commerce.seguimiento_vencido"
EVENTO_RESUMEN = "commerce.resumen_ventas"
SECCION = "leads"

# Un seguimiento vencido hace más de esto ya no genera aviso: sigue visible en «Vencidos». Cubre una
# noche, un fin de semana largo y un job caído, pero evita que, al encender el pack o al volver de una
# caída, lleguen de golpe avisos de semanas atrás.
VENTANA_RECORDATORIO = timedelta(days=4)
# El resumen sale desde su hora y hasta estas horas después (si el job estuvo caído); pasado eso, un
# «buenos días» a media tarde ya no tiene sentido y se salta ese día.
VENTANA_RESUMEN_HORAS = 4
# Etapas en las que el lead ya está resuelto: no se avisa de lo que ya no hay que hacer.
ETAPAS_RESUELTAS = ("convertido", "perdido")
TOPE_NOMBRE = 60
TOPE_NOTA = 140


@dataclass
class Conteo:
    """Qué pasó en una corrida, por negocio (para el log y las pruebas)."""

    enviados: int = 0
    ya_avisados: int = 0
    saltados_horario: int = 0
    saltados_resueltos: int = 0

    def sumar(self, otro: Conteo) -> None:
        self.enviados += otro.enviados
        self.ya_avisados += otro.ya_avisados
        self.saltados_horario += otro.saltados_horario
        self.saltados_resueltos += otro.saltados_resueltos


def clave_recordatorio(lead_id: uuid.UUID, en: datetime) -> str:
    return f"seguimiento:{lead_id}:{int(en.timestamp())}"


def clave_resumen(dia_local: str) -> str:
    return f"resumen_ventas:{dia_local}"


def _corto(texto: str | None, tope: int) -> str:
    limpio = " ".join((texto or "").split())
    return limpio if len(limpio) <= tope else limpio[: tope - 1].rstrip() + "…"


def spec_recordatorio(lead: Lead, cliente: str | None, zona: ZoneInfo) -> NotificationSpec:
    """«Seguimiento: <cliente>» con la nota en el cuerpo y la hora programada. El destino es la ficha
    del CLIENTE (HU-CRM-20). `data` lleva solo ids."""
    assert lead.next_action_at is not None and lead.customer_id is not None
    hora = lead.next_action_at.astimezone(zona).strftime("%H:%M")
    nota = _corto(lead.next_action_note, TOPE_NOTA)
    nombre = _corto(cliente, TOPE_NOMBRE) or "un cliente"
    return NotificationSpec(
        event_key=EVENTO_RECORDATORIO,
        category="ventas",
        severity="info",
        title=f"Seguimiento: {nombre}",
        body=f"{nota} (programado a las {hora})" if nota else f"Tenías un seguimiento programado a las {hora}.",
        dedupe_key=clave_recordatorio(lead.id, lead.next_action_at),
        section=SECCION,
        data={"kind": "seguimiento_vencido", "lead_id": str(lead.id)},
        target={"entity": "cliente", "id": str(lead.customer_id), "section": SECCION},
    )


def spec_resumen(dia_local: str, *, hoy: int, vencidos: int, nuevos: int) -> NotificationSpec:
    """Tu día de ventas. Abre la lista ya filtrada: por «Vencidos» si los hay; si no, por «Hoy»; sin
    ninguno de los dos, la lista de siempre."""
    filtro = "seguimiento:vencidos" if vencidos else ("seguimiento:hoy" if hoy else None)
    partes = [
        f"{hoy} {'seguimiento' if hoy == 1 else 'seguimientos'} para hoy",
        f"{vencidos} {'vencido' if vencidos == 1 else 'vencidos'}",
        f"{nuevos} {'lead nuevo' if nuevos == 1 else 'leads nuevos'} desde ayer",
    ]
    target: dict[str, str] = {"entity": "lead", "section": SECCION}
    if filtro:
        target["filtro"] = filtro
    return NotificationSpec(
        event_key=EVENTO_RESUMEN,
        category="ventas",
        severity="info",
        title="Tu día de ventas",
        body=" · ".join(partes) + ".",
        dedupe_key=clave_resumen(dia_local),
        section=SECCION,
        data={"kind": "resumen_ventas", "dia": dia_local, "hoy": hoy, "vencidos": vencidos, "nuevos": nuevos},
        target=target,
    )


async def _ya_avisadas(session, tenant_id: uuid.UUID, claves: list[str]) -> set[str]:  # type: ignore[no-untyped-def]
    if not claves:
        return set()
    filas = await session.execute(
        select(Notification.dedupe_key).where(
            Notification.tenant_id == tenant_id,
            Notification.dedupe_key.in_(claves),
            Notification.deleted_at.is_(None),
        )
    )
    return set(filas.scalars().all())


async def recordar_vencidos(  # type: ignore[no-untyped-def]
    session,
    tenant_id: uuid.UUID,
    *,
    ahora: datetime,
    zona: ZoneInfo,
    abierto: bool,
) -> Conteo:
    """Avisa de los seguimientos que ya llegaron a su hora y siguen pendientes.

    `abierto` = el negocio está dentro de su horario de atención: fuera de él no se avisa y el
    seguimiento queda para la primera corrida con el horario abierto (sigue dentro de la ventana)."""
    conteo = Conteo()
    candidatos = (
        await session.execute(
            select(Lead)
            .where(
                Lead.tenant_id == tenant_id,
                Lead.next_action_at.is_not(None),
                Lead.next_action_at <= ahora,
                Lead.next_action_at > ahora - VENTANA_RECORDATORIO,
                Lead.customer_id.is_not(None),
            )
            .order_by(Lead.next_action_at, Lead.id)
        )
    ).scalars().all()
    pendientes = []
    for lead in candidatos:
        if lead.status in ETAPAS_RESUELTAS:
            conteo.saltados_resueltos += 1
        else:
            pendientes.append(lead)
    if not pendientes:
        return conteo
    ya = await _ya_avisadas(session, tenant_id, [clave_recordatorio(lead.id, lead.next_action_at) for lead in pendientes])
    nuevos = [lead for lead in pendientes if clave_recordatorio(lead.id, lead.next_action_at) not in ya]
    conteo.ya_avisados = len(pendientes) - len(nuevos)
    if not nuevos:
        return conteo
    if not abierto:
        conteo.saltados_horario = len(nuevos)
        return conteo
    nombres = dict(
        (
            await session.execute(
                select(Customer.id, Customer.name).where(
                    Customer.tenant_id == tenant_id, Customer.id.in_([lead.customer_id for lead in nuevos])
                )
            )
        ).all()
    )
    for lead in nuevos:
        if await notify(session, tenant_id, spec_recordatorio(lead, nombres.get(lead.customer_id), zona)) is not None:
            conteo.enviados += 1
        else:
            conteo.ya_avisados += 1
    return conteo


async def resumen_diario(  # type: ignore[no-untyped-def]
    session,
    tenant_id: uuid.UUID,
    *,
    ahora: datetime,
    zona: ZoneInfo,
    hora: int,
) -> bool:
    """Manda «Tu día de ventas» una vez al día, desde `hora` (hora del negocio). Devuelve si salió.

    Cuenta con las mismas reglas que la lista: «vencidos» = ya pasó, «hoy» = lo que queda del día del
    negocio, para que el aviso y los filtros digan lo mismo. Sin nada que decir no manda nada."""
    local = ahora.astimezone(zona)
    if not hora <= local.hour < hora + VENTANA_RESUMEN_HORAS:
        return False
    dia = local.date().isoformat()
    if await _ya_avisadas(session, tenant_id, [clave_resumen(dia)]):
        return False
    lim = seg.limites(zona, ahora)
    abiertos = Lead.status.not_in(ETAPAS_RESUELTAS)

    async def contar(*condiciones) -> int:  # type: ignore[no-untyped-def]
        return int(
            (
                await session.execute(select(func.count(Lead.id)).where(Lead.tenant_id == tenant_id, *condiciones))
            ).scalar_one()
        )

    vencidos = await contar(abiertos, seg.condicion("vencidos", Lead.next_action_at, lim))
    hoy = await contar(abiertos, seg.condicion("hoy", Lead.next_action_at, lim))
    nuevos = await contar(Lead.created_at >= ahora - timedelta(hours=24))
    if not (vencidos or hoy or nuevos):
        return False
    return await notify(session, tenant_id, spec_resumen(dia, hoy=hoy, vencidos=vencidos, nuevos=nuevos)) is not None
