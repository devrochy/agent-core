"""Horas, fechas y preguntas del borrador para el verificador (HU-AOS-20 parte 3, adenda de ADR-0116). Puro.

- **Horas:** «15:00», «3:30», «3 p. m.», «3pm», «a las 3», «a las 3 y media», «3 de la tarde» o «de la
  noche» (+12), «15h», «15h30» y «mediodía». Un número suelto solo es hora con el ancla «a las» / «las»
  («2 horas», «24 horas» o una cantidad no lo son). Sin «a. m./p. m.» y menor de 13, la hora vale `h` o
  `h+12`: «a las 3» la respaldan 03:00 y 15:00.
- **Fechas:** «30 de octubre [de 2026]», «el jueves 30», «el jueves» (la próxima), ISO, «hoy», «mañana»
  (no «en la mañana») y «pasado mañana», con el reloj y la zona del negocio. Un día de la semana que no
  coincide con la fecha deja la mención «sin normalizar»: nunca bloquea.
- **Respaldo:** de los resultados de las acciones, solo las claves que ofrecen horarios (`free_slots`,
  `hora`, `start`, `date`, `fecha`, `days`, el texto de `_safe_append`); nunca `busy_slots` (horas
  ocupadas), `business_hours` ni el texto de un error.
- **Preguntas:** cada `?` (una racha cuenta una vez) y cada `¿` sin cierre.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ZONA_POR_DEFECTO = "America/Bogota"
_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_DIAS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
_MES = "|".join(_MESES)
_DIA = "|".join(_DIAS)

# El texto se compara sin tildes y en minúsculas; las posiciones no cambian (cada carácter sigue
# ocupando uno) salvo en la normalización de «a. m./p. m.», que se hace antes de buscar.
_MERIDIANO = re.compile(r"\b([ap])\s?\.?\s?m\s?\.?(?=\W|$)")
_HORA_MERIDIANO = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])m\b")
_HORA_DOS_PUNTOS = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_HORA_H = re.compile(r"\b(\d{1,2})h(\d{2})?\b")
_HORA_ANCLA = re.compile(
    r"\b(?:a\s+la|a\s+las|la|las)\s+(\d{1,2})(?::(\d{2}))?(?:\s+y\s+(media|cuarto))?(?:\s*([ap])m)?"
    r"(?:\s+de\s+la\s+(manana|tarde|noche))?\b"
    r"(?!\s+(?:horas?|minutos?|dias?|semanas?|meses|personas?|veces|unidades)\b)"
)
_HORA_PARTE = re.compile(r"\b(\d{1,2})\s+de\s+la\s+(manana|tarde|noche)\b")
_MEDIODIA = re.compile(r"\bmediodia\b")
_FECHA_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_FECHA_LARGA = re.compile(rf"\b(?:({_DIA})\s+)?(\d{{1,2}})\s+de\s+({_MES})(?:\s+(?:de|del)\s+(\d{{4}}))?\b")
_FECHA_DIA_NUM = re.compile(rf"\b({_DIA})\s+(\d{{1,2}})\b(?!\s+de\s+(?:{_MES}))")
_FECHA_DIA = re.compile(rf"\b({_DIA})\b(?!\s+\d)")
_RELATIVA = re.compile(r"\b(pasado\s+manana|hoy|manana)\b")
_ANTES_DE_MANANA = re.compile(r"(?:la|de|en|por|esta)\s+$")
_PREGUNTA_CERRADA = re.compile(r"¿[^¿?]*\?")


def _plano(texto: str) -> str:
    """Minúsculas sin tildes, del mismo largo (ñ → n)."""
    salida = []
    for c in (texto or "").lower():
        base = unicodedata.normalize("NFD", c)[0]
        salida.append(base if base.isascii() else c)
    return "".join(salida)


def normalizar(texto: str) -> str:
    """El texto plano con «a. m.» / «p. m.» / «a.m.» / «pm» como «am» / «pm» (no rompen la oración)."""
    return _MERIDIANO.sub(lambda m: f"{m.group(1)}m", _plano(texto))


def _hora(h: int, m: int) -> str | None:
    return f"{h:02d}:{m:02d}" if 0 <= h <= 23 and 0 <= m <= 59 else None


def _candidatas(h: int, m: int, *, meridiano: str | None = None, parte: str | None = None) -> frozenset[str]:
    if meridiano == "a":
        valores = [h % 12]
    elif meridiano == "p":
        valores = [h % 12 + 12]
    elif parte in ("tarde", "noche"):
        valores = [h + 12 if h < 12 else h]
    elif parte == "manana":
        valores = [h]
    elif h == 12 or h >= 13:
        valores = [h]
    else:
        valores = [h, h + 12]
    return frozenset(x for v in valores if (x := _hora(v, m)) is not None)


@dataclass(frozen=True)
class Mencion:
    inicio: int
    fin: int
    horas: frozenset[str] = frozenset()
    fechas: frozenset[date] = frozenset()
    # La fecha no se pudo fijar (día de la semana que no coincide, fecha imposible): solo sombra.
    sin_normalizar: bool = False
    # Fecha con día del mes (larga, ISO, «jueves 30»): se exige sola. «El jueves», «mañana» u «hoy»
    # solo dan contexto a una hora de la misma oración.
    explicita: bool = False


def _sin_solapes(menciones: Iterable[Mencion]) -> list[Mencion]:
    """Las menciones sin solaparse: gana la más larga (y la primera)."""
    elegidas: list[Mencion] = []
    for m in sorted(menciones, key=lambda x: (-(x.fin - x.inicio), x.inicio)):
        if all(m.fin <= e.inicio or m.inicio >= e.fin for e in elegidas):
            elegidas.append(m)
    return sorted(elegidas, key=lambda x: x.inicio)


def horas_en(plano: str) -> list[Mencion]:
    """Las horas de un texto ya `normalizar`."""
    halladas: list[Mencion] = []
    for m in _HORA_MERIDIANO.finditer(plano):
        halladas.append(Mencion(m.start(), m.end(), _candidatas(int(m.group(1)), int(m.group(2) or 0), meridiano=m.group(3))))
    for m in _HORA_DOS_PUNTOS.finditer(plano):
        halladas.append(Mencion(m.start(), m.end(), _candidatas(int(m.group(1)), int(m.group(2)))))
    for m in _HORA_H.finditer(plano):
        halladas.append(Mencion(m.start(), m.end(), _candidatas(int(m.group(1)), int(m.group(2) or 0))))
    for m in _HORA_ANCLA.finditer(plano):
        minutos = int(m.group(2)) if m.group(2) else {"media": 30, "cuarto": 15}.get(m.group(3) or "", 0)
        halladas.append(
            Mencion(m.start(), m.end(), _candidatas(int(m.group(1)), minutos, meridiano=m.group(4), parte=m.group(5)))
        )
    for m in _HORA_PARTE.finditer(plano):
        halladas.append(Mencion(m.start(), m.end(), _candidatas(int(m.group(1)), 0, parte=m.group(2))))
    for m in _MEDIODIA.finditer(plano):
        halladas.append(Mencion(m.start(), m.end(), frozenset({"12:00"})))
    return [h for h in _sin_solapes(halladas) if h.horas]


def _fecha(anio: int, mes: int, dia: int) -> date | None:
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def fechas_en(plano: str, hoy: date) -> list[Mencion]:
    """Las fechas de un texto ya `normalizar`, con `hoy` en la zona del negocio."""
    halladas: list[Mencion] = []
    for m in _FECHA_ISO.finditer(plano):
        f = _fecha(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        halladas.append(
            Mencion(m.start(), m.end(), fechas=frozenset({f}) if f else frozenset(), sin_normalizar=f is None, explicita=True)
        )
    for m in _FECHA_LARGA.finditer(plano):
        dia, mes = int(m.group(2)), _MESES[m.group(3)]
        anios = [int(m.group(4))] if m.group(4) else [hoy.year, hoy.year + 1]
        # Sin año: la próxima (este año o el siguiente, si ya pasó hace más de una semana).
        f = next(
            (x for a in anios if (x := _fecha(a, mes, dia)) and (m.group(4) or x >= hoy - timedelta(days=7))), None
        )
        ok = f is not None and (m.group(1) is None or f.weekday() == _DIAS[m.group(1)])
        halladas.append(
            Mencion(m.start(), m.end(), fechas=frozenset({f}) if ok and f else frozenset(), sin_normalizar=not ok, explicita=True)
        )
    for m in _FECHA_DIA_NUM.finditer(plano):
        semana, dia = _DIAS[m.group(1)], int(m.group(2))
        # «el jueves 30»: este mes o el siguiente, el primero que cae en ese día de la semana.
        f = next(
            (
                x
                for desfase in (0, 1, 2)
                if (x := _fecha(hoy.year + (hoy.month + desfase - 1) // 12, (hoy.month + desfase - 1) % 12 + 1, dia))
                and x >= hoy - timedelta(days=7)
                and x.weekday() == semana
            ),
            None,
        )
        halladas.append(
            Mencion(m.start(), m.end(), fechas=frozenset({f}) if f else frozenset(), sin_normalizar=f is None, explicita=True)
        )
    for m in _FECHA_DIA.finditer(plano):
        semana = _DIAS[m.group(1)]
        # «el jueves»: el próximo (hoy, si es jueves, o el de la semana siguiente).
        delta = (semana - hoy.weekday()) % 7
        posibles = {hoy + timedelta(days=delta)} | ({hoy + timedelta(days=7)} if delta == 0 else set())
        halladas.append(Mencion(m.start(), m.end(), fechas=frozenset(posibles)))
    for m in _RELATIVA.finditer(plano):
        palabra = re.sub(r"\s+", " ", m.group(1))
        if palabra == "manana" and _ANTES_DE_MANANA.search(plano[: m.start()]):
            continue  # «en la mañana», «de la mañana», «por la mañana»: parte del día, no fecha
        dias = {"hoy": 0, "manana": 1, "pasado manana": 2}[palabra]
        halladas.append(Mencion(m.start(), m.end(), fechas=frozenset({hoy + timedelta(days=dias)})))
    return _sin_solapes(halladas)


_COORDINADA = re.compile(r"^¿\s*(?:y|o|e|u)\b", re.IGNORECASE)
_ITEM = re.compile(r"^\s*(?:\d{1,2}[.)]|[-•*])\s+")


def contar_preguntas(texto: str) -> int:
    """Oraciones interrogativas: `¿…?`, las que terminan en `?` (una racha cuenta una vez) y un `¿`
    sin cierre. Pedir datos en serie es una sola pregunta: la que arranca con «¿Y…?» / «¿O…?» se suma
    a la anterior, y una lista (ítems numerados o con viñeta) cuenta una vez."""
    listas = 0
    en_lista = False
    corrido: list[str] = []
    for linea in (texto or "").splitlines():
        if _ITEM.match(linea):
            if not en_lista and ("?" in linea or "¿" in linea):
                listas += 1
                en_lista = True
            continue
        en_lista = False
        corrido.append(linea)
    t = re.sub(r"\?+", "?", " ".join(corrido))
    cerradas = [m.group(0) for m in _PREGUNTA_CERRADA.finditer(t)]
    resto = _PREGUNTA_CERRADA.sub(" ", t)
    sueltas = resto.count("?") + resto.count("¿")
    coordinadas = sum(1 for i, p in enumerate(cerradas) if i > 0 and _COORDINADA.match(p))
    return listas + len(cerradas) - coordinadas + sueltas


# ------------------------------------------------------------------ respaldo
@dataclass
class Respaldo:
    """Lo que respalda una hora o una fecha que el borrador afirma."""

    pares: set[tuple[date, str]] = field(default_factory=set)
    horas: set[str] = field(default_factory=set)
    fechas: set[date] = field(default_factory=set)
    # Horas que el cliente dijo y que una tarea tiene fijadas por el cliente (eco legítimo).
    eco: set[str] = field(default_factory=set)

    def sumar(self, fecha: date | None, hora: str | None) -> None:
        if hora:
            self.horas.add(hora)
        if fecha:
            self.fechas.add(fecha)
        if fecha and hora:
            self.pares.add((fecha, hora))


_EXCLUIDAS = frozenset({"busy_slots", "business_hours", "error", "hint", "message", "mensaje"})
_CLAVES_HORA = frozenset({"free_slots", "slots", "horas", "hora", "time", "hora_local"})
_ISO = re.compile(r"^\s*\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\s*$")
_CLAVES_FECHA = frozenset({"date", "fecha", "dia"})
_CLAVES_TEXTO = frozenset({"_safe_append"})


def _instante(valor: str, zona: ZoneInfo) -> tuple[date, str] | None:
    try:
        dt = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(zona)
    return dt.date(), f"{dt.hour:02d}:{dt.minute:02d}"


def _fecha_de(valor: str, hoy: date) -> date | None:
    plano = normalizar(valor)
    for m in fechas_en(plano, hoy):
        if len(m.fechas) == 1:
            return next(iter(m.fechas))
    return None


def _horas_de(valor: str) -> set[str]:
    """«HH:MM» del servidor es de 24 h: sin la ambigüedad de «a las 3»."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*", valor)
    if m:
        h = _hora(int(m.group(1)), int(m.group(2)))
        return {h} if h else set()
    return {h for x in horas_en(normalizar(valor)) for h in x.horas}


def _sumar_texto(respaldo: Respaldo, texto: str, hoy: date) -> None:
    """Las horas y fechas de un texto del sistema, emparejadas si van en la misma oración."""
    for oracion in _oraciones(normalizar(texto)):
        fechas = [f for m in fechas_en(oracion, hoy) for f in m.fechas]
        horas = [h for m in horas_en(oracion) for h in m.horas]
        for h in horas:
            respaldo.horas.add(h)
        for f in fechas:
            respaldo.fechas.add(f)
            for h in horas:
                respaldo.pares.add((f, h))


def _recorrer(nodo: Any, respaldo: Respaldo, zona: ZoneInfo, hoy: date, fecha_ctx: date | None = None) -> None:
    """Todo instante o fecha ISO que devolvió el servidor respalda (un `proximo_cargo`, el `start` de
    una cita), salvo bajo las claves excluidas (`busy_slots`: horas ocupadas; texto de un error). Las
    horas sueltas «HH:MM» solo bajo las claves que ofrecen horarios (`free_slots`, `hora`)."""
    if isinstance(nodo, list):
        for item in nodo:
            _recorrer(item, respaldo, zona, hoy, fecha_ctx)
        return
    if not isinstance(nodo, dict):
        return
    fecha_local = fecha_ctx
    crudo = nodo.get("date")
    if isinstance(crudo, str) and (f := _fecha_de(crudo, hoy)) is not None:
        fecha_local = f
    for clave, valor in nodo.items():
        k = str(clave).lower()
        if k in _EXCLUIDAS:
            continue
        if isinstance(valor, dict | list) and k not in _CLAVES_HORA:
            _recorrer(valor, respaldo, zona, hoy, fecha_local)
            continue
        if k in _CLAVES_HORA:
            for texto in _cadenas(valor):
                for h in _horas_de(texto):
                    respaldo.sumar(fecha_local, h)
        elif not isinstance(valor, str):
            continue
        elif k in _CLAVES_TEXTO:
            _sumar_texto(respaldo, valor, hoy)
        elif _ISO.match(valor) and (par := _instante(valor, zona)) is not None:
            respaldo.sumar(par[0], par[1] if "T" in valor or " " in valor.strip() else None)
        elif k in _CLAVES_FECHA and (f := _fecha_de(valor, hoy)) is not None:
            respaldo.sumar(f, None)


def _cadenas(valor: Any) -> Iterator[str]:
    if isinstance(valor, str):
        yield valor
    elif isinstance(valor, list):
        for item in valor:
            yield from _cadenas(item)


def _valor_de_dato(valor: Any, respaldo: Respaldo, zona: ZoneInfo, hoy: date) -> None:
    """El valor de un dato de una tarea: instante ISO, fecha o «HH:MM»."""
    if not isinstance(valor, str):
        return
    if (par := _instante(valor, zona)) is not None and "T" in valor:
        respaldo.sumar(*par)
    elif (f := _fecha_de(valor, hoy)) is not None:
        respaldo.sumar(f, None)
    else:
        for h in _horas_de(valor):
            respaldo.sumar(None, h)


def respaldo_de(
    resultados: Iterable[Any],
    tareas: Iterable[Any],
    *,
    contexto_sistema: str = "",
    contexto_cliente: str = "",
    zona: str | None = None,
    ahora: datetime | None = None,
) -> tuple[Respaldo, date]:
    """El respaldo de horarios y la fecha de hoy en la zona del negocio."""
    tz = _zona(zona)
    hoy = (ahora.astimezone(tz) if ahora and ahora.tzinfo else (ahora or datetime.now(tz))).date()
    respaldo = Respaldo(fechas={hoy})
    for resultado in resultados:
        _recorrer(resultado, respaldo, tz, hoy)
    if contexto_sistema:
        _sumar_texto(respaldo, contexto_sistema, hoy)
    del_cliente: set[str] = set()
    for tarea in tareas:
        slots = tarea.get("slots") if isinstance(tarea, dict) else None
        for meta in (slots or {}).values():
            if not isinstance(meta, dict) or meta.get("value") is None:
                continue
            fuente = str(meta.get("source") or "")
            if fuente.startswith("action:"):
                _valor_de_dato(meta["value"], respaldo, tz, hoy)
            else:
                propio = Respaldo()
                _valor_de_dato(meta["value"], propio, tz, hoy)
                del_cliente |= propio.horas
    if contexto_cliente and del_cliente:
        dichas = {h for m in horas_en(normalizar(contexto_cliente)) for h in m.horas}
        respaldo.eco = dichas & del_cliente
    return respaldo, hoy


def _zona(nombre: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(nombre or ZONA_POR_DEFECTO)
    except Exception:
        return ZoneInfo(ZONA_POR_DEFECTO)


_CORTE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _oraciones(plano: str) -> Iterator[str]:
    for parte in _CORTE.split(plano):
        if parte.strip():
            yield parte


def horarios_sin_respaldo(oraciones: Iterable[str], respaldo: Respaldo, hoy: date) -> tuple[list[str], int]:
    """`(sin respaldo, sin normalizar)` de las oraciones que ya afirman (sin preguntas), ya pasadas por
    `normalizar` antes de partirlas (si no, «p. m.» corta la oración). Una hora se
    compara con el par (fecha, hora) cuando la oración trae una fecha que el respaldo tiene con horas;
    si no, contra las horas sueltas. Una fecha sin hora, contra las fechas (y hoy)."""
    from core.shared.polaridad import afirma

    fallas: list[str] = []
    sin_normalizar = 0
    for oracion in oraciones:
        plano = normalizar(oracion)
        fechas_m = fechas_en(plano, hoy)
        horas_m = horas_en(plano)
        sin_normalizar += sum(1 for m in fechas_m if m.sin_normalizar)
        fechas = {f for m in fechas_m for f in m.fechas}
        con_horas = {f for f, _h in respaldo.pares}
        for m in horas_m:
            if not afirma(plano, _match(plano, m)):
                continue
            if m.horas & respaldo.eco:
                continue
            if fechas & con_horas:
                ok = any((f, h) in respaldo.pares for f in fechas for h in m.horas)
            else:
                ok = bool(m.horas & respaldo.horas)
            if not ok:
                fallas.append(plano[m.inicio : m.fin])
        if not horas_m:
            for m in fechas_m:
                if not m.explicita or m.sin_normalizar or not afirma(plano, _match(plano, m)):
                    continue
                if not (m.fechas & respaldo.fechas):
                    fallas.append(plano[m.inicio : m.fin])
    return fallas, sin_normalizar


def _match(texto: str, m: Mencion) -> re.Match[str]:
    """Un `re.Match` sobre la mención (lo que pide `polaridad.afirma`)."""
    hallado = re.compile(re.escape(texto[m.inicio : m.fin])).search(texto, m.inicio)
    assert hallado is not None
    return hallado


__all__ = [
    "ZONA_POR_DEFECTO",
    "Mencion",
    "Respaldo",
    "contar_preguntas",
    "fechas_en",
    "horarios_sin_respaldo",
    "horas_en",
    "normalizar",
    "respaldo_de",
]
