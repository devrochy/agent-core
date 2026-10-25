"""Configuración del CRM (el panel admin genera este formulario automáticamente)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from core.mcp.types import BasePluginConfig

_PREFIJO = re.compile(r"^[A-Za-z0-9]{1,8}$")
# `business_name` y `vertical` (HU-SH-03, ADR-0121): datos del alta del servicio que vende
# un plan de la plataforma; `vertical` es un id de preset validado, no texto libre.
_CAMPOS_VALIDOS = {
    "full_name", "id_document", "address", "municipality", "city", "phone", "email",
    "business_name", "vertical",
}


class CRMConfig(BasePluginConfig):
    pipeline_name: str = Field(
        default="Ventas", description="Nombre del pipeline de ventas"
    )
    auto_create_lead: bool = Field(
        default=True,
        description="Crear un lead automáticamente cuando inicia una conversación nueva",
    )
    follow_up_hours: int = Field(
        default=24, ge=0, le=8760, description="Horas sin actividad antes de marcar un lead para seguimiento"
    )
    scoring_mode: Literal["manual", "auto"] = Field(
        default="auto", description="Cómo se puntúan los leads (auto = el agente pregunta y califica)"
    )

    # ---------------------------------------------------------------- segmentación de clientes
    # Conteos y días: al menos 1 (422 si no; P0 de pruebas 2026-10-17).
    customer_frecuente_min_pedidos: int = Field(
        default=2, ge=1, le=10_000, description="Mínimo de pedidos para considerarse 'frecuente'"
    )
    customer_frecuente_intervalo_max_dias: int = Field(
        default=45, ge=1, le=3650, description="Días máximos entre pedidos para mantenerse 'frecuente'"
    )
    customer_fiel_min_pedidos: int = Field(
        default=5, ge=1, le=10_000, description="Mínimo de pedidos para considerarse 'fiel'"
    )
    customer_inactivo_dias_sin_comprar: int = Field(
        default=120, ge=1, le=3650, description="Días sin comprar para marcar al cliente como 'inactivo'"
    )

    # ---------------------------------------------------------------- montos y pagos
    monto_alto_threshold: float = Field(
        default=1_000_000, ge=0, le=9_999_999_999, allow_inf_nan=False, description="Monto en COP que dispara escalamiento a humano"
    )
    # Umbral propio para planes recurrentes (HU-COM-02). `None` = un plan nunca
    # escala por monto alto: el cobro ya pasa por el snapshot del servidor
    # (HU-COM-01) y el negocio conoce su catálogo. Así se puede vender el plan
    # Empresarial (1,8M) sin fricción.
    monto_alto_planes_threshold: float | None = Field(
        default=None,
        ge=0,
        le=9_999_999_999,
        allow_inf_nan=False,
        description="Monto en COP que dispara escalamiento para planes recurrentes; None = nunca",
    )
    # Gate determinístico en crear_pedido (incidente real 2026-09-11, tenant yopoldo): un
    # pedido con pago_en_linea por debajo del mínimo de la pasarela nunca puede completarse, y
    # descubrirlo al generar el link es tarde para redirigir la conversación con naturalidad.
    # El mínimo lo fija la pasarela y depende del ambiente (Wompi: 150.000 COP en sandbox, 1.500
    # en producción), así que por defecto se le pregunta a la capacidad de pagos del core
    # (`core.payments.monto_minimo`); pack_commerce no importa pack_payments (invariante 5).
    # Un valor explícito manda sobre el de la pasarela (política propia del negocio).
    pago_en_linea_monto_minimo: float | None = Field(
        default=None,
        ge=0,
        le=9_999_999_999,
        allow_inf_nan=False,
        description=(
            "Monto mínimo en COP para aceptar pago_en_linea al crear un pedido. Vacío = el de la "
            "pasarela según su ambiente (sandbox o producción); un valor lo reemplaza."
        ),
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

    # ---------------------------------------------------------------- numeración y checkout (HU-COM-03)
    order_prefix: str = Field(
        default="OC",
        description="Prefijo del número público de pedido de este negocio (1-8 letras/números).",
    )
    checkout_fields: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "domicilio": ["full_name", "id_document", "address", "municipality", "city", "phone"],
            "recoger_en_tienda": ["full_name", "id_document", "phone"],
            "plan": ["full_name", "id_document", "phone", "email"],
        },
        description=(
            "Campos que pide crear_pedido por tipo de entrega o producto "
            "(claves: full_name, id_document, address, municipality, city, phone, email, "
            "business_name, vertical). Los planes y el retiro en tienda no piden dirección. "
            "business_name y vertical (tipo de negocio) sirven a quien vende planes de la "
            "plataforma: el alta automática crea el negocio con ellos."
        ),
    )

    @field_validator("order_prefix", mode="before")
    @classmethod
    def _normalizar_prefijo(cls, valor: object) -> str:
        texto = str(valor or "").strip().upper()
        if not _PREFIJO.match(texto):
            return "OC"
        return texto

    @field_validator("checkout_fields")
    @classmethod
    def _campos_conocidos(cls, valor: dict[str, list[str]]) -> dict[str, list[str]]:
        for campos in (valor or {}).values():
            desconocidos = [c for c in campos if c not in _CAMPOS_VALIDOS]
            if desconocidos:
                # Mensaje fijo (sin el valor recibido): termina en `error.fields` de la respuesta.
                raise ValueError(
                    "checkout_fields tiene campos desconocidos; los válidos son: "
                    + ", ".join(sorted(_CAMPOS_VALIDOS))
                )
        return valor

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

    # ---------------------------------------------------------------- crm_completion_check (ADR-0025)
    completion_claims_disabled: str = Field(
        default="",
        description=(
            "Nombres de claims incorporados a desactivar, separados por coma "
            "(pedido, link_de_pago, asesor_humano, dato_de_contacto_guardado). "
            "Vacío = todos activos."
        ),
    )
    completion_claims_custom: str = Field(
        default="",
        max_length=4000,
        json_schema_extra={"format": "textarea"},
        description=(
            "Claims propios de este negocio para crm_completion_check, uno por "
            "línea (máx. 4000 caracteres en total -- este guardrail corre en "
            "CADA turno de conversación). "
            "`etiqueta: frase uno | frase dos => tool_uno, tool_dos`. "
            "Las frases NUNCA se interpretan como regex (se escapan). Si el "
            "nombre coincide con un claim incorporado, lo reemplaza."
        ),
    )

    # ---------------------------------------------------------------- intake de leads (ADR-0055 superseded)
    lead_intake_enabled: bool = Field(
        default=False,
        description=(
            "Aceptar leads desde landings externas por la API pública del pack "
            "(/api/pack/pack_commerce/leads/<slug>). Desactivado por defecto."
        ),
    )
    lead_intake_secret: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Secreto compartido del intake. La landing lo envía en el header "
            "X-Intake-Secret. Vacío = intake sin configurar (503)."
        ),
    )
    lead_intake_allowed_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Orígenes permitidos para el formulario alojado (iframe). Vacío = sin "
            "restricción de origen."
        ),
    )
    lead_intake_turnstile: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Secret key del widget Cloudflare Turnstile de ESTE negocio. Si está "
            "definido, el intake exige y valida el token (siteverify). Vacío = sin "
            "Turnstile."
        ),
    )
    lead_intake_default_status: str = Field(
        default="nuevo", description="Estado inicial del lead capturado por intake"
    )
    lead_intake_default_interest: str = Field(
        default="", description="Interés por defecto si el formulario no lo envía"
    )
    lead_intake_source: str = Field(
        default="landing", description="Origen (source) guardado en el lead del intake"
    )