"""Configuración de pack-subscriptions (ADR-0083).

Define cómo se cobra el período y los defaults del plan. La pasarela NO vive acá:
se consume vía `core/payments` (registry) si `pack_payments` está habilitado.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from core.mcp.types import BasePluginConfig


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
    trial_dias: int = Field(default=30, description="Días de prueba por defecto de un plan.")
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

    @field_validator("recordatorios_dias")
    @classmethod
    def _dias_validos(cls, valor: list[int]) -> list[int]:
        dias = sorted({int(d) for d in valor or []})
        if any(d < -30 or d > 90 for d in dias):
            raise ValueError("Los recordatorios van de -30 a 90 días respecto al vencimiento.")
        return dias