"""Motivos de escalación legibles (HU-UX-17, SPEC-31).

`Escalation.reason` es un texto que arma quien escala: la tool `escalate_to_human`
(`tool:<código>`, y antes `tool:<texto libre>`), los plugins (`plugin:<tool o razón>`) y el
sistema (`keyword`, `llm_error`, `guardrail`, `send_failed`…). Este módulo lo normaliza a un
código cerrado (`MotivoCodigo`) y a una etiqueta en español para el panel, y traduce el
código a un predicado SQL para filtrar la lista sin traer todas las filas.

Es puro salvo el resolutor de habilidades, que se inyecta (`resolver_habilidad`) para que
los tests no dependan del host de tools.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, get_args

from sqlalchemy import ColumnElement, not_, or_

from core.db.models import Escalation

# Códigos que el modelo elige al llamar `escalate_to_human` (catálogo cerrado, como en
# agent-sale). `otro` va con un `detalle` libre aparte.
MotivoTool = Literal[
    "solicitud_cliente", "queja", "fuera_de_alcance", "intentos_fallidos", "monto_alto", "otro"
]
MOTIVOS_TOOL: tuple[str, ...] = get_args(MotivoTool)

MotivoCodigo = Literal[
    # Elegidos por el modelo (tool `escalate_to_human`).
    "solicitud_cliente",
    "queja",
    "fuera_de_alcance",
    "intentos_fallidos",
    "monto_alto",
    # Generados por el sistema.
    "palabra_clave",
    "accion_habilidad",
    "regla_negocio",
    "verificacion",
    "frase_prohibida",
    "falla_ia",
    "rechazo_ia",
    "envio_fallido",
    "sin_resolver",
    "limite_iteraciones",
    # Lo que no encaja en ninguno (incluye el texto libre de antes de HU-UX-17).
    "otro",
]
MOTIVOS: tuple[str, ...] = get_args(MotivoCodigo)

ETIQUETAS: dict[str, str] = {
    "solicitud_cliente": "Solicitud del cliente",
    "queja": "Queja",
    "fuera_de_alcance": "Fuera de alcance",
    "intentos_fallidos": "Intentos fallidos",
    "monto_alto": "Monto alto",
    "palabra_clave": "Pidió hablar con una persona",
    "accion_habilidad": "Acción de una habilidad",
    "regla_negocio": "Regla del negocio",
    "verificacion": "Respuesta no verificada",
    "frase_prohibida": "Frase prohibida por la personalidad",
    "falla_ia": "Falla del asistente de IA",
    "rechazo_ia": "El asistente no pudo responder",
    "envio_fallido": "No se pudo enviar el mensaje",
    "sin_resolver": "Varios turnos sin resolver",
    "limite_iteraciones": "El asistente no llegó a una respuesta",
    "otro": "Otro",
}

# Motivos que el sistema guarda tal cual en `reason`.
_EXACTOS: dict[str, str] = {
    "keyword": "palabra_clave",
    "llm_error": "falla_ia",
    "llm_refusal": "rechazo_ia",
    "persona_forbidden_phrase": "frase_prohibida",
    "send_failed": "envio_fallido",
    "turns_unresolved": "sin_resolver",
    "max_iterations": "limite_iteraciones",
}

# Prefijos que guardan los guardrails si su `reason` llega crudo a la escalación
# (`guardrails.py`): `regla:<nombre>` y `<guardrail>: <motivo>`.
_PREFIJO_REGLA = "regla:"
_PREFIJO_TOOL = "tool:"
_PREFIJO_PLUGIN = "plugin:"


def motivo_de_tool(reason: str | None) -> tuple[str, str]:
    """`(código, detalle)` para lo que mandó el modelo en `escalate_to_human`.

    Un `reason` fuera del catálogo (un modelo o una grabación vieja con texto libre) no se
    rechaza: queda como `otro` y el texto pasa a ser el detalle."""
    texto = (reason or "").strip()
    codigo = texto.lower()
    if codigo in MOTIVOS_TOOL:
        return codigo, ""
    return "otro", texto


def codigo(reason: str, summary: str = "") -> str:
    """Código cerrado del motivo guardado en `Escalation.reason`."""
    if reason in _EXACTOS:
        return _EXACTOS[reason]
    if reason.startswith(_PREFIJO_TOOL):
        texto = reason[len(_PREFIJO_TOOL):]
        return texto if texto in MOTIVOS_TOOL and texto != "otro" else "otro"
    if reason.startswith(_PREFIJO_PLUGIN):
        return "accion_habilidad"
    if reason.startswith(_PREFIJO_REGLA):
        return "regla_negocio"
    if reason == "guardrail":
        # El loop guarda el id del guardrail en `summary` (`regla` para las reglas del negocio).
        return "regla_negocio" if summary == "regla" else "verificacion"
    if ": " in reason:
        return "verificacion"
    return "otro"


def _humanizar(nombre: str) -> str:
    texto = nombre.replace("_", " ").replace(".", " ").strip()
    return texto[:1].upper() + texto[1:] if texto else ""


def etiqueta(
    reason: str,
    summary: str = "",
    resolver_habilidad: Callable[[str], str | None] | None = None,
) -> str:
    """Etiqueta en español. Para `plugin:<x>`, «Acción de <habilidad>» con el nombre visible
    del plugin que registró la tool `x`; si no se resuelve, el nombre `x` legible."""
    cod = codigo(reason, summary)
    if cod == "accion_habilidad":
        nombre = reason[len(_PREFIJO_PLUGIN):].strip()
        habilidad = (resolver_habilidad(nombre) if resolver_habilidad and nombre else None) or _humanizar(nombre)
        return f"Acción de {habilidad}" if habilidad else ETIQUETAS[cod]
    if cod == "regla_negocio" and reason.startswith(_PREFIJO_REGLA):
        nombre = _humanizar(reason[len(_PREFIJO_REGLA):])
        return f"{ETIQUETAS[cod]}: {nombre}" if nombre else ETIQUETAS[cod]
    return ETIQUETAS[cod]


def condicion_sql(cod: str) -> ColumnElement[bool]:
    """Predicado sobre `Escalation.reason` equivalente a `codigo(...) == cod`.

    `otro` es la negación de todos los demás, así que cualquier formato nuevo que no se
    clasifique cae ahí también en el filtro (como en `codigo`)."""
    if cod == "otro":
        return not_(or_(*(_condicion(c) for c in MOTIVOS if c != "otro")))
    return _condicion(cod)


def _condicion(cod: str) -> ColumnElement[bool]:
    columna, summary = Escalation.reason, Escalation.summary
    exactos = [r for r, c in _EXACTOS.items() if c == cod]
    partes: list[ColumnElement[bool]] = [columna.in_(exactos)] if exactos else []
    if cod in MOTIVOS_TOOL:
        partes.append(columna == f"{_PREFIJO_TOOL}{cod}")
    elif cod == "accion_habilidad":
        partes.append(columna.startswith(_PREFIJO_PLUGIN, autoescape=True))
    elif cod == "regla_negocio":
        partes.append(columna.startswith(_PREFIJO_REGLA, autoescape=True))
        partes.append((columna == "guardrail") & (summary == "regla"))
    elif cod == "verificacion":
        partes.append((columna == "guardrail") & (summary != "regla"))
        partes.append(
            columna.contains(": ", autoescape=True)
            & not_(columna.startswith(_PREFIJO_TOOL, autoescape=True))
            & not_(columna.startswith(_PREFIJO_PLUGIN, autoescape=True))
            & not_(columna.startswith(_PREFIJO_REGLA, autoescape=True))
        )
    return or_(*partes)
