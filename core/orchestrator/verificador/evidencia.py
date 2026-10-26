"""Evidencia del turno para el verificador de afirmaciones (ADR-0119, HU-ARCH-29).

La evidencia sale de lo que el sistema hizo, nunca de lo que dijo el modelo:
- los resultados de tools del turno actual (con `_ok` y `_current_turn`, que
  estampa el loop) y de turnos anteriores (`audit_log`, `_current_turn=False`);
- el contexto que el negocio y el cliente ya pusieron en la conversación (prompt
  del sistema y mensajes del cliente), solo para las URLs: la web, el Instagram o
  el catálogo del negocio no salen de ninguna tool;
- con el motor de procedimientos activo (`tenants.aos_procedures`, HU-AOS-06), el estado
  de las tareas del episodio (`EstadoTareas`): los datos que fijó el servidor
  (`source = action:*`) son evidencia de montos y referencias, y los de la tarea en foco
  (o de una que se completó después de la última respuesta) respaldan los cierres de la
  acción que los fijó. Lo que escribió el modelo, un botón o el contenido del cliente no
  es evidencia. También cuentan las referencias vigentes del bloque dinámico (ADR-0130),
  que son dato del servidor.

Los mensajes anteriores del propio asistente NO son evidencia: lavarían un dato
inventado en un turno previo.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from core.shared.dinero import extract_monetary_values, normalized_amount_key

TOOL_NAME_KEY = "_tool_name"

# Con esquema o empezando por `www.` (WhatsApp también enlaza esas). Los dominios
# sueltos ("tienda.co/pago") quedan como deuda: exigen validar el TLD.
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]{}\"'`]+", re.IGNORECASE)
# Puntuación que cierra una oración pegada a la URL ("…/abc." o "…/abc).").
_URL_COLA = ".,;:!?¡¿*_~"
# Referencias públicas tipo `OC-0003` (con la versión del link, ADR-0087: `OC-0003-r2`).
REFERENCIA_RE = re.compile(r"\b[A-Z]{2,5}-\d{3,}(?:-r\d+)?\b")


def urls_con_posicion(texto: str) -> list[tuple[int, int, str]]:
    """(inicio, fin, url) de cada URL del texto, sin la puntuación de cierre de la oración."""
    encontradas = []
    for match in URL_RE.finditer(texto or ""):
        url = match.group(0).rstrip(_URL_COLA)
        encontradas.append((match.start(), match.start() + len(url), url))
    return encontradas


def urls_en(texto: str) -> list[str]:
    return [url for _inicio, _fin, url in urls_con_posicion(texto)]


def clave_url(url: str) -> str:
    """Forma comparable de una URL: sin esquema ni `www.`, host en minúsculas y sin
    barra final (`HTTPS://Tienda.com/x/` y `www.tienda.com/x` son la misma)."""
    sin_esquema = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    host, separador, resto = sin_esquema.partition("/")
    host = host.lower().removeprefix("www.")
    return (host + separador + resto).rstrip("/")


def _claves(texto: str) -> set[str]:
    return {clave_url(url) for url in urls_en(texto)}


_CLAVE_IMAGEN = re.compile(r"image|imagen|foto|photo|thumbnail", re.IGNORECASE)
# Claves bajo las que una tool devuelve un link para el cliente (pago, checkout o el
# texto que el core agrega con `_safe_append`). La página de un producto o una imagen
# no respaldan "te envío el link de pago".
_CLAVE_ENLACE = re.compile(r"link|url|pago|payment|checkout|_safe_append", re.IGNORECASE)


# Claves con texto libre de una respuesta fallida (no datos del servidor).
_TEXTO_DEL_ERROR = frozenset({"error", "hint", "message", "mensaje"})


def _textos(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _textos(value)
    elif isinstance(node, list):
        for item in node:
            yield from _textos(item)


def _textos_de_enlace(node: Any, clave: str = "") -> Iterator[str]:
    if isinstance(node, str):
        if _CLAVE_ENLACE.search(clave) and not _CLAVE_IMAGEN.search(clave):
            yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if not _CLAVE_IMAGEN.search(str(key)):
                yield from _textos_de_enlace(value, str(key))
    elif isinstance(node, list):
        for item in node:
            yield from _textos_de_enlace(item, clave)


def familia(referencia: str) -> str:
    """Prefijo de una referencia pública (`OC` de `OC-0042-r2`)."""
    return referencia.split("-", 1)[0]


@dataclass(frozen=True)
class EstadoTareas:
    """El estado que suma el motor activo (HU-AOS-06). `tareas`: instantáneas de
    `ejecucion.instantaneas_de_tareas` (`procedure_id`, `status`, `cierre`, `slots` con
    `value` y `source`). `referencias_vigentes`: las líneas del bloque de ADR-0130."""

    tareas: tuple[dict[str, Any], ...] = ()
    referencias_vigentes: tuple[str, ...] = ()
    # Datos que el motor fijó en este turno por un comando aceptado (`ContextoMotor.datos_fijados`):
    # respaldan solo las afirmaciones que los declaran (`Afirmacion.datos`), nunca montos ni links.
    datos_fijados: tuple[str, ...] = ()


def _datos_del_servidor(tarea: Any) -> tuple[dict[str, Any], set[str]]:
    """`({dato: valor}, {acción})` de los datos que fijó una acción del servidor. Las acciones
    son solo las de los datos que respaldan cierres (`respalda_cierre`: los declara una afirmación
    del procedimiento)."""
    valores: dict[str, Any] = {}
    acciones: set[str] = set()
    slots = tarea.get("slots") if isinstance(tarea, dict) else None
    for nombre, meta in (slots or {}).items():
        if not isinstance(meta, dict) or meta.get("value") is None:
            continue
        fuente = meta.get("source")
        if isinstance(fuente, str) and fuente.startswith("action:") and len(fuente) > 7:
            valores[str(nombre)] = meta["value"]
            if meta.get("respalda_cierre") is True:
                acciones.add(fuente[7:])
    return valores, acciones


@dataclass
class Evidencia:
    # Montos verificados (clave normalizada), del turno actual y de los anteriores:
    # repetir un monto que ya confirmó una acción es legítimo.
    montos: set[str] = field(default_factory=set)
    # Montos del turno actual, en orden, para la instrucción de reintento.
    montos_turno: list[float] = field(default_factory=list)
    # Tools que terminaron bien en el turno actual.
    acciones_ok: set[str] = field(default_factory=set)
    # URLs devueltas por tools del turno actual (incluye `_safe_append`), también si la
    # tool terminó con error: la URL la dio el servidor, no la inventó el modelo (p. ej. los
    # enlaces legales de `terminos_pendientes`, ADR-0125). Un link de pago de un turno
    # anterior puede estar vencido (ADR-0087): no cuenta.
    urls_turno: set[str] = field(default_factory=set)
    # Las que la tool devolvió como link (pago, checkout, `_safe_append`) y terminó bien: son
    # las que respaldan "te envío el link de pago" (`Afirmacion.basta_url`).
    urls_enlace: set[str] = field(default_factory=set)
    # URLs del prompt del sistema (sin la memoria ni el perfil, que escribe el propio
    # asistente) y de los mensajes del cliente.
    # Todas las URLs se guardan con `clave_url`.
    urls_contexto: set[str] = field(default_factory=set)
    # Referencias públicas que aparecen en cualquier resultado (y, con estado, en los datos del
    # servidor de las tareas y en las referencias vigentes).
    referencias: set[str] = field(default_factory=set)
    # Con estado (motor activo, HU-AOS-06):
    con_estado: bool = False
    # Acciones que fijaron un dato de la tarea en foco o de una recién completada: respaldan
    # los cierres de esas acciones aunque no hayan corrido en este turno.
    acciones_estado: set[str] = field(default_factory=set)
    # Prefijos de las referencias que el servidor fijó en las tareas (`OC`): solo esas
    # familias se exigen al borrador.
    familias_estado: set[str] = field(default_factory=set)
    # Datos de la tarea en foco que el motor fijó en este turno (comando aceptado).
    datos_fijados: set[str] = field(default_factory=set)
    # Con estado (HU-AOS-20 parte 3): horas y fechas que respaldan lo que el borrador ofrece, y hoy.
    horario: Any = None
    hoy: Any = None

    @classmethod
    def desde(
        cls,
        resultados: list[dict[str, Any]],
        contexto: str = "",
        estado: EstadoTareas | None = None,
        *,
        contexto_sistema: str = "",
        contexto_cliente: str = "",
        zona: str | None = None,
        ahora: Any = None,
    ) -> Evidencia:
        """`contexto_sistema` (prompt del sistema sin el bloque dinámico) respalda horarios; las
        horas de `contexto_cliente` solo cuentan como eco de un dato que la tarea fijó del cliente."""
        ev = cls(urls_contexto=_claves(contexto))
        if estado is not None:
            ev._sumar_estado(estado)
            from core.orchestrator.verificador.horarios import respaldo_de

            ev.horario, ev.hoy = respaldo_de(
                resultados,
                estado.tareas,
                contexto_sistema=contexto_sistema,
                contexto_cliente=contexto_cliente,
                zona=zona,
                ahora=ahora,
            )
        for resultado in resultados:
            if not isinstance(resultado, dict):
                continue
            # Sin las marcas (llamada directa en tests), se asume turno actual y éxito,
            # mismo criterio que los guardrails de HU-COM-02.
            actual = resultado.get("_current_turn", True)
            ok = resultado.get("_ok", True)
            for valor in extract_monetary_values(resultado):
                ev.montos.add(normalized_amount_key(valor))
                if actual and valor not in ev.montos_turno:
                    ev.montos_turno.append(valor)
            textos = list(_textos(resultado))
            for texto in textos:
                ev.referencias.update(REFERENCIA_RE.findall(texto))
            if actual:
                # De una tool fallida, no las del texto del error ni del `hint`: podrían repetir
                # un argumento con una URL que inventó el modelo.
                datos = resultado if ok else {k: v for k, v in resultado.items() if k not in _TEXTO_DEL_ERROR}
                for texto in _textos(datos):
                    ev.urls_turno.update(_claves(texto))
            if actual and ok:
                nombre = str(resultado.get(TOOL_NAME_KEY) or "")
                if nombre:
                    ev.acciones_ok.add(nombre)
                for texto in _textos_de_enlace(resultado):
                    ev.urls_enlace.update(_claves(texto))
        return ev

    def _sumar_estado(self, estado: EstadoTareas) -> None:
        self.con_estado = True
        self.datos_fijados.update(str(n) for n in estado.datos_fijados)
        for linea in estado.referencias_vigentes:
            if isinstance(linea, str):
                self.referencias.update(REFERENCIA_RE.findall(linea))
        for tarea in estado.tareas:
            valores, acciones = _datos_del_servidor(tarea)
            for valor in extract_monetary_values(valores):
                self.montos.add(normalized_amount_key(valor))
            for texto in _textos(valores):
                encontradas = REFERENCIA_RE.findall(texto)
                self.referencias.update(encontradas)
                self.familias_estado.update(familia(r) for r in encontradas)
            if isinstance(tarea, dict) and tarea.get("cierre") is True:
                self.acciones_estado |= acciones
