"""Simuladores de las tools del core para la prueba del panel (HU-ARCH-14).

Correo, plantillas y escalamiento no deben salir en una prueba. Memoria, perfil y notas
sí pueden escribir: viven en el cliente sintético `channel="test"`, que no cuenta y se
purga al cerrar la prueba (decisión del dueño, 2026-11-14).
"""

from __future__ import annotations

from typing import Any

from core.mcp.types import ToolContext


async def simular_escalar(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {
        "status": "escalated",
        "bot_paused": False,
        "simulado": True,
        "message": "Escalamiento simulado en la prueba del panel; no se pausó el bot real.",
    }


async def simular_email(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {
        "status": "simulado",
        "to": str(arguments.get("to") or ""),
        "message": "Correo simulado en la prueba del panel; no salió ningún mensaje.",
    }


async def simular_plantilla_opciones(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {
        "status": "simulado",
        "clave": str(arguments.get("clave") or ""),
        "message": "Plantilla de opciones simulada en la prueba del panel; no se envió.",
    }


__all__ = ["simular_email", "simular_escalar", "simular_plantilla_opciones"]
