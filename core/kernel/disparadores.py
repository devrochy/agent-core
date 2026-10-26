"""Puntaje de disparadores del router del Coordinador (HU-AOS-17, adenda de ADR-0138). Puro.

Sin embeddings ni LLM (P-19, R-AOS-11): el texto del cliente se compara **léxicamente** con los
ejemplos de cada procedimiento (`triggers.examples` de `procedure/1`, más los
`procedures[].overrides.examples` del agente que lo declara) y con su `triggers.description`.

1. Se normaliza (minúsculas, sin tildes ni signos; `textos.normalizar`).
2. Las `equivalencias` del playbook del kernel `disparadores` reemplazan cada variante por su forma
   canónica (la frase más larga primero), y se quitan las `vacias`.
3. Cada término se recorta a sus primeras `_RAIZ` letras (raíz burda del español).
4. El puntaje de un ejemplo combina, con pesos IDF, cuánto del ejemplo aparece en el texto y cuánto
   del texto cubre el ejemplo (media geométrica 0,6/0,4): un término que comparten muchos procedimientos ("plan") pesa menos que uno propio de
   uno solo ("cita"). El del procedimiento es el mejor de sus ejemplos; la descripción cuenta con
   un factor menor (es larga y genérica).

El léxico vive en el playbook (invariante 3). Se mide con `tests/fixtures/ruteo/golden.jsonl`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache

from core.kernel.tarjeta import RotulosInvalidos

_RAIZ = 5
# La descripción es una frase larga y genérica: cuenta, pero menos que un ejemplo.
_FACTOR_DESCRIPCION = 0.85
# Peso de un término del texto que ningún procedimiento usa: resta, pero menos que uno conocido.
_PESO_DESCONOCIDO = 0.3
# El puntaje es la media geométrica ponderada de cuánto del ejemplo dice el texto (lo que más
# pesa) y cuánto del texto explica el ejemplo (palabras de más restan, pero poco).
_PESO_EJEMPLO = 0.6
# Tope del texto que se puntúa: un mensaje larguísimo no debe costar más (p95 del kernel ≤ 50 ms).
MAX_CARACTERES = 500


@dataclass(frozen=True)
class Lexico:
    vacias: frozenset[str]
    # (variante normalizada en palabras, canónica), la más larga primero.
    equivalencias: tuple[tuple[tuple[str, ...], str], ...]
    # Conectores que separan dos pedidos en un mensaje (HU-AOS-20), normalizados y en palabras.
    conectores: tuple[tuple[str, ...], ...] = ()


def parsear_lexico(contenido: str) -> Lexico:
    """Las secciones `vacias`, `equivalencias` y `conectores` del playbook. Fail-closed: si falta una
    o queda vacía, o una línea de equivalencias no tiene la forma `canon: a, b`, lanza (test de CI)."""
    from core.kernel.procedures.textos import normalizar, secciones

    crudas = secciones(contenido)
    vacias = frozenset(n for f in crudas.get("vacias", []) if (n := normalizar(f)))
    if not vacias:
        raise RotulosInvalidos("el léxico de disparadores no tiene la sección vacias")
    pares: dict[tuple[str, ...], str] = {}
    for linea in crudas.get("equivalencias", []):
        canon, sep, resto = linea.partition(":")
        canonica = normalizar(canon)
        if not sep or not canonica:
            raise RotulosInvalidos(f"equivalencia mal formada: {linea[:60]!r}")
        for variante in resto.split(","):
            if v := normalizar(variante):
                pares[tuple(v.split())] = canonica
    if not pares:
        raise RotulosInvalidos("el léxico de disparadores no tiene la sección equivalencias")
    orden = tuple(sorted(pares.items(), key=lambda kv: (-len(kv[0]), kv[0])))
    conectores = {tuple(n.split()) for f in crudas.get("conectores", []) if (n := normalizar(f))}
    if not conectores:
        raise RotulosInvalidos("el léxico de disparadores no tiene la sección conectores")
    return Lexico(
        vacias=vacias, equivalencias=orden, conectores=tuple(sorted(conectores, key=lambda c: (-len(c), c)))
    )


@cache
def lexico() -> Lexico:
    from core.agents.playbooks import load_core_playbook

    return parsear_lexico(load_core_playbook("disparadores"))


def _equivaler(palabras: list[str], lx: Lexico, *, solo_frases: bool = False) -> list[str]:
    salida: list[str] = []
    i = 0
    while i < len(palabras):
        for variante, canonica in lx.equivalencias:
            n = len(variante)
            if solo_frases and n == 1:
                continue
            if tuple(palabras[i : i + n]) == variante:
                salida.extend(canonica.split())
                i += n
                break
        else:
            salida.append(palabras[i])
            i += 1
    return salida


def terminos(texto: str, lx: Lexico | None = None) -> tuple[str, ...]:
    """Términos con significado del texto (sin repetir, en orden). Las equivalencias se aplican
    de varias palabras antes de quitar las vacías ("me pongo al día") y todas después ("comprar el
    plan" → "comprar plan")."""
    from core.kernel.procedures.textos import normalizar

    lx = lx or lexico()
    palabras = _equivaler(normalizar((texto or "")[:MAX_CARACTERES]).split(), lx, solo_frases=True)
    palabras = _equivaler([p for p in palabras if p not in lx.vacias], lx)
    vistos: dict[str, None] = {}
    for t in palabras:
        if t not in lx.vacias:
            vistos.setdefault(t[:_RAIZ], None)
    return tuple(vistos)


# Varios pedidos en un mensaje (HU-AOS-20): signos que cortan siempre y tope de segmentos.
_CORTE_RE = re.compile(r"[,;\n]+|(?<!\b\w)\.(?=\s)|[!?¡¿]+")
MAX_SEGMENTOS = 8


def segmentar(texto: str, lx: Lexico | None = None) -> list[str]:
    """Partes del mensaje que pueden traer un pedido cada una: se corta por coma, punto y coma,
    salto de línea, signos de exclamación o pregunta, punto seguido de espacio que no cierra una
    abreviatura de una letra (no rompe «1.500» ni «p. m.») y por los `conectores` del léxico como palabras aisladas. Una parte
    sin términos con significado se une a la anterior (o a la siguiente, si es la primera). Como
    mucho `MAX_SEGMENTOS`: el resto se une a la última."""
    from core.kernel.procedures.textos import normalizar

    lx = lx or lexico()
    partes: list[str] = []
    for trozo in _CORTE_RE.split((texto or "")[:MAX_CARACTERES]):
        palabras = trozo.split()
        normal = [normalizar(p) for p in palabras]
        actual: list[str] = []
        i = 0
        while i < len(palabras):
            conector = next((c for c in lx.conectores if tuple(normal[i : i + len(c)]) == c), None)
            if conector is not None:
                if actual:
                    partes.append(" ".join(actual))
                actual = []
                i += len(conector)
                continue
            actual.append(palabras[i])
            i += 1
        if actual:
            partes.append(" ".join(actual))
    unidas: list[str] = []
    pendiente = ""
    for parte in partes:
        if not terminos(parte, lx):
            if unidas:
                unidas[-1] = f"{unidas[-1]} {parte}"
            else:
                pendiente = f"{pendiente} {parte}".strip()
            continue
        unidas.append(f"{pendiente} {parte}".strip() if pendiente else parte)
        pendiente = ""
    if len(unidas) > MAX_SEGMENTOS:
        unidas = [*unidas[: MAX_SEGMENTOS - 1], " ".join(unidas[MAX_SEGMENTOS - 1 :])]
    return unidas


@dataclass(frozen=True)
class Exponente:
    """Lo que puntúa un procedimiento: sus ejemplos y su descripción."""

    procedure_id: str
    ejemplos: tuple[str, ...]
    descripcion: str = ""


@dataclass(frozen=True)
class Puntaje:
    procedure_id: str
    score: float


class Indice:
    """Términos de los ejemplos de un conjunto de procedimientos, con su IDF. Se arma una vez por
    release y agente (inmutables) y se reusa en cada turno."""

    def __init__(self, exponentes: Iterable[Exponente], lx: Lexico | None = None) -> None:
        self._lx = lx or lexico()
        self._por_proc: dict[str, list[tuple[frozenset[str], float]]] = {}
        documentos: dict[str, set[str]] = {}
        for e in exponentes:
            filas = self._por_proc.setdefault(e.procedure_id, [])
            vocab = documentos.setdefault(e.procedure_id, set())
            for ej in e.ejemplos:
                t = frozenset(terminos(ej, self._lx))
                if t:
                    filas.append((t, 1.0))
                    vocab |= t
            if e.descripcion:
                t = frozenset(terminos(e.descripcion, self._lx))
                if t:
                    filas.append((t, _FACTOR_DESCRIPCION))
                    vocab |= t
        self._vocab = {pid: frozenset(v) for pid, v in documentos.items()}
        n = max(1, len(documentos))
        df: dict[str, int] = {}
        for vocab in documentos.values():
            for t in vocab:
                df[t] = df.get(t, 0) + 1
        # IDF suavizado: con un solo procedimiento todo pesa 1.
        self._idf = {t: 1.0 + math.log(n / c) for t, c in df.items()}

    @property
    def procedimientos(self) -> frozenset[str]:
        return frozenset(self._por_proc)

    def _peso(self, termino: str) -> float:
        return self._idf.get(termino, _PESO_DESCONOCIDO)

    def puntuar(self, texto: str) -> list[Puntaje]:
        """Puntaje de cada procedimiento (0..1), de mayor a menor (empate: por id)."""
        t = frozenset(terminos(texto, self._lx))
        if not t:
            return []
        salida: list[Puntaje] = []
        for pid, filas in self._por_proc.items():
            # Lo que el texto dice de más solo resta si no es del vocabulario de ESTE procedimiento
            # (otro ejemplo suyo lo usa): "contratar el plan mensual" no pierde por "plan".
            propio = self._vocab.get(pid, frozenset())
            mejor = 0.0
            for e, factor in filas:
                comun = sum(self._peso(x) for x in t & e)
                if not comun:
                    continue
                cubre_ejemplo = comun / sum(self._peso(x) for x in e)
                ajeno = sum(self._peso(x) for x in t - e - propio)
                cubre_texto = comun / (comun + ajeno)
                valor = cubre_ejemplo**_PESO_EJEMPLO * cubre_texto ** (1 - _PESO_EJEMPLO)
                mejor = max(mejor, valor * factor)
            if mejor > 0:
                salida.append(Puntaje(pid, round(min(1.0, mejor), 3)))
        salida.sort(key=lambda p: (-p.score, p.procedure_id))
        return salida


def fuerte(puntajes: Sequence[Puntaje], *, strong: float, margin: float) -> Puntaje | None:
    """El candidato que decide la ruta: ≥ `strong` y con `margin` sobre el segundo."""
    if not puntajes or puntajes[0].score < strong:
        return None
    segundo = puntajes[1].score if len(puntajes) > 1 else 0.0
    return puntajes[0] if puntajes[0].score - segundo >= margin else None


def exponentes_de(procedimientos: Mapping[str, object], overrides: Mapping[str, Sequence[str]] | None = None) -> list[Exponente]:
    """Los exponentes de unos `Procedure` (y los ejemplos extra del agente por id)."""
    salida: list[Exponente] = []
    for pid, proc in procedimientos.items():
        trig = getattr(proc, "triggers", None)
        ejemplos = [str(x) for x in (getattr(trig, "examples", None) or [])]
        ejemplos += [str(x) for x in (overrides or {}).get(pid, ())]
        salida.append(Exponente(pid, tuple(ejemplos), str(getattr(trig, "description", "") or "")))
    return salida


__all__ = [
    "MAX_CARACTERES",
    "MAX_SEGMENTOS",
    "Exponente",
    "Indice",
    "Lexico",
    "Puntaje",
    "exponentes_de",
    "fuerte",
    "lexico",
    "parsear_lexico",
    "segmentar",
    "terminos",
]
