"""Catálogo de los crons de la plataforma (ADR-0183).

Fuente única de la clasificación por módulo y del horario por defecto de cada tarea periódica
del beat. La API de gestión de crons y el planificador leen de aquí, no de
`celery.conf.beat_schedule`: ese diccionario solo tiene los crons de los packs una vez cargados
los plugins, y la API no siempre los ha cargado.

`tests/test_cron_catalogo.py` cierra el catálogo en los dos sentidos: cada entrada del beat
debe tener su metadata (un cron nuevo sin clasificar rompe el CI) y cada horario por defecto
debe coincidir con el crontab del código.

Este módulo no importa `celery_app` (lo importa `celery_app`), solo `celery.schedules`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from celery.schedules import crontab

# Orden de los módulos en la pantalla.
MODULOS: tuple[str, ...] = (
    "Canales y Meta",
    "Facturación de la plataforma",
    "Suscripciones",
    "Cobranza",
    "Comercio y CRM",
    "Calendario",
    "Memoria e IA",
    "Releases y canary",
    "Agent OS",
    "Notificaciones",
    "Acceso y altas",
    "Plataforma",
)
SIN_CLASIFICAR = "Sin clasificar"

_CAMPO = re.compile(r"^[0-9*/,\-]+$")


@dataclass(frozen=True)
class CronMeta:
    """Metadata de un cron. `default` es la expresión de 5 campos (UTC); los crons por intervalo
    fijo (p. ej. cada 30 s) llevan `interval_s` y no son reprogramables."""

    name: str
    module: str
    label: str
    description: str
    default: str | None = None
    interval_s: float | None = None
    # No se puede pausar: sin él la plataforma deja de funcionar (latido, outbox).
    critical: bool = False
    # Toca dinero o escribe a clientes: la pantalla pide una confirmación reforzada.
    sensitive: bool = False
    reprogramable: bool = True
    # False = no se guarda cada ejecución exitosa (latido, outbox: miles al día); los fallos y las
    # ejecuciones manuales siempre se guardan.
    record_success: bool = True

    def __post_init__(self) -> None:
        if (self.default is None) == (self.interval_s is None):
            raise ValueError(f"{self.name}: define `default` o `interval_s`, uno solo")
        if self.interval_s is not None and self.reprogramable:
            raise ValueError(f"{self.name}: un cron por intervalo no es reprogramable")


def _m(
    name: str, module: str, label: str, description: str, default: str | None = None, **kw: object
) -> tuple[str, CronMeta]:
    return name, CronMeta(name, module, label, description, default, **kw)  # type: ignore[arg-type]


CATALOGO: dict[str, CronMeta] = dict(
    [
        # ---- Canales y Meta
        _m("core.meta_subscriptions_health_check", "Canales y Meta", "Salud de las suscripciones de webhook",
           "Revisa que Meta siga entregando los webhooks de cada conexión.", "*/15 * * * *"),
        _m("core.instagram_token_refresh", "Canales y Meta", "Refresco del token de Instagram",
           "Refresca el token de 60 días de Instagram Login de las conexiones por vencer.", "30 3 * * *"),
        _m("core.message_template_sync", "Canales y Meta", "Sincronización de plantillas de WhatsApp",
           "Trae el estado de las plantillas de cada cuenta de WhatsApp Business (respaldo del webhook).",
           "0 * * * *"),
        _m("core.meta_pricing_sync", "Canales y Meta", "Sincronización de precios de Meta",
           "Trae el costo real de los mensajes (pricing_analytics) de cada cuenta.", "0 2 * * *"),
        _m("core.meta_health_check", "Canales y Meta", "Salud de las conexiones de Meta",
           "Revisión completa de las conexiones y de la cola de fallos.", "30 * * * *"),
        _m("core.meta_token_expiry_check", "Canales y Meta", "Aviso de tokens por vencer",
           "Alerta de los access tokens de Meta vencidos o por vencer.", "15 3 * * *"),
        _m("core.twilio_template_poll", "Canales y Meta", "Aprobación de plantillas de Twilio",
           "Consulta si WhatsApp ya aprobó o rechazó las plantillas de Twilio en revisión.", "*/5 * * * *"),
        _m("core.twilio_template_sync", "Canales y Meta", "Sincronización de plantillas de Twilio",
           "Trae todas las plantillas de cada cuenta de Twilio y el estado de su aprobación.", "20 * * * *"),
        _m("core.twilio_health_check", "Canales y Meta", "Salud de las conexiones de Twilio",
           "Revisa la cuenta, el número y los webhooks de cada conexión de WhatsApp vía Twilio.", "*/15 * * * *"),
        # ---- Facturación de la plataforma
        _m("core.billing_lifecycle", "Facturación de la plataforma", "Normalización de suscripciones vencidas",
           "Pasa las suscripciones de prueba o activas vencidas a su estado real.", "0 4 * * *"),
        _m("core.billing_renovacion", "Facturación de la plataforma", "Renovación del plan",
           "Cobra el período siguiente de cada negocio y avisa por su canal preferido.", "0 13 * * *",
           sensitive=True),
        _m("core.billing_conciliacion", "Facturación de la plataforma", "Conciliación de cobros",
           "Compara los dos canales de cobro y alerta cada diferencia.", "30 5 * * *"),
        # ---- Suscripciones (pack)
        _m("pack_subscriptions.expirar_inscripciones", "Suscripciones", "Expirar inscripciones",
           "Vence las inscripciones que llegaron a su fin.", "30 3 * * *"),
        _m("pack_subscriptions.sincronizar_precios_de_plan", "Suscripciones", "Sincronizar precios del plan",
           "Aplica los cambios de precio programados de los planes.", "10 * * * *"),
        _m("pack_subscriptions.generar_cargos", "Suscripciones", "Generar cargos",
           "Genera los cargos del período de cada suscripción.", "15 3 * * *", sensitive=True),
        _m("pack_subscriptions.verificar_descuentos", "Suscripciones", "Verificar descuentos",
           "Revisa la vigencia de las promociones y descuentos.", "30 3 * * *"),
        _m("pack_subscriptions.marcar_vencidas", "Suscripciones", "Marcar suscripciones vencidas",
           "Marca como vencidas las suscripciones con cobros sin pagar.", "45 3 * * *"),
        _m("pack_subscriptions.avisos_de_cobro", "Suscripciones", "Avisos de cobro",
           "Envía los avisos de cobro a los clientes, respetando el horario legal de cada negocio.",
           "5 * * * *", sensitive=True),
        # ---- Cobranza (pack)
        _m("pack_cobranza.marcar_mora", "Cobranza", "Marcar mora",
           "Marca las cuotas vencidas como en mora.", "0 2 * * *"),
        _m("pack_cobranza.generar_cargos_suscripcion", "Cobranza", "Generar cargos de suscripción",
           "Genera los cargos recurrentes de la cartera.", "10 2 * * *", sensitive=True),
        _m("pack_cobranza.cierre_diario", "Cobranza", "Cierre diario",
           "Cierra el día de cobranza de cada negocio.", "55 23 * * *"),
        # ---- Comercio y CRM (pack)
        _m("pack_commerce.follow_up_leads", "Comercio y CRM", "Seguimiento de leads",
           "Escribe a los leads que quedaron sin respuesta.", "0 9 * * *", sensitive=True),
        _m("pack_commerce.follow_up_quotes", "Comercio y CRM", "Seguimiento de cotizaciones",
           "Escribe a los clientes con cotizaciones sin cerrar.", "30 9,15,20 * * *", sensitive=True),
        _m("pack_commerce.recordar_seguimientos", "Comercio y CRM", "Recordatorios de seguimiento",
           "Avisa al equipo de los seguimientos de leads que vencen.", "*/5 * * * *"),
        # ---- Calendario (pack)
        _m("pack_calendar.recordatorios_de_citas", "Calendario", "Recordatorios de citas",
           "Envía el recordatorio de las citas próximas.", "*/15 * * * *", sensitive=True),
        # ---- Memoria e IA
        _m("core.memory_consolidation", "Memoria e IA", "Consolidación de la memoria",
           "Convierte los datos de las tareas cerradas en hechos de memoria.", "30 3 * * *"),
        _m("core.memory_retention", "Memoria e IA", "Retención de la memoria",
           "Purga la memoria semántica vencida (no hace nada si la retención está en 0).", "45 3 * * *"),
        # ---- Releases y canary
        _m("core.canary_senal_dura", "Releases y canary", "Canary: señal dura",
           "Vigila las versiones en canary y retira la habilidad que falla.", "*/5 * * * *"),
        _m("core.canary_no_inferioridad", "Releases y canary", "Canary: no inferioridad",
           "Compara la versión canary contra la estable.", "0 * * * *"),
        # ---- Agent OS
        _m("core.kernel_esperas_vencidas", "Agent OS", "Esperas vencidas",
           "Cancela las tareas que esperan un pago más de 72 horas.", "25 * * * *"),
        # ---- Notificaciones
        _m("core.cliente_esperando", "Notificaciones", "Cliente esperando respuesta",
           "Avisa de los clientes con una escalación abierta sin respuesta humana.", "*/5 * * * *",
           sensitive=True),
        _m("core.notifications_retention", "Notificaciones", "Retención de notificaciones",
           "Da de baja las notificaciones de más de 180 días.", "0 4 * * *"),
        # ---- Acceso y altas
        _m("core.access_events_retention", "Acceso y altas", "Retención de eventos de acceso",
           "Da de baja los eventos de acceso de más de 180 días.", "10 4 * * *"),
        _m("core.alta_huellas_retencion", "Acceso y altas", "Retención de huellas de altas",
           "Olvida las huellas de teléfono y documento de los pedidos de alta a los 12 meses.", "10 4 * * *"),
        # ---- Plataforma
        _m("core.worker_heartbeat", "Plataforma", "Latido del worker",
           "Marca que el worker está vivo; lo lee el checklist de la plataforma.", "*/1 * * * *",
           critical=True, reprogramable=False, record_success=False),
        _m("core.domain_events_dispatch", "Plataforma", "Despachador de eventos de dominio",
           "Entrega los eventos pendientes del outbox, con reintentos.", None, interval_s=30.0,
           critical=True, reprogramable=False, record_success=False),
        _m("core.cron_runs_retention", "Plataforma", "Mantenimiento del historial de crons",
           "Borra el historial de ejecuciones de más de 30 días y cierra las que quedaron colgadas.",
           "20 4 * * *"),
    ]
)


def get_meta(name: str) -> CronMeta | None:
    return CATALOGO.get(name)


def parse_expression(expression: str) -> crontab:
    """Expresión de 5 campos (minuto hora día-del-mes mes día-de-la-semana, UTC) → `crontab`.

    Solo cifras, `*`, `/`, `,` y `-`. Lanza `ValueError` con un texto apto para mostrar."""
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("La expresión debe tener 5 campos: minuto hora día mes día-de-la-semana.")
    for part in parts:
        if not _CAMPO.match(part):
            raise ValueError(f"Campo no válido: «{part}». Usa cifras, *, /, , y -.")
    minute, hour, day_of_month, month, day_of_week = parts
    try:
        return crontab(
            minute=minute, hour=hour, day_of_month=day_of_month,
            month_of_year=month, day_of_week=day_of_week,
        )
    except Exception as exc:  # celery lanza ParseException/ValueError según el campo
        raise ValueError(f"Expresión no válida: {exc}") from exc


def validate_schedule(expression: str) -> str:
    """Valida y devuelve la expresión normalizada (espacios simples)."""
    expression = " ".join(expression.split())
    parse_expression(expression)
    return expression
