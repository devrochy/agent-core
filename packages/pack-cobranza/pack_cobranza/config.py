"""Configuración de pack-cobranza (Pydantic -> formulario auto-generado en el panel).

Parámetros de operación del pack **por tenant**. Como el propietario ES el tenant
(ADR-0062), aquí viven también los datos de cobranza del propietario (país,
oficina, días). Las reglas finas de negociación viven en la tabla
`pack_cobranza_reglas_negociacion` (una fila por tenant).
"""

from __future__ import annotations

from pydantic import Field

from core.mcp.types import BasePluginConfig


class CobranzaConfig(BasePluginConfig):
    nombre_oficina: str = Field(
        default="", description="Remitente que verán los clientes en las notificaciones"
    )
    moneda: str = Field(default="COP", description="Moneda ISO-4217 de la cartera")
    pais: str = Field(default="", description="País de operación (ISO-3166)")
    dias_tolerancia_cobro: int = Field(
        default=0, description="Días de tolerancia de pago del propietario"
    )
    dias_anticipacion_cobro: int = Field(
        default=0, description="Días de anticipación del cobro al propietario"
    )
    asistente_activo: bool = Field(
        default=True, description="Habilitar las respuestas automáticas por WhatsApp"
    )
    umbral_mora_dias: int = Field(
        default=5, description="Días de atraso a partir de los cuales el cliente es 'rojo'"
    )
    aviso_vencimiento_dias: int = Field(
        default=2, description="Días de anticipación del recordatorio de cuota"
    )
    escalar_siempre_a_humano: bool = Field(
        default=False, description="Desviar toda conversación a un gestor humano"
    )
