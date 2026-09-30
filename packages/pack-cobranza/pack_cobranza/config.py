"""Configuración de pack-cobranza (Pydantic -> formulario auto-generado en el panel).

Parámetros de operación del pack **por tenant**. Como el propietario ES el tenant
(ADR-0062), aquí viven también los datos de cobranza del propietario (país,
oficina, días). Las reglas finas de negociación viven en la tabla
`pack_cobranza_reglas_negociacion` (una fila por tenant).
"""

from __future__ import annotations

from pydantic import Field, SecretStr

from core.mcp.types import BasePluginConfig


class CobranzaConfig(BasePluginConfig):
    jwt_secret: SecretStr = Field(
        default=SecretStr(""), description="Secreto para firmar los tokens de la app de campo (gestores)"
    )
    nombre_oficina: str = Field(
        default="", description="Remitente que verán los clientes en las notificaciones"
    )
    propietario_whatsapp: str = Field(
        default="", description="WhatsApp del propietario para el enlace de contacto (wa.me)"
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
    # Cobro digital (ADR-0067). Con `pack_payments` activo se generan links/QR en
    # línea; sin él, la app muestra la cuenta informativa de abajo.
    pago_qr_activo: bool = Field(
        default=False, description="Habilitar el cobro con link/QR en la app de campo"
    )
    pago_titular: str = Field(default="", description="Titular de la cuenta (cobro informativo)")
    pago_banco: str = Field(default="", description="Banco para transferencia (cobro informativo)")
    pago_tipo_cuenta: str = Field(default="", description="Tipo de cuenta (ahorros/corriente)")
    pago_numero_cuenta: str = Field(default="", description="Número de cuenta (cobro informativo)")
    pago_alias: str = Field(
        default="", description="Alias/llave de billetera (Nequi, Daviplata) o llave de transferencia"
    )
    pago_qr_imagen_url: str = Field(
        default="", description="URL de una imagen QR del comercio (opcional, cobro informativo)"
    )
