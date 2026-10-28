"""Catálogo de eventos que avisan al cliente y las rutas de datos que cada uno ofrece (ADR-0108, ADR-0178 D3).

Dato puro: `evento → {ruta: descripción}`. Vive en el kernel (y no en `gateway/event_notices.py`, que lo reexporta con
el mismo nombre) para que `kernel/opciones/contexto.py` derive de aquí las `RUTAS_DE_DATOS` de las plantillas con
opciones sin importar del gateway. Una ruta nueva aquí es una ruta que el panel ofrece como variable del aviso Y,
si el evento no es de cobranza, un dato que una plantilla con opciones puede citar: no agregues ids ni nada que no
sea un valor para mostrar (una prueba fija el conjunto).
"""

from __future__ import annotations

# Evento → rutas del contexto que se pueden usar como variables. Lo que no está
# aquí no se puede mapear (el panel solo ofrece estas rutas).
_VARIABLES_CARGO = {
    "cliente.nombre": "Nombre del cliente",
    "suscripcion.concepto": "Plan o concepto de la suscripción",
    "cargo.monto": "Monto del cargo",
    "cargo.moneda": "Moneda",
    "cargo.periodo": "Período (AAAA-MM)",
    "cargo.fecha_vencimiento": "Fecha de vencimiento",
    "cargo.referencia": "Referencia del pago",
    "cargo.link": "Link de pago",
    "cargo.datos_transferencia": "Datos de transferencia del negocio (cuentas)",
    "cargo.forma_de_pago": "Cómo pagar: el link o, si no hay, los datos de transferencia",
}

_VARIABLES_CITA = {
    "cliente.nombre": "Nombre del cliente",
    "cita.titulo": "Tipo o título de la cita",
    "cita.fecha": "Fecha de la cita",
    "cita.hora": "Hora de la cita",
    "cita.zona": "Zona horaria",
    "cita.enlace": "Enlace de la reunión",
}

EVENTOS: dict[str, dict[str, str]] = {
    "subscription.charge_created": dict(_VARIABLES_CARGO),
    # HU-SUB-05: recordatorios y pausa por mora.
    "subscription.charge_reminder": {
        **_VARIABLES_CARGO,
        "recordatorio.dias": "Cuándo vence (\"en 3 días\", \"hoy\", \"hace 3 días\")",
    },
    "subscription.paused": {
        "cliente.nombre": "Nombre del cliente",
        "suscripcion.concepto": "Plan o concepto de la suscripción",
        "cargo.monto": "Monto adeudado",
        "cargo.periodo": "Período adeudado",
        "cargo.link": "Link de pago",
        "cargo.datos_transferencia": "Datos de transferencia del negocio (cuentas)",
        "cargo.forma_de_pago": "Cómo pagar: el link o, si no hay, los datos de transferencia",
        "mora.dias": "Días de mora",
    },
    # HU-SH-02: citas (al cliente).
    "appointment.confirmed": dict(_VARIABLES_CITA),
    "appointment.reminder": dict(_VARIABLES_CITA),
    "appointment.no_show": dict(_VARIABLES_CITA),
    # HU-SUB-06: oferta de un plan pago antes de que termine la prueba de Free.
    "subscription.trial_ending": {
        "cliente.nombre": "Nombre del cliente",
        "suscripcion.concepto": "Plan o concepto de la suscripción",
        "prueba.termina": "Fecha en que termina la prueba",
    },
    # HU-SH-03 (ADR-0121): el acceso al negocio comprado llega por correo. Sin enlace ni
    # token: el enlace va SOLO por correo y el aviso es igual en todos los casos.
    "provisioning.invitation": {
        "cliente.nombre": "Nombre del cliente",
        "negocio.nombre": "Nombre del negocio",
        "invitacion.correo": "Correo al que llega el acceso (enmascarado)",
    },
}

# Eventos de cobranza (Ley 2300, HU-SUB-18). Su aviso solo debe salir en horario legal, y la entrega no lo puede
# garantizar en todos los caminos (cola diferida de QR, reintentos del outbox, reenvío manual del panel). Por eso
# no salen por QR como texto (ADR-0178 D2) ni se mapean a plantillas con opciones (ADR-0178 D3).
COBRANZA = frozenset({"subscription.charge_created", "subscription.charge_reminder", "subscription.paused"})
