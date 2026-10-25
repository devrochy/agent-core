"""Configuración de pack-subscriptions (ADR-0083).

Define cómo se cobra el período y los defaults del plan. La pasarela NO vive acá:
se consume vía `core/payments` (registry) si `pack_payments` está habilitado.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from core.mcp.types import BasePluginConfig
from pack_subscriptions.horario_legal import VENTANA_LEGAL_CO, dentro_de_lo_legal


class SubscriptionConfig(BasePluginConfig):
    cobro: str = Field(
        default="link",
        description="Cómo se cobra el período: 'link' (link de pago si hay pasarela) o 'manual'.",
    )
    moneda: str = Field(default="COP", description="Moneda por defecto de los planes.")
    periodicidad: Literal["mensual", "trimestral", "anual"] = Field(
        default="mensual",
        description="Periodicidad por defecto si el plan no la trae (mensual, trimestral o anual).",
    )
    trial_dias: int = Field(
        default=30,
        ge=0,
        le=365,
        description="Días de prueba por defecto del plan Free. Solo un plan de $0 tiene prueba (D-16).",
    )
    dias_gracia: int = Field(
        default=5,
        ge=0,
        le=60,
        description=(
            "Días después del vencimiento de un cargo impago antes de pasar la "
            "suscripción a 'vencida' (D-SPEC-06-3)."
        ),
    )
    # ---------------------------------------------------------------- avisos (HU-SUB-05)
    dias_anticipacion: int = Field(
        default=3,
        ge=0,
        le=30,
        description="Días antes del vencimiento en que se genera el cargo y se envía el link.",
    )
    recordatorios_dias: list[int] = Field(
        default_factory=lambda: [-3, 0, 3, 7],
        description=(
            "Días respecto al vencimiento en que se recuerda un cargo impago "
            "(negativo = antes, 0 = el día, positivo = después). Cada uno se envía una sola vez."
        ),
    )
    pausar_tras_dias_mora: int = Field(
        default=0,
        ge=0,
        le=180,
        description="Pausar la suscripción tras N días de mora (0 = no pausar nunca).",
    )
    aviso_cargo_texto: str = Field(
        default=(
            "Hola {cliente.nombre}, ya está tu cobro de {suscripcion.concepto} por {cargo.monto} "
            "(vence el {cargo.fecha_vencimiento}). Puedes pagarlo aquí: {cargo.link}"
        ),
        description="Texto del aviso de cargo nuevo cuando el cliente escribió en las últimas 24 h.",
    )
    recordatorio_texto: str = Field(
        default=(
            "Hola {cliente.nombre}, te recordamos el pago de {suscripcion.concepto} por "
            "{cargo.monto}, que vence {recordatorio.dias}. Link de pago: {cargo.link}"
        ),
        description="Texto de los recordatorios de un cargo impago (dentro de las 24 h).",
    )
    aviso_pausa_texto: str = Field(
        default=(
            "Hola {cliente.nombre}, pausamos tu suscripción a {suscripcion.concepto} porque el "
            "pago de {cargo.monto} lleva {mora.dias} días pendiente. Cuando pagues se reactiva: "
            "{cargo.link}"
        ),
        description="Texto del aviso de pausa por mora (dentro de las 24 h).",
    )
    # HU-SUB-24: cuando el cargo no tiene link (monto bajo el mínimo, sin pasarela o link caído) el aviso
    # lleva los datos de transferencia del negocio en lugar del link.
    aviso_cargo_transferencia_texto: str = Field(
        default=(
            "Hola {cliente.nombre}, ya está tu cobro de {suscripcion.concepto} por {cargo.monto} "
            "(vence el {cargo.fecha_vencimiento}). Puedes pagarlo por transferencia. "
            "{cargo.datos_transferencia} Indica en la transferencia: {suscripcion.concepto} "
            "{cargo.periodo}. Cuando pagues, avísanos por aquí."
        ),
        description="Aviso de cargo nuevo cuando no hay link de pago y el negocio tiene cuentas de transferencia.",
    )
    recordatorio_transferencia_texto: str = Field(
        default=(
            "Hola {cliente.nombre}, te recordamos el pago de {suscripcion.concepto} por "
            "{cargo.monto}, que vence {recordatorio.dias}. Puedes pagarlo por transferencia. "
            "{cargo.datos_transferencia} Indica en la transferencia: {suscripcion.concepto} "
            "{cargo.periodo}."
        ),
        description="Recordatorio de un cargo impago sin link de pago (con cuentas de transferencia).",
    )
    aviso_pausa_transferencia_texto: str = Field(
        default=(
            "Hola {cliente.nombre}, pausamos tu suscripción a {suscripcion.concepto} porque el "
            "pago de {cargo.monto} lleva {mora.dias} días pendiente. Cuando pagues se reactiva. "
            "{cargo.datos_transferencia} Indica en la transferencia: {suscripcion.concepto} "
            "{cargo.periodo}."
        ),
        description="Aviso de pausa por mora cuando el cargo no tiene link de pago.",
    )

    varias_por_cliente: bool = Field(
        default=False,
        description=(
            "Permitir varias suscripciones por cliente (p. ej. plan individual y familiar). "
            "Si está apagado, comprar otro plan pago se redirige al cambio de plan (HU-SUB-07)."
        ),
    )

    # ------------------------------------------------------ fin de la prueba (HU-SUB-06)
    dias_aviso_fin_prueba: int = Field(
        default=3,
        ge=0,
        le=30,
        description="Días antes del fin de la prueba de Free en que se ofrece un plan pago (0 = no avisar).",
    )
    aviso_fin_prueba_texto: str = Field(
        default=(
            "Hola {cliente.nombre}, tu prueba gratis de {suscripcion.concepto} termina el "
            "{prueba.termina}. Si quieres seguir, respóndenos y te ayudamos a elegir un plan."
        ),
        description="Texto del aviso de fin de la prueba (dentro de las 24 h).",
    )

    # ------------------------------------------------------ horario legal (HU-SUB-18)
    ventana_cobro: list[dict[str, Any]] = Field(
        default_factory=lambda: [dict(r) for r in VENTANA_LEGAL_CO],
        description=(
            "Días y horas en que pueden salir los avisos de cobro, en la zona horaria del negocio. "
            "Por defecto, el horario de la Ley 2300 de 2023: lunes a viernes de 7:00 a 19:00 y "
            "sábados de 8:00 a 15:00. Se puede achicar, nunca ampliar. Domingos y festivos, nunca."
        ),
    )
    pais_festivos: Literal["CO"] = Field(
        default="CO",
        description="País cuyos festivos bloquean los avisos de cobro.",
    )

    @field_validator("ventana_cobro")
    @classmethod
    def _ventana_legal(cls, valor: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rangos: list[dict[str, Any]] = []
        for rango in valor or []:
            try:
                dia = int(rango["day"])
                desde, hasta = str(rango["from"]).strip(), str(rango["to"]).strip()
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Cada rango lleva day (0 = lunes), from y to en HH:MM.") from exc
            if len(desde) != 5 or len(hasta) != 5 or desde[2] != ":" or hasta[2] != ":":
                raise ValueError("Las horas van en formato HH:MM.")
            rangos.append({"day": dia, "from": desde, "to": hasta})
        if not dentro_de_lo_legal(rangos):
            raise ValueError(
                "La ventana de cobro solo puede estar dentro del horario legal: lunes a viernes de "
                "07:00 a 19:00 y sábados de 08:00 a 15:00."
            )
        return rangos

    @field_validator("recordatorios_dias")
    @classmethod
    def _dias_validos(cls, valor: list[int]) -> list[int]:
        dias = sorted({int(d) for d in valor or []})
        if any(d < -30 or d > 90 for d in dias):
            raise ValueError("Los recordatorios van de -30 a 90 días respecto al vencimiento.")
        return dias