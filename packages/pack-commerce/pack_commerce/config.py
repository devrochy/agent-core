"""Configuración del CRM (el panel admin genera este formulario automáticamente)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from core.mcp.types import BasePluginConfig


class CRMConfig(BasePluginConfig):
    pipeline_name: str = Field(
        default="Ventas", description="Nombre del pipeline de ventas"
    )
    auto_create_lead: bool = Field(
        default=True,
        description="Crear un lead automáticamente cuando inicia una conversación nueva",
    )
    follow_up_hours: int = Field(
        default=24, description="Horas sin actividad antes de marcar un lead para seguimiento"
    )
    scoring_mode: Literal["manual", "auto"] = Field(
        default="auto", description="Cómo se puntúan los leads (auto = el agente pregunta y califica)"
    )

    # ---------------------------------------------------------------- segmentación de clientes
    customer_frecuente_min_pedidos: int = Field(
        default=2, description="Mínimo de pedidos para considerarse 'frecuente'"
    )
    customer_frecuente_intervalo_max_dias: int = Field(
        default=45, description="Días máximos entre pedidos para mantenerse 'frecuente'"
    )
    customer_fiel_min_pedidos: int = Field(
        default=5, description="Mínimo de pedidos para considerarse 'fiel'"
    )
    customer_inactivo_dias_sin_comprar: int = Field(
        default=120, description="Días sin comprar para marcar al cliente como 'inactivo'"
    )

    # ---------------------------------------------------------------- montos y pagos
    monto_alto_threshold: float = Field(
        default=1_000_000, description="Monto en COP que dispara escalamiento a humano"
    )
    # Gate determinístico en crear_pedido (incidente real 2026-09-11, tenant
    # yopoldo): un pedido con pago_en_linea por debajo del mínimo real de la
    # pasarela (Wompi: $150.000 COP, ver pack_payments/providers/wompi.py::
    # MIN_AMOUNT_COP) nunca puede completarse -- pack_commerce no puede
    # importar esa constante (invariante 5, ningún pack importa de otro), así
    # que este valor es la política propia de pack_commerce, con un default
    # que hoy coincide con el mínimo real. Antes de esto, `crear_pedido`
    # dejaba pasar cualquier monto con pago_en_linea y el rechazo solo
    # ocurría (si el modelo llegaba a llamarla) en generar_link_pago, ya
    # tarde para redirigir la conversación con naturalidad.
    pago_en_linea_monto_minimo: float = Field(
        default=150_000,
        description="Monto mínimo en COP para aceptar pago_en_linea al crear un pedido",
    )
    # Cuentas de transferencia visibles al cliente (nunca pasan por el LLM).
    # Formato: [{"entity","accountType","accountNumber","holderName","holderDocument"}]
    transfer_accounts: list[dict] = Field(
        default_factory=list,
        description="Cuentas para transferencia (Nequi, Bancolombia...). Se envían por _safe_append.",
    )

    # ---------------------------------------------------------------- moneda
    currency_symbol: str = Field(default="$", description="Símbolo de moneda para precios")
    currency_code: str = Field(default="COP", description="Código de moneda (COP, USD)")

    # ---------------------------------------------------------------- copy (ADR-0017: CONFIG editable)
    follow_up_lead_message: str = Field(
        default=(
            "¡Hola{name}! 👋 Quedamos pendientes de tu interés en {interest}. "
            "¿Sigues interesado? Estoy aquí para ayudarte con lo que necesites."
        ),
        description="Mensaje de reenganche de leads ({name}, {interest})",
    )
    follow_up_quote_message: str = Field(
        default=(
            "¡Hola! Te tengo lista tu cotización por {total}. ¿La confirmo para armar tu pedido? 😋"
        ),
        description="Mensaje de reenganche de cotizaciones ({total})",
    )
    order_status_visible: dict[str, str] = Field(
        default_factory=lambda: {
            "abierto": "en preparación",
            "despachado": "en camino",
            "entregado": "entregado",
            "cancelado": "cancelado",
            "expirado": "vencido",
        },
        description="Etiquetas de estado visibles al cliente",
    )
    order_status_label: dict[str, str] = Field(
        default_factory=lambda: {
            "abierto": "Abierto",
            "despachado": "Despachado",
            "entregado": "Entregado",
            "cancelado": "Cancelado",
            "expirado": "Expirado",
        },
        description="Etiquetas de estado en el panel",
    )
    order_confirmed_message: str = Field(
        default="✅ Tu pedido {public_order_number} quedó confirmado. Total a pagar: {total}.",
        description="Mensaje de confirmación de pedido (efectivo)",
    )
    order_confirmed_transfer_message: str = Field(
        default=(
            "Tu pedido {public_order_number} quedó confirmado por transferencia.\n\n{transfer_block}"
        ),
        description="Mensaje de confirmación de pedido por transferencia",
    )