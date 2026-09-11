"""Configuración de pack-payments (Pydantic -> formulario auto-generado en el admin).

Cada tenant guarda sus PROPIAS credenciales de Wompi (cifradas con Fernet,
ver docs/PLUGIN-CONTRACT.md § 6) — un tenant con este pack activo cobra a
sus propios clientes con su propia cuenta de comercio, nunca con una
credencial compartida.
"""

from __future__ import annotations

from pydantic import Field, SecretStr

from core.mcp.types import BasePluginConfig


class PaymentsConfig(BasePluginConfig):
    public_key: str = Field(..., description="Llave pública de Wompi (pub_prod_... / pub_test_...)")
    private_key: SecretStr = Field(..., description="Llave privada de Wompi (prv_prod_... / prv_test_...)")
    events_secret: SecretStr = Field(..., description="Secreto de eventos de Wompi (para validar webhooks)")
    sandbox: bool = Field(default=True, description="Usar el ambiente de pruebas de Wompi")
    currency: str = Field(default="COP", description="Moneda ISO-4217 de los cobros")
    link_expira_horas: int = Field(default=24, description="Horas de vigencia del link de pago generado")
