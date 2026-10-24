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

   **Eco del resumen** (adenda 2026-10-24 de ADR-0113): con el texto del mensaje del kernel que el
   cliente está contestando (`eco`), las palabras de ese mensaje cuentan como `cortesia` (nunca como
   núcleo): «Sí, confirmo. Todo bien con el anual» confirma cuando el resumen decía «Premium anual».
   Solo vale lo que el cliente VIO: «confirmo el mensual» ante un resumen del anual sigue sin
   confirmar (la palabra no está en el eco). Las negaciones del léxico nunca se vuelven eco.

4. (Adenda 2026-10-12) Una frase exacta de `## si` con frases de `cortesia` intercaladas también
   confirma ("sí, está bien, gracias" → "si esta bien"), con las mismas guardas: todo en el
   léxico y ninguna negación. La cortesía nunca suma núcleo: "sí, gracias" sigue sin confirmar.

**Confirmación de un DATO** (`es_asentimiento`, adenda 2026-10-12 de ADR-0113): un dato que el
cliente acaba de dar no compromete nada (sin monto ni efecto), así que como respuesta inmediata a
la pregunta del kernel basta un asentimiento: "sí", "ok", "dale", "sí, el correo está bien". Usa
además las secciones `asiente_dato` y `objetos_de_dato` del léxico, que `es_confirmacion` NO lee:
"confirmo el correo" nunca confirma una acción con monto. Mismas guardas (pregunta, símbolos,
largo, negaciones). Solo lo usa el motor para una confirmación pendiente de tipo dato.

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
_SECCIONES = ("si", "nucleo", "prefijos", "cortesia", "negaciones", "asiente_dato", "objetos_de_dato")


@dataclass(frozen=True)
class Lexico:
    exactas: frozenset[str]
    nucleo: frozenset[str]
    prefijos: frozenset[str]
    cortesia: frozenset[str]
    negaciones: frozenset[str]
    # Solo para confirmar un DATO (`es_asentimiento`), nunca una acción o una etapa.
    asiente_dato: frozenset[str] = frozenset()
    objetos_de_dato: frozenset[str] = frozenset()

    @property
    def permitidas(self) -> frozenset[str]:
        return self.nucleo | self.prefijos | self.cortesia

    @property
    def permitidas_dato(self) -> frozenset[str]:
        return self.permitidas | self.asiente_dato | self.objetos_de_dato

    @property
    def largo_max_frase(self) -> int:
        return max((len(f.split()) for f in self.permitidas_dato | self.negaciones), default=1)


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
        asiente_dato=valores["asiente_dato"], objetos_de_dato=valores["objetos_de_dato"],
    )
    choque = lexico.permitidas_dato & lexico.negaciones
    if choque:
        raise RotulosInvalidos(f"frases permitidas y negadas a la vez: {sorted(choque)}")
    # Por palabra también: una frase permitida de varias palabras con una negación adentro ("no hay
    # problema") se tragaría el "no" al partir el texto (la frase más larga gana).
    sueltas = {n for n in lexico.negaciones if " " not in n}
    con_negacion = sorted(f for f in lexico.permitidas_dato if sueltas & set(f.split()))
    if con_negacion:
        raise RotulosInvalidos(f"frases permitidas con una negación adentro: {con_negacion}")
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


def _normalizado_seguro(texto: str) -> str | None:
    """El texto normalizado, o None si nunca puede confirmar (vacío, pregunta o símbolos que la
    normalización borraría)."""
    from core.kernel.procedures.textos import normalizar

    crudo = unicodedata.normalize("NFKC", texto or "")
    norm = normalizar(crudo)
    if not norm or any(c in _PREGUNTA for c in crudo):
        return None  # una pregunta nunca confirma ("¿confirmo?")
    if not _solo_lo_legible(crudo):
        return None  # un símbolo o una letra que la normalización borraría ("confirmo ❌")
    return norm


# Tope de palabras del eco: es el resumen de un solo mensaje, no una conversación.
_MAX_PALABRAS_ECO = 120


def _palabras_del_eco(eco: str, lx: Lexico) -> frozenset[str]:
    """Las palabras del mensaje que el cliente contesta, sin las que el léxico trata como negación
    (ni sueltas ni dentro de una frase negada de varias palabras)."""
    from core.kernel.procedures.textos import normalizar

    if not eco:
        return frozenset()
    norm = normalizar(unicodedata.normalize("NFKC", eco))
    prohibidas = {palabra for frase in lx.negaciones for palabra in frase.split()}
    return frozenset(p for p in norm.split()[:_MAX_PALABRAS_ECO] if p not in prohibidas)


def _frases_sin_negacion(norm: str, conocidas: frozenset[str], lx: Lexico) -> list[str] | None:
    """Las frases del léxico del texto, o None si es largo, trae una palabra fuera del léxico o
    una negación (en cualquier parte descarta)."""
    palabras = norm.split()
    if len(palabras) > MAX_PALABRAS:
        return None
    frases = _frases(palabras, conocidas | lx.negaciones, lx.largo_max_frase)
    if frases is None or any(f in lx.negaciones for f in frases):
        return None
    return frases


def es_confirmacion(texto: str, lex: Lexico | None = None, eco: str = "") -> bool:
    """¿El texto es una confirmación explícita, con confianza alta? (acciones, etapas y datos)

    `eco`: el mensaje del kernel que el cliente contesta; sus palabras acompañan a una marca de
    confirmación (ver "Eco del resumen" arriba) pero nunca la reemplazan."""
    lx = lex if lex is not None else lexico()
    norm = _normalizado_seguro(texto)
    if norm is None:
        return False
    if norm in lx.exactas:
        return True
    frases = _frases_sin_negacion(norm, lx.permitidas | _palabras_del_eco(eco, lx), lx)
    if frases is None:
        return False
    if any(f in lx.nucleo for f in frases):
        return True
    # Regla 4: una frase exacta de `## si` con cortesía intercalada.
    resto = " ".join(f for f in frases if f not in lx.cortesia)
    return bool(resto) and resto in lx.exactas


def es_asentimiento(texto: str, lex: Lexico | None = None) -> bool:
    """¿El texto asiente a la confirmación de un DATO? (`sí`, `ok`, `sí, el correo está bien`).

    Nunca para una acción o una etapa: ahí rige `es_confirmacion`, más estricto."""
    lx = lex if lex is not None else lexico()
    if es_confirmacion(texto, lx):
        return True
    norm = _normalizado_seguro(texto)
    if norm is None:
        return False
    frases = _frases_sin_negacion(norm, lx.permitidas_dato, lx)
    if frases is None:
        return False
    return any(f in lx.nucleo or f in lx.prefijos or f in lx.asiente_dato for f in frases)


__all__ = ["MAX_PALABRAS", "Lexico", "es_asentimiento", "es_confirmacion", "lexico", "parsear_lexico"]
