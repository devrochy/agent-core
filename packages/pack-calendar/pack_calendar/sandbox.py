"""Simuladores de Agenda para la prueba del panel (HU-ARCH-14).

Las citas de la prueba viven en el sandbox de la conversación: no se crea
`pack_calendar_appointments`, no se escribe en Google Calendar y no se envía correo.
La disponibilidad y el catálogo de agendas/tipos se leen reales (solo lectura).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from core.mcp.sandbox import SandboxPrueba
from core.mcp.types import ToolContext


async def _sandbox(context: ToolContext) -> SandboxPrueba:
    if context.sandbox is None:
        raise RuntimeError("El simulador de agenda requiere sandbox de prueba")
    return context.sandbox


async def _citas(context: ToolContext) -> list[dict[str, Any]]:
    return await (await _sandbox(context)).listar("appointments")


async def simular_create_event(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    email = str(arguments.get("attendee_email") or "").strip()
    if not email:
        return {
            "error": "Falta el email del cliente: pedíselo antes de agendar (es obligatorio).",
            "status": "needs_data",
            "missing": ["attendee_email"],
        }
    from datetime import datetime

    start = str(arguments.get("start_datetime") or "")
    try:
        inicio = datetime.fromisoformat(start)
    except ValueError:
        return {"error": "La fecha/hora de la cita no es válida.", "status": "failed"}
    duracion = int(arguments.get("duration_minutes") or 30)
    fin = inicio + timedelta(minutes=duracion)
    event_id = str(uuid.uuid4())
    cita = {
        "event_id": event_id,
        "title": str(arguments.get("title") or "Cita"),
        "start": inicio.isoformat(),
        "end": fin.isoformat(),
        "attendee_email": email,
        "status": "programada",
    }
    await (await _sandbox(context)).guardar("appointments", event_id, cita)
    return {
        "event_id": event_id,
        "status": "created",
        "inicio": inicio.isoformat(),
        "fin": fin.isoformat(),
        "email_sent": False,
        "email_reason": "simulado",
        "_safe_append": f"📅 Tu cita quedó agendada para el {inicio.isoformat()} (simulada).",
    }


async def simular_update_event(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    event_id = str(arguments.get("event_id") or "")
    cita = await sandbox.leer("appointments", event_id) if event_id else None
    if cita is None:
        return {"status": "not_found", "message": "No encontré una cita simulada para reagendar."}
    cita["start"] = str(arguments.get("start_datetime") or cita["start"])
    cita["status"] = "reagendada"
    await sandbox.guardar("appointments", event_id, cita)
    return {
        "status": "updated",
        "event_id": event_id,
        "inicio": cita["start"],
        "email_sent": False,
        "notified": False,
        "_safe_append": f"📅 Tu cita quedó reagendada para el {cita['start']} (simulada).",
    }


async def simular_cancel_event(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    citas = await _citas(context)
    ids = [str(e) for e in (arguments.get("event_ids") or []) if str(e)]
    single = str(arguments.get("event_id") or "")
    if single:
        ids.append(single)
    cancel_all = bool(arguments.get("cancel_all"))
    canceladas: list[str] = []
    for cita in citas:
        if cancel_all or cita["event_id"] in ids:
            cita["status"] = "cancelada"
            await sandbox.guardar("appointments", cita["event_id"], cita)
            canceladas.append(cita["event_id"])
    if not canceladas:
        return {"status": "not_found", "message": "No encontré una cita simulada para cancelar."}
    return {
        "status": "cancelled",
        "cancelled": canceladas,
        "notified": False,
        "email_sent": False,
        "event_id": canceladas[0],
    }


async def simular_list_appointments(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    citas = [c for c in await _citas(context) if c.get("status") != "cancelada"]
    futuras = []
    for i, cita in enumerate(citas):
        futuras.append(
            {
                "index": i + 1,
                "event_id": cita["event_id"],
                "title": cita.get("title", "Cita"),
                "inicio": cita.get("start"),
                "ya_paso": False,
            }
        )
    return {"appointments": futuras, "total": len(futuras), "proximas": len(futuras)}


__all__ = [
    "simular_cancel_event",
    "simular_create_event",
    "simular_list_appointments",
    "simular_update_event",
]
