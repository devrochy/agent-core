"""Adapters de sync del core (ADR-0097): conversaciones y clientes/leads.

Los marcadores son las columnas que el core ya mantiene:
- Conversación: `COALESCE(closed_at, started_at)` — se crea al abrir y "cambia"
  al cerrar; los cambios de estado intermedios no alteran el cursor (deuda
  documentada: se resolverá con una columna `updated_at` en el core).
- Cliente: `created_at` (los clientes del core casi no mutan; la ficha rica de
  cobranza vive en el pack).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func

from core.db.models import Conversation, Customer
from core.sync import SyncAdapter, register_sync


def _marcador_conversacion(row: Conversation) -> datetime:
    return row.closed_at or row.started_at


def _serializar_conversacion(row: Conversation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "customerId": str(row.customer_id),
        "canal": row.channel,
        "estado": row.status,
        "botPausado": row.bot_paused,
        "iniciadaEn": row.started_at.isoformat(),
        "cerradaEn": row.closed_at.isoformat() if row.closed_at else None,
    }


def _serializar_cliente(row: Customer) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "nombre": row.name,
        "canal": row.channel,
        "externalId": row.external_id,
        "telefono": row.contact_phone,
        "creadoEn": row.created_at.isoformat(),
    }


register_sync(
    SyncAdapter(
        entidad="conversaciones",
        seccion="conversations",
        label="Conversaciones",
        model=Conversation,
        marcador_sql=func.coalesce(Conversation.closed_at, Conversation.started_at),
        marcador_row=_marcador_conversacion,
        serialize=_serializar_conversacion,
        tiene_deleted_at=True,
    )
)

register_sync(
    SyncAdapter(
        entidad="clientes",
        seccion="leads",
        label="Clientes",
        model=Customer,
        marcador_sql=Customer.created_at,
        marcador_row=lambda row: row.created_at,
        serialize=_serializar_cliente,
        tiene_deleted_at=True,
    )
)
