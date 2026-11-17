"""Horario propio de un agente (SPEC-47, HU-AOS-82, D-SPEC-47-5).

El horario del negocio (`escalation_config.business_hours`: `{"tz", "ranges": [{"day", "from",
"to"}]}`) es el de todos sus agentes. Un agente puede fijar el suyo (`agent_drafts.schedule`,
congelado en su versión y en la release): mismo formato, más la zona horaria. Reglas:

- `None` (o un horario guardado vacío o inválido) = hereda el del negocio: nunca falla el turno.
- El override solo cambia el horario: el modo (`business_hours_mode`, bloquea o informativo) y el
  aviso de fuera de horario siguen siendo del negocio.
- Los crons del negocio no lo miran (es conversacional).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_FRANJAS = 21


class HorarioInvalido(ValueError):
    """El horario no se puede guardar; el mensaje es para el dueño."""


def normalizar(crudo: Any) -> dict[str, Any] | None:
    """El horario a guardar, o `None` (heredar). Lanza `HorarioInvalido` con un mensaje claro."""
    if crudo is None:
        return None
    if not isinstance(crudo, Mapping):
        raise HorarioInvalido("El horario tiene que ser un objeto con zona horaria y franjas.")
    tz = str(crudo.get("tz") or "").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HorarioInvalido("La zona horaria no es válida.") from None
    franjas = crudo.get("ranges")
    if not isinstance(franjas, list) or not franjas:
        raise HorarioInvalido("Agrega al menos una franja (día y horas) o vuelve al horario del negocio.")
    if len(franjas) > MAX_FRANJAS:
        raise HorarioInvalido(f"Puedes tener hasta {MAX_FRANJAS} franjas.")
    salida: list[dict[str, Any]] = []
    for i, f in enumerate(franjas):
        if not isinstance(f, Mapping):
            raise HorarioInvalido(f"La franja {i + 1} no es válida.")
        dia, desde, hasta = f.get("day"), str(f.get("from") or ""), str(f.get("to") or "")
        if not isinstance(dia, int) or isinstance(dia, bool) or not 0 <= dia <= 6:
            raise HorarioInvalido(f"La franja {i + 1} tiene un día inválido.")
        if not _HORA.match(desde) or not _HORA.match(hasta) or desde >= hasta:
            raise HorarioInvalido(f"La franja {i + 1} necesita una hora de inicio menor que la de fin (HH:MM).")
        salida.append({"day": dia, "from": desde, "to": hasta})
    salida.sort(key=lambda r: (r["day"], r["from"]))
    return {"tz": tz, "ranges": salida}


def huella(horario: Mapping[str, Any] | None) -> str | None:
    if horario is None:
        return None
    return hashlib.sha256(json.dumps(horario, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def efectivo(propio: Any, del_negocio: Any) -> Any:
    """El horario que vale para el agente: el suyo si es válido; si no, el del negocio (fallback)."""
    try:
        normalizado = normalizar(propio)
    except HorarioInvalido:
        normalizado = None
    return normalizado if normalizado is not None else del_negocio


def con_horario_del_agente(escalation: Mapping[str, Any] | None, propio: Any) -> dict[str, Any]:
    """`escalation_config` con `business_hours` reemplazado por el horario efectivo del agente."""
    escalation_cfg = dict(escalation or {})
    horario = efectivo(propio, escalation_cfg.get("business_hours"))
    if horario is not None:
        escalation_cfg["business_hours"] = horario
    return escalation_cfg
