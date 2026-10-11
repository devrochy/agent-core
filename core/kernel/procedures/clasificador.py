"""Clasificador de confirmación escrita: determinista y conservador (HU-AOS-09, ADR-0113).

Decide si el texto del cliente es una confirmación EXPLÍCITA de lo que el kernel le pidió
confirmar. El botón firmado sigue siendo la vía preferida (payload determinista, sin interpretar
texto); este clasificador cubre los canales sin botón y al cliente que escribe en vez de tocarlo.
Solo lo usa el motor activo (`tenants.aos_procedures`), a través de `textos.confirmacion_alta`.

Precisión antes que cobertura: un falso "sí" ejecuta algo que compromete; un falso "no" solo hace
que el asistente vuelva a pedir la confirmación. Reglas, en orden:

1. Una pregunta (`?`, `¿`, `؟`, `‽`… sobre el texto en NFKC) nunca confirma ("¿confirmo?"), ni un
   texto con símbolos o letras no latinas que la normalización borraría ("confirmo ❌", "confirmo
   нет"); solo se toleran los emojis de aprobación (👍 ✅ 👌…).
2. Una frase exacta de `## si` del playbook `confirmacion` confirma (lo que aceptaba HU-AOS-05).
3. Si no, el texto confirma solo si:
   - no tiene más de `MAX_PALABRAS` palabras;
   - todas sus palabras (o frases de varias palabras) están en `nucleo`, `prefijos` o `cortesia`;
   - trae al menos una del `nucleo` (una marca de confirmación: "confirmo", "acepto", "correcto");
   - no trae ninguna de `negaciones`. Es redundante con la regla anterior, pero deja explícito que
     una frase con "no", "pero" o "espera" nunca confirma, aunque alguien amplíe el léxico.

Así "sí", "dale", "ok" o "listo" solos no confirman, porque no traen núcleo (decisión del dueño),
y cualquier palabra fuera del léxico tampoco ("sí, pero cambia el correo", "confirmo mañana").

El léxico vive en el playbook del kernel `confirmacion` (invariante 3): aquí solo está la gramática.
Se mide con `tests/fixtures/confirmacion/etiquetado.jsonl`, sin LLM: el test exige precisión 1.0.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import cache

from core.kernel.tarjeta import RotulosInvalidos

MAX_PALABRAS = 8
# Signos de pregunta de cualquier escritura (se mira sobre el texto en NFKC: "？" → "?").
_PREGUNTA = frozenset("?¿؟‽⁇⁈⁉⸮;")
# Los únicos símbolos que se toleran: emojis de aprobación (y sus tonos de piel). Cualquier otro
# símbolo, o una letra fuera del alfabeto latino, descarta: la normalización los borraría en
# silencio ("confirmo ❌", "confirmo нет").
_EMOJIS_OK = frozenset("👍👌✅✔☑🙏🙌")
_TONOS = frozenset(chr(c) for c in range(0x1F3FB, 0x1F400))


def _solo_lo_legible(crudo: str) -> bool:
    """¿Todo lo que la normalización borraría es inofensivo (signos de puntuación, espacios,
    acentos, dígitos y los emojis de aprobación)?"""
    for c in crudo:
        cat = unicodedata.category(c)
        if cat.startswith("L"):
            base = unicodedata.normalize("NFKD", c)[0].lower()
            if not ("a" <= base <= "z" or base == "ñ"):
                return False
        elif cat in ("So", "Sk", "Sm", "Sc", "Cf", "Co", "Cn", "Cs"):
            if c not in _EMOJIS_OK and c not in _TONOS and c != "\ufe0f":
                return False
    return True
_SECCIONES = ("si", "nucleo", "prefijos", "cortesia", "negaciones")


@dataclass(frozen=True)
class Lexico:
    exactas: frozenset[str]
    nucleo: frozenset[str]
    prefijos: frozenset[str]
    cortesia: frozenset[str]
    negaciones: frozenset[str]

    @property
    def permitidas(self) -> frozenset[str]:
        return self.nucleo | self.prefijos | self.cortesia

    @property
    def largo_max_frase(self) -> int:
        return max((len(f.split()) for f in self.permitidas | self.negaciones), default=1)


def parsear_lexico(contenido: str) -> Lexico:
    """Las cinco secciones del playbook `confirmacion`, normalizadas. Fail-closed: si falta una
    sección o queda vacía, lanza (lo cubre un test de CI)."""
    from core.kernel.procedures.textos import normalizar, secciones

    crudas = secciones(contenido)
    valores: dict[str, frozenset[str]] = {}
    for nombre in _SECCIONES:
        frases = frozenset(n for f in crudas.get(nombre, []) if (n := normalizar(f)))
        if not frases:
            raise RotulosInvalidos(f"el léxico de confirmación no tiene la sección {nombre}")
        valores[nombre] = frases
    lexico = Lexico(
        exactas=valores["si"], nucleo=valores["nucleo"], prefijos=valores["prefijos"],
        cortesia=valores["cortesia"], negaciones=valores["negaciones"],
    )
    choque = lexico.permitidas & lexico.negaciones
    if choque:
        raise RotulosInvalidos(f"frases permitidas y negadas a la vez: {sorted(choque)}")
    return lexico


@cache
def lexico() -> Lexico:
    from core.agents.playbooks import load_core_playbook

    return parsear_lexico(load_core_playbook("confirmacion"))


def _frases(palabras: list[str], conocidas: frozenset[str], largo_max: int) -> list[str] | None:
    """Parte las palabras en frases del léxico, la más larga primero. None si alguna palabra no
    está en ninguna frase conocida."""
    salida: list[str] = []
    i = 0
    while i < len(palabras):
        for n in range(min(largo_max, len(palabras) - i), 0, -1):
            candidata = " ".join(palabras[i : i + n])
            if candidata in conocidas:
                salida.append(candidata)
                i += n
                break
        else:
            return None
    return salida


def es_confirmacion(texto: str, lex: Lexico | None = None) -> bool:
    """¿El texto es una confirmación explícita, con confianza alta?"""
    from core.kernel.procedures.textos import normalizar

    lx = lex if lex is not None else lexico()
    crudo = unicodedata.normalize("NFKC", texto or "")
    norm = normalizar(crudo)
    if not norm or any(c in _PREGUNTA for c in crudo):
        return False  # una pregunta nunca confirma ("¿confirmo?")
    if not _solo_lo_legible(crudo):
        return False  # un símbolo o una letra que la normalización borraría ("confirmo ❌")
    if norm in lx.exactas:
        return True
    palabras = norm.split()
    if len(palabras) > MAX_PALABRAS:
        return False
    frases = _frases(palabras, lx.permitidas | lx.negaciones, lx.largo_max_frase)
    if frases is None:
        return False  # una palabra fuera del léxico
    if any(f in lx.negaciones for f in frases):
        return False  # una negación o duda en cualquier parte descarta
    return any(f in lx.nucleo for f in frases)


__all__ = ["MAX_PALABRAS", "Lexico", "es_confirmacion", "lexico", "parsear_lexico"]
