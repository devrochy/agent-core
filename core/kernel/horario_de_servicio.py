"""Horario de servicio del comercio como dato para el asistente (HU-AOS-58).

Con el modo de horario `informativo` (`business_hours_mode`) el horario configurado es el del equipo
humano del comercio, no un bloqueo: el asistente responde 24/7 (el ingreso no corta el turno, ver
`core/kernel/ingress.py`) y conoce ese horario para informarlo. Esta línea entra al bloque dinámico
del prompt; su texto vive en el playbook del kernel `horario-de-servicio` (invariante 3). Sin modo
`informativo`, o sin franjas, no hay bloque: el prompt queda igual que antes (replay intacto).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any

from core.kernel.fuera_de_horario import IDIOMA_POR_DEFECTO, IDIOMAS, idioma_del_negocio
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "horario-de-servicio"
MODO_INFORMATIVO = "informativo"
MODO_BLOQUEA = "bloquea"

_DIAS = {
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "pt": ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"),
}
_A = {"es": "a", "en": "to", "pt": "a"}
_Y = {"es": "y", "en": "and", "pt": "e"}
_DE = {"es": "de", "en": "from", "pt": "das"}
_HASTA = {"es": "a", "en": "to", "pt": "às"}


class PlaybookInvalido(ValueError):
    """El playbook no tiene el encabezado y una línea por idioma, o una plantilla no encaja."""


def modo_del_horario(escalation: Mapping[str, Any] | None) -> str:
    """`informativo` solo si está guardado así; todo lo demás es `bloquea` (lo de siempre)."""
    valor = (escalation or {}).get("business_hours_mode")
    return MODO_INFORMATIVO if valor == MODO_INFORMATIVO else MODO_BLOQUEA


def parsear_playbook(contenido: str) -> dict[str, str]:
    from core.kernel.procedures.textos import secciones

    crudas = secciones(contenido)
    claves = ["encabezado", *IDIOMAS]
    faltan = [c for c in claves if len(crudas.get(c, [])) != 1]
    if faltan:
        raise PlaybookInvalido(f"horario de servicio incompleto: {faltan}")
    textos = {c: crudas[c][0] for c in claves}
    for idioma in IDIOMAS:
        try:
            textos[idioma].format(horario="x")
        except (KeyError, IndexError, ValueError) as exc:
            raise PlaybookInvalido(f"la línea {idioma} usa un campo desconocido: {exc}") from exc
    return textos


@cache
def textos() -> Mapping[str, str]:
    """Fail-closed: un playbook roto lanza y lo cubre un test de CI."""
    from core.agents.playbooks import load_core_playbook

    return parsear_playbook(load_core_playbook(PLAYBOOK))


def _hhmm(valor: Any) -> str | None:
    hh, _, mm = str(valor or "").strip().partition(":")
    if not (hh.isdecimal() and mm.isdecimal() and hh.isascii() and mm.isascii()):
        return None
    return f"{int(hh):02d}:{int(mm):02d}"


def _franjas_por_dia(ranges: Any) -> dict[int, list[tuple[str, str]]]:
    por_dia: dict[int, list[tuple[str, str]]] = {}
    if not isinstance(ranges, list):
        return por_dia
    for rng in ranges:
        if not isinstance(rng, dict):
            continue
        bruto = rng.get("day")
        if bruto is None:
            continue
        try:
            dia = int(bruto)
        except (TypeError, ValueError):
            continue
        desde, hasta = _hhmm(rng.get("from")), _hhmm(rng.get("to"))
        if 0 <= dia <= 6 and desde and hasta:
            por_dia.setdefault(dia, []).append((desde, hasta))
    return {d: sorted(f) for d, f in sorted(por_dia.items())}


def _dias(dias: list[int], idioma: str) -> str:
    nombres = _DIAS[idioma]
    if len(dias) == 1:
        return nombres[dias[0]]
    if dias == list(range(dias[0], dias[-1] + 1)) and len(dias) >= 3:
        return f"{nombres[dias[0]]} {_A[idioma]} {nombres[dias[-1]]}"
    return ", ".join(nombres[d] for d in dias[:-1]) + f" {_Y[idioma]} {nombres[dias[-1]]}"


def resumen(business_hours: Mapping[str, Any] | None, idioma: str = IDIOMA_POR_DEFECTO) -> str:
    """«de lunes a viernes de 08:00 a 18:00; sábado de 09:00 a 13:00». "" si no hay franjas válidas.

    Los días con las mismas franjas se agrupan, así la línea del prompt es corta."""
    if not isinstance(business_hours, Mapping):
        return ""
    idioma = idioma if idioma in IDIOMAS else IDIOMA_POR_DEFECTO
    por_dia = _franjas_por_dia(business_hours.get("ranges"))
    if not por_dia:
        return ""
    grupos: list[tuple[list[int], list[tuple[str, str]]]] = []
    for dia, franjas in por_dia.items():
        igual = next((g for g in grupos if g[1] == franjas), None)
        if igual is not None:
            igual[0].append(dia)
        else:
            grupos.append(([dia], franjas))
    partes = []
    for dias, franjas in grupos:
        horas = f" {_Y[idioma]} ".join(
            f"{_DE[idioma]} {d} {_HASTA[idioma]} {h}" for d, h in franjas
        )
        partes.append(f"{_dias(dias, idioma)} {horas}")
    return "; ".join(partes)


def para_prompt(escalation: Mapping[str, Any] | None, language: str | None) -> str:
    """El bloque del prompt, o "" si el horario no es informativo o no tiene franjas."""
    if modo_del_horario(escalation) != MODO_INFORMATIVO:
        return ""
    idioma = idioma_del_negocio(language)
    horario = resumen((escalation or {}).get("business_hours"), idioma)
    if not horario:
        return ""
    t = textos()
    return f"{t['encabezado']}\n{t[idioma].format(horario=horario)}"
