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
