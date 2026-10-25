"""Cron jobs de pack-subscriptions (ADR-0083, #156).

- `generar_cargos`: genera el cargo del período de cada suscripción vencida y,
  si `pack_payments` está habilitado, su link de pago. Idempotente por período.
- `avisos_de_cobro`: cada hora; avisa al cliente (cargo nuevo, recordatorios, pausa por
  mora, fin de prueba) solo dentro del horario legal del negocio y una vez por día
  (HU-SUB-18, Ley 2300 de 2023).

El decorador `@register_cron` salta si el pack está inactivo; el filtro por
tenant habilitado es explícito (`_tenant_ids`, ADR-0019).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from core.db.session import get_session
from core.events.outbox import emit_domain
from core.shared.events import (
    ON_SUBSCRIPTION_CHARGE_CREATED,
    ON_SUBSCRIPTION_CHARGE_REMINDER,
    ON_SUBSCRIPTION_PAST_DUE,
    ON_SUBSCRIPTION_PAUSED,
    ON_SUBSCRIPTION_TRIAL_ENDING,
)
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from pack_subscriptions.avisos import recordatorio_de_hoy
from pack_subscriptions.horario_legal import (
    en_horario_de_cobro,
    hora_local,
    proximo_inicio_de_cobro,
    siguiente_dia,
)
from pack_subscriptions.models import Subscription, SubscriptionCharge
from pack_subscriptions.periodos import etiqueta_periodo, sumar_periodo

log = get_logger(__name__)

PACK_ID = "pack_subscriptions"


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants cuyo AgentSpec incluye este pack (ADR-0019)."""
    from core.agents.spec import enabled_packs, load_active_spec

    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null"))
        ).scalars().all()

    result: list[uuid.UUID] = []
    for raw in rows:
        tid = uuid.UUID(str(raw))
        try:
            async with tenant_scope(tid):
                spec = await load_active_spec()
            if PACK_ID in enabled_packs(spec):
                result.append(tid)
        except Exception:
            continue
    return result


async def _config_del_negocio():  # type: ignore[no-untyped-def]
    from core.plugin_manager import plugin_manager
    from pack_subscriptions.config import SubscriptionConfig

    try:
        config = await plugin_manager.load_config(PACK_ID)
    except Exception:
        config = None
    return config if isinstance(config, SubscriptionConfig) else SubscriptionConfig()


async def zona_del_negocio(session) -> str | None:  # type: ignore[no-untyped-def]
    """Zona horaria del negocio actual (ADR-0091); None = la de Bogotá."""
    from core.db.models import AppSettings
    from core.shared.tenant import get_current_tenant_id

    return (
        await session.execute(
            select(AppSettings.timezone).where(AppSettings.tenant_id == get_current_tenant_id())
        )
    ).scalars().first()


async def ya_emitido(session, tenant_id: uuid.UUID, dedupe_key: str) -> bool:  # type: ignore[no-untyped-def]
    """True si el outbox ya tiene un evento con esa clave (índice único por negocio)."""
    return (
        await session.execute(
            text("select 1 from domain_events where tenant_id = :t and dedupe_key = :k limit 1"),
            {"t": tenant_id, "k": dedupe_key},
        )
    ).first() is not None


async def reservar_aviso_del_dia(  # type: ignore[no-untyped-def]
    session, tenant_id: uuid.UUID, customer_id: uuid.UUID, fecha: date, topic: str, charge_id: uuid.UUID | None
) -> bool:
    """Reserva el único aviso de cobro del día para el cliente (HU-SUB-18). False si ya
    tuvo uno ese día en la fecha local del negocio: el aviso espera al día hábil siguiente.
    Va en la misma transacción que el evento: si el evento se revierte, la reserva también."""
    fila = (
        await session.execute(
            text(
                "insert into pack_subscriptions_avisos_cobro "
                "(tenant_id, customer_id, fecha, topic, charge_id) "
                "values (:t, :c, :f, :topic, :cargo) on conflict do nothing returning 1"
            ),
            {"t": tenant_id, "c": customer_id, "f": fecha, "topic": topic, "cargo": charge_id},
        )
    ).first()
    return fila is not None


@register_cron(PACK_ID, "15 3 * * *")
async def generar_cargos() -> None:
    await _generar_cargos(datetime.now(UTC))


async def _generar_cargos(now: datetime) -> int:
    """Cargo del período de cada suscripción que llegó a su `proximo_cargo`
    (ADR-0083, HU-SUB-04). Idempotente por período.

    - El período sale del calendario (`periodicidad` + `dia_ancla`), no `+30 días`.
    - No cambia el estado: antes ponía `activa` aunque nadie pagara (defecto 2).
    - No cobra durante una prueba ni planes en $0 (Free, HU-SUB-06).
    - Las `vencida` no acumulan cargos: al pagar, el período siguiente se calcula
      desde el pagado (`_on_payment_confirmed`).
    Si hay pasarela, genera el link (`sub-<cargo.id>`) y deja el aviso al cliente en el
    outbox para el próximo horario legal (HU-SUB-18): este cron corre de noche.
    """
    total = 0
    for tenant_id in await _tenant_ids():
        from core.agents.spec import enabled_packs, load_active_spec
        from core.payments import payment_registry

        async with tenant_scope(tenant_id):
            spec = await load_active_spec()
            cap = payment_registry.capability_for(enabled_packs(spec))
            config = await _config_del_negocio()
            # HU-SUB-05: el cargo se genera `dias_anticipacion` antes del vencimiento
            # para que el link llegue antes (su vencimiento sigue siendo el inicio del
            # período, D-SPEC-06-3).
            horizonte = now + timedelta(days=config.dias_anticipacion)
            async with get_session() as session:
                subs = (
                    await session.execute(
                        select(Subscription).where(
                            Subscription.tenant_id == tenant_id,
                            Subscription.estado.in_(("trialing", "activa")),
                            Subscription.deleted_at.is_(None),
                            Subscription.proximo_cargo.isnot(None),
                            Subscription.proximo_cargo <= horizonte,
                        )
                    )
                ).scalars().all()
                generadas = 0
                for sub in subs:
                    if await generar_cargo_de(session, tenant_id, sub, now, cap=cap, config=config) is not None:
                        generadas += 1
        total += generadas
        log.info("subs_cargos_generados", tenant_id=str(tenant_id), generadas=generadas)
    return total


async def generar_cargo_de(session, tenant_id: uuid.UUID, sub: Subscription, now: datetime, *, cap, config) -> SubscriptionCharge | None:  # type: ignore[no-untyped-def]
    """Cargo del período que empieza en `sub.proximo_cargo`, con su link y su aviso, y avanza
    el calendario de la suscripción. El aviso queda en el outbox para el próximo horario
    legal del negocio, en un día en que el cliente no tenga otro aviso de cobro (HU-SUB-18). None si no corresponde cobrar (prueba, Free, plan en
    $0) o el cargo del período ya existía. Si el último período lo pagó otro canal, el cargo
    se crea sin link ni aviso (HU-SH-04)."""
    if sub.trial_termina is not None and sub.trial_termina > now:
        return None
    if sub.estado == "trialing" and not Decimal(sub.valor_periodo or 0):
        # Free al terminar la prueba: no se renueva, vence (`_marcar_vencidas`).
        return None
    inicio = sub.proximo_cargo
    if inicio is None:
        return None
    _aplicar_cambio_programado(sub, inicio)
    # Un cambio de plan pendiente se calculó sobre el período que termina:
    # pagarlo después ya no aplica (auditoría HU-SUB-07).
    from pack_subscriptions.capability import _anular_cambios_pendientes

    await _anular_cambios_pendientes(session, sub)
    siguiente = sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla)
    if not Decimal(sub.valor_periodo or 0):
        sub.periodo_actual_inicio = inicio
        sub.proximo_cargo = siguiente
        return None
    periodo = etiqueta_periodo(inicio, sub.periodicidad)
    existe = (
        await session.execute(
            select(SubscriptionCharge).where(
                SubscriptionCharge.tenant_id == tenant_id,
                SubscriptionCharge.subscription_id == sub.id,
                SubscriptionCharge.periodo == periodo,
                SubscriptionCharge.tipo == "periodo",
                SubscriptionCharge.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    sub.periodo_actual_inicio = inicio
    sub.proximo_cargo = siguiente
    if existe is not None:
        return None
    cargo = SubscriptionCharge(
        tenant_id=tenant_id,
        subscription_id=sub.id,
        periodo=periodo,
        monto=Decimal(sub.valor_periodo or 0),
        estado="pendiente",
        fecha_vencimiento=inicio,
    )
    session.add(cargo)
    await session.flush()
    if await paga_por_otro_canal(session, tenant_id, sub):
        # HU-SH-04 (ADR-0123): el último período lo pagó otro canal (p. ej. el panel de la
        # plataforma). El cargo existe, pero sin link ni aviso: el cliente no recibe dos cobros
        # del mismo período. Si paga por el otro canal, se registra como pago externo.
        log.info("subs_cargo_sin_aviso_paga_por_otro_canal", subscription_id=str(sub.id), periodo=periodo)
        return cargo
    if cap is not None:
        try:
            link = await cap.create_link(
                reference=f"sub-{cargo.id}",
                amount=float(cargo.monto or 0),
                description=f"Suscripción {sub.concepto}",
            )
            cargo.referencia = link.reference
            cargo.link_url = link.url
        except Exception:
            log.warning("subs_link_fallido", subscription_id=str(sub.id), exc_info=True)
    # Aviso al cliente por el outbox, en la MISMA transacción que el cargo (ADR-0107): si el
    # cargo se revierte, no hay aviso. El core lo envía como plantilla o texto según la
    # ventana de 24 h (ADR-0108); la entrega espera el horario legal (HU-SUB-18).
    entregar_desde = await _turno_legal_del_aviso(session, tenant_id, sub, cargo, now, config)
    await emit_domain(
        session,
        tenant_id=tenant_id,
        topic=ON_SUBSCRIPTION_CHARGE_CREATED,
        dedupe_key=f"sub_charge:{cargo.id}",
        payload=payload_cargo_nuevo(cargo, sub, config.aviso_cargo_texto),
        entregar_desde=entregar_desde,
    )
    return cargo


async def _turno_legal_del_aviso(session, tenant_id: uuid.UUID, sub: Subscription, cargo: SubscriptionCharge, now: datetime, config) -> datetime | None:  # type: ignore[no-untyped-def]
    """Primer momento del horario legal en un día en que el cliente no tenga otro aviso de
    cobro, con ese día ya reservado. None (entregar ya) solo si la ventana del negocio no
    abre en dos semanas, que la validación de la config no permite."""
    tz = await zona_del_negocio(session)
    momento: datetime | None = now
    for _ in range(15):
        momento = proximo_inicio_de_cobro(momento, tz, config.ventana_cobro, config.pais_festivos)
        if momento is None:
            break
        if await reservar_aviso_del_dia(
            session, tenant_id, sub.customer_id, hora_local(momento, tz).date(),
            ON_SUBSCRIPTION_CHARGE_CREATED, cargo.id,
        ):
            return momento
        momento = siguiente_dia(momento, tz)
    log.warning("subs_aviso_cargo_sin_turno_legal", subscription_id=str(sub.id))
    return None


@register_cron(PACK_ID, "45 3 * * *")
async def marcar_vencidas() -> None:
    await _marcar_vencidas(datetime.now(UTC))


async def _marcar_vencidas(now: datetime) -> int:
    """Pasa a `vencida` la suscripción con un cargo impago más allá de los días de
    gracia del negocio (D-SPEC-06-3, `SubscriptionConfig.dias_gracia`) y emite
    `subscription.past_due` por el outbox (HU-SUB-04; los avisos son HU-SUB-05)."""
    total = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            config = await _config_del_negocio()
            limite = now - timedelta(days=config.dias_gracia)
            async with get_session() as session:
                filas = (
                    await session.execute(
                        select(SubscriptionCharge, Subscription)
                        .join(Subscription, Subscription.id == SubscriptionCharge.subscription_id)
                        .where(
                            SubscriptionCharge.tenant_id == tenant_id,
                            Subscription.tenant_id == tenant_id,
                            SubscriptionCharge.estado == "pendiente",
                            SubscriptionCharge.tipo == "periodo",
                            SubscriptionCharge.deleted_at.is_(None),
                            SubscriptionCharge.fecha_vencimiento.isnot(None),
                            SubscriptionCharge.fecha_vencimiento <= limite,
                            Subscription.estado.in_(("activa", "trialing")),
                            Subscription.deleted_at.is_(None),
                        )
                        .order_by(SubscriptionCharge.fecha_vencimiento)
                    )
                ).all()
                vistas: set[uuid.UUID] = set()
                for cargo, sub in filas:
                    if sub.id in vistas:
                        continue
                    vistas.add(sub.id)
                    sub.estado = "vencida"
                    await emit_domain(
                        session,
                        tenant_id=tenant_id,
                        topic=ON_SUBSCRIPTION_PAST_DUE,
                        dedupe_key=f"sub_past_due:{cargo.id}",
                        payload={
                            "charge_id": str(cargo.id),
                            "subscription_id": str(sub.id),
                            "customer_id": str(sub.customer_id),
                            "concepto": sub.concepto,
                            "monto": str(cargo.monto),
                            "moneda": sub.moneda,
                            "periodo": cargo.periodo,
                            "fecha_vencimiento": cargo.fecha_vencimiento.date().isoformat(),
                            "dias_gracia": config.dias_gracia,
                        },
                    )
                # HU-SUB-06: Free sin pasar a un plan pago vence al terminar la prueba.
                free_fin = (
                    await session.execute(
                        select(Subscription).where(
                            Subscription.tenant_id == tenant_id,
                            Subscription.estado == "trialing",
                            Subscription.valor_periodo == 0,
                            Subscription.trial_termina.isnot(None),
                            Subscription.trial_termina <= now,
                            Subscription.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
                for sub in free_fin:
                    if sub.id in vistas:
                        continue
                    vistas.add(sub.id)
                    sub.estado = "vencida"
                    sub.proximo_cargo = None
                    await emit_domain(
                        session,
                        tenant_id=tenant_id,
                        topic=ON_SUBSCRIPTION_PAST_DUE,
                        dedupe_key=f"sub_trial_expired:{sub.id}",
                        payload={
                            "subscription_id": str(sub.id),
                            "customer_id": str(sub.customer_id),
                            "concepto": sub.concepto,
                            "motivo": "fin_de_prueba",
                            "trial_termina": sub.trial_termina.date().isoformat() if sub.trial_termina else None,
                        },
                    )
            total += len(vistas)
            if vistas:
                log.info("subs_vencidas", tenant_id=str(tenant_id), total=len(vistas))
    return total


async def paga_por_otro_canal(session, tenant_id: uuid.UUID, sub: Subscription) -> bool:  # type: ignore[no-untyped-def]
    """¿El último período pagado de la suscripción lo pagó otro canal (`pago_externo`)?
    Entonces el cliente paga por ahí: el pack no le manda link ni recordatorios
    (HU-SH-04, ADR-0123). Vuelve a cobrarle cuando pague un período con el link del pack."""
    ultimo = (
        await session.execute(
            select(SubscriptionCharge)
            .where(
                SubscriptionCharge.tenant_id == tenant_id,
                SubscriptionCharge.subscription_id == sub.id,
                SubscriptionCharge.tipo == "periodo",
                SubscriptionCharge.estado == "pagado",
                SubscriptionCharge.deleted_at.is_(None),
            )
            .order_by(SubscriptionCharge.fecha_vencimiento.desc().nulls_last(), SubscriptionCharge.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return ultimo is not None and bool(ultimo.pago_externo)


def _aplicar_cambio_programado(sub: Subscription, inicio: datetime) -> None:
    """Downgrade programado (HU-SUB-07): el período que empieza en `inicio` ya va con el
    plan nuevo, así que su cargo sale con el precio nuevo."""
    cambio = sub.cambio_programado or {}
    desde = cambio.get("desde")
    if not cambio or not desde or datetime.fromisoformat(desde) > inicio:
        return
    from pack_subscriptions.capability import aplicar_plan

    aplicar_plan(sub, cambio, inicio)
    log.info("subs_downgrade_aplicado", subscription_id=str(sub.id), plan=sub.concepto)


def _payload_cargo(cargo: SubscriptionCharge, sub: Subscription) -> dict[str, object]:
    return {
        "charge_id": str(cargo.id),
        "subscription_id": str(sub.id),
        "customer_id": str(sub.customer_id),
        "concepto": sub.concepto,
        "monto": str(cargo.monto),
        "moneda": sub.moneda,
        "periodo": cargo.periodo,
        "fecha_vencimiento": cargo.fecha_vencimiento.date().isoformat() if cargo.fecha_vencimiento else None,
        "referencia": cargo.referencia,
        "link": cargo.link_url,
    }


def payload_cargo_nuevo(cargo: SubscriptionCharge, sub: Subscription, texto: str) -> dict[str, object]:
    """Payload del aviso de cargo nuevo (`subscription.charge_created`)."""
    return {**_payload_cargo(cargo, sub), "texto": texto}


@register_cron(PACK_ID, "5 * * * *")
async def avisos_de_cobro() -> None:
    """Avisos de cobro (HU-SUB-05) cada hora; cada negocio solo dentro de su horario
    legal (HU-SUB-18)."""
    await _avisos_de_cobro(datetime.now(UTC))


async def _avisos_de_cobro(now: datetime) -> dict[str, int]:
    """Por cada negocio **dentro de su horario legal de cobro** (HU-SUB-18: Ley 2300, zona
    del negocio, sin domingos ni festivos) y por cada cargo impago de una suscripción viva:

    - **recordatorio** del día configurado que toque (`recordatorio_de_hoy`), una vez;
    - **pausa** si el negocio la activó y la mora pasó `pausar_tras_dias_mora`: la
      suscripción queda `pausada` (`motivo_pausa = "mora"`), se avisa al cliente y a
      los dueños.
    Un cargo pagado no entra (sin spam). Todo sale por el outbox (`dedupe_key`).
    **Un aviso por cliente y día** (`reservar_aviso_del_dia`): los cargos se recorren del más
    viejo al más nuevo y lo que no cabe hoy sale el siguiente día hábil (el recordatorio que
    toque entonces; `recordatorio_de_hoy` no manda ráfagas).
    """
    totales = {"recordatorios": 0, "pausadas": 0, "fin_de_prueba": 0, "fuera_de_horario": 0}
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            config = await _config_del_negocio()
            async with get_session() as session:
                tz = await zona_del_negocio(session)
                if not en_horario_de_cobro(now, tz, config.ventana_cobro, config.pais_festivos):
                    totales["fuera_de_horario"] += 1
                    continue
                hoy = hora_local(now, tz).date()
                filas = (
                    await session.execute(
                        select(SubscriptionCharge, Subscription)
                        .join(Subscription, Subscription.id == SubscriptionCharge.subscription_id)
                        .where(
                            SubscriptionCharge.tenant_id == tenant_id,
                            Subscription.tenant_id == tenant_id,
                            SubscriptionCharge.estado == "pendiente",
                            SubscriptionCharge.tipo == "periodo",
                            SubscriptionCharge.deleted_at.is_(None),
                            SubscriptionCharge.fecha_vencimiento.isnot(None),
                            Subscription.estado.in_(("activa", "vencida", "trialing")),
                            Subscription.deleted_at.is_(None),
                        )
                        .order_by(SubscriptionCharge.fecha_vencimiento)
                    )
                ).all()
                pausadas: set[uuid.UUID] = set()
                for cargo, sub in filas:
                    vence = cargo.fecha_vencimiento
                    dias_mora = (now - vence).days
                    if (
                        config.pausar_tras_dias_mora
                        and dias_mora >= config.pausar_tras_dias_mora
                        and sub.estado in ("activa", "vencida")
                        and sub.id not in pausadas
                    ):
                        if not await reservar_aviso_del_dia(
                            session, tenant_id, sub.customer_id, hoy, ON_SUBSCRIPTION_PAUSED, cargo.id
                        ):
                            continue  # ya tuvo su aviso de hoy: se pausa el siguiente día hábil
                        pausadas.add(sub.id)
                        sub.estado = "pausada"
                        sub.motivo_pausa = "mora"
                        sub.proximo_cargo = None
                        await emit_domain(
                            session,
                            tenant_id=tenant_id,
                            topic=ON_SUBSCRIPTION_PAUSED,
                            dedupe_key=f"sub_paused:{sub.id}:{cargo.id}",
                            payload={
                                **_payload_cargo(cargo, sub),
                                "dias_mora": dias_mora,
                                "texto": config.aviso_pausa_texto,
                            },
                        )
                        await _avisar_pausa_al_negocio(tenant_id, sub, cargo, dias_mora)
                        continue
                    if sub.id in pausadas:
                        continue
                    if await paga_por_otro_canal(session, tenant_id, sub):
                        continue  # HU-SH-04: lo cobra el otro canal
                    dia = recordatorio_de_hoy(
                        vence.date(), cargo.created_at.date(), hoy, config.recordatorios_dias
                    )
                    if dia is None or await ya_emitido(session, tenant_id, f"sub_reminder:{cargo.id}:{dia}"):
                        continue
                    if not await reservar_aviso_del_dia(
                        session, tenant_id, sub.customer_id, hoy, ON_SUBSCRIPTION_CHARGE_REMINDER, cargo.id
                    ):
                        continue
                    insertado = await emit_domain(
                        session,
                        tenant_id=tenant_id,
                        topic=ON_SUBSCRIPTION_CHARGE_REMINDER,
                        dedupe_key=f"sub_reminder:{cargo.id}:{dia}",
                        payload={**_payload_cargo(cargo, sub), "dias": dia, "texto": config.recordatorio_texto},
                    )
                    totales["recordatorios"] += int(insertado)
                totales["pausadas"] += len(pausadas)
                totales["fin_de_prueba"] += await _ofertas_fin_de_prueba(session, tenant_id, config, now, hoy)
    if any(totales.values()):
        log.info("subs_avisos_de_cobro", **totales)
    return totales


async def _ofertas_fin_de_prueba(session, tenant_id: uuid.UUID, config, now: datetime, hoy: date) -> int:  # type: ignore[no-untyped-def]
    """Free en prueba que termina dentro de `dias_aviso_fin_prueba`: se le ofrece un
    plan pago (HU-SUB-06), una sola vez por suscripción. Es un mensaje comercial: va en el
    mismo horario y cuenta para el aviso del día (la Superfinanciera aplica la Ley 2300
    también a la publicidad)."""
    if not config.dias_aviso_fin_prueba:
        return 0
    subs = (
        await session.execute(
            select(Subscription).where(
                Subscription.tenant_id == tenant_id,
                Subscription.estado == "trialing",
                Subscription.valor_periodo == 0,
                Subscription.trial_termina.isnot(None),
                Subscription.trial_termina > now,
                Subscription.trial_termina <= now + timedelta(days=config.dias_aviso_fin_prueba),
                Subscription.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    enviados = 0
    for sub in subs:
        if await ya_emitido(session, tenant_id, f"sub_trial_ending:{sub.id}"):
            continue
        if not await reservar_aviso_del_dia(
            session, tenant_id, sub.customer_id, hoy, ON_SUBSCRIPTION_TRIAL_ENDING, None
        ):
            continue
        insertado = await emit_domain(
            session,
            tenant_id=tenant_id,
            topic=ON_SUBSCRIPTION_TRIAL_ENDING,
            dedupe_key=f"sub_trial_ending:{sub.id}",
            payload={
                "subscription_id": str(sub.id),
                "customer_id": str(sub.customer_id),
                "concepto": sub.concepto,
                "trial_termina": sub.trial_termina.date().isoformat(),
                "texto": config.aviso_fin_prueba_texto,
            },
        )
        enviados += int(insertado)
    return enviados


async def _avisar_pausa_al_negocio(
    tenant_id: uuid.UUID, sub: Subscription, cargo: SubscriptionCharge, dias_mora: int
) -> None:
    from core.shared.audit import record_audit

    await record_audit(
        actor=f"plugin:{PACK_ID}",
        action="subs_pausada_por_mora",
        output_data={"subscription_id": str(sub.id), "charge_id": str(cargo.id), "dias_mora": dias_mora},
    )
    try:
        from core.tasks.push_tasks import notify_owners_push

        notify_owners_push.delay(
            str(tenant_id),
            "Suscripción pausada por mora",
            f"Se pausó «{sub.concepto}»: el pago de {cargo.monto} lleva {dias_mora} días pendiente.",
            {"kind": "subs_pausada_por_mora", "subscription_id": str(sub.id)},
        )
    except Exception:
        log.warning("subs_alerta_push_fallida", subscription_id=str(sub.id), exc_info=True)
