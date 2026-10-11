"""Mensaje de confirmación que arma el kernel (HU-AOS-09, design §7.1, ADR-0113).

Lo irreversible se confirma con un botón y el cliente confirma lo que el SERVIDOR va a ejecutar:
el cuerpo se arma con los argumentos exactos que firma el botón (`confirmar:<tarea>:<seq>:<firma>`,
atado al hash de esos argumentos) y con los datos de la tarea, nunca con el texto del modelo. Los
rótulos viven en el playbook del kernel `confirmacion_boton` (invariante 3).

Puro: recibe los datos ya elegidos por el motor (`ContextoMotor.confirmacion_del_turno`) y devuelve
el mensaje. Todo valor pasa por el mismo saneado que los parámetros de plantilla (sin saltos ni
tabulaciones, acotado), así un valor nunca abre una línea ni parece otra instrucción, y el cuerpo
respeta los topes de WhatsApp (1024 caracteres el cuerpo, 20 el título del botón).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

from core.kernel.tarjeta import RotulosInvalidos

MAX_CUERPO = 1024           # cuerpo de un mensaje interactivo de WhatsApp
MAX_TITULO_BOTON = 20       # título de un botón de respuesta
MAX_VALOR = 80
MAX_LINEAS = 10
_SECCIONES = (
    "encabezado", "linea", "monto", "verdadero", "falso", "boton", "instruccion_boton", "instruccion_texto",
    "sin_resumen",
)
_CAMPOS = {"encabezado": ("titulo",), "linea": ("rotulo", "valor"), "monto": ("valor",)}
# Un identificador interno no le dice nada al cliente (uuid, o ids tipo `Q-9f3a…` largos).
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Dato:
    nombre: str
    valor: Any
    es_monto: bool = False


@dataclass(frozen=True)
class Confirmacion:
    """Lo que el kernel envía después del texto del turno.

    - `interactivo`: el payload del botón (`{"type": "button", "body", "buttons"}`) para un canal
      que lo soporta con la ventana abierta;
    - `texto`: el mismo cuerpo con la instrucción de responder «confirmo», para los demás."""

    cuerpo: str
    payload: str
    titulo_boton: str
    texto: str

    def interactivo(self) -> dict[str, Any]:
        return {"type": "button", "body": self.cuerpo, "buttons": [{"id": self.payload, "title": self.titulo_boton}]}


@dataclass(frozen=True)
class Rotulos:
    textos: dict[str, str]


def parsear_rotulos(contenido: str) -> Rotulos:
    from core.kernel.procedures.textos import secciones

    crudas = secciones(contenido)
    faltan = [s for s in _SECCIONES if len(crudas.get(s, [])) != 1]
    if faltan:
        raise RotulosInvalidos(f"rótulos de confirmación incompletos: {faltan}")
    textos = {s: crudas[s][0] for s in _SECCIONES}
    for seccion, campos in _CAMPOS.items():
        try:
            textos[seccion].format(**{c: "x" for c in campos})
        except (KeyError, IndexError, ValueError) as exc:
            raise RotulosInvalidos(f"el rótulo {seccion} usa un campo desconocido: {exc}") from exc
    if len(textos["boton"]) > MAX_TITULO_BOTON:
        raise RotulosInvalidos("el título del botón pasa de 20 caracteres")
    return Rotulos(textos=textos)


@cache
def rotulos() -> Rotulos:
    from core.agents.playbooks import load_core_playbook

    return parsear_rotulos(load_core_playbook("confirmacion_boton"))


# Marcas de formato de WhatsApp (negrita, cursiva, tachado, monoespaciado): un valor no puede
# tachar ni resaltar lo que el cliente confirma.
_MARCAS_WHATSAPP = str.maketrans("", "", "*_~`")


def limpiar(valor: Any, limite: int = MAX_VALOR) -> str:
    """Una línea visible y acotada: sin controles ni formato Unicode (saltos, bidi, ancho cero:
    categorías Cc, Cf, Zl y Zp), sin marcas de formato de WhatsApp y sin espacios repetidos."""
    crudo = str(valor).translate(_MARCAS_WHATSAPP)
    texto = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in crudo)
    texto = re.sub(r"\s{2,}", " ", texto).strip()
    return texto[:limite].rstrip()


def _parece_tarjeta(texto: str) -> bool:
    """13 a 19 dígitos (con espacios o guiones) que pasan Luhn: un número de tarjeta nunca se
    muestra, aunque llegue en un argumento con otro nombre."""
    digitos = re.sub(r"[ -]", "", texto)
    if not digitos.isdigit() or not 13 <= len(digitos) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digitos)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _numero(v: int | float) -> str:
    """600000 → 600.000; 12.5 → 12,50 (separadores de es-CO)."""
    if isinstance(v, float) and not v.is_integer():
        crudo = f"{v:,.2f}"
    else:
        crudo = f"{int(v):,}"
    return crudo.replace(",", "§").replace(".", ",").replace("§", ".")


def _valor(dato: Dato, r: Rotulos) -> str | None:
    v = dato.valor
    if isinstance(v, bool):
        return r.textos["verdadero"] if v else r.textos["falso"]
    if isinstance(v, int | float):
        if v != v or v in (float("inf"), float("-inf")) or _parece_tarjeta(str(int(v))):
            return None
        texto = _numero(v)
        return r.textos["monto"].format(valor=texto) if dato.es_monto else texto
    if isinstance(v, str):
        limpio = limpiar(v)
        return limpio if limpio and not _UUID.fullmatch(limpio) and not _parece_tarjeta(limpio) else None
    return None  # listas, objetos y None no se muestran


def _rotulo(nombre: str) -> str | None:
    """El nombre con espacios: `metodo_entrega` → "Metodo entrega". Los ids no se muestran."""
    if nombre == "id" or nombre.endswith("_id") or nombre.endswith("_ids"):
        return None
    return limpiar(nombre.replace("_", " ").strip().capitalize(), 40) or None


def armar(titulo: str, datos: Sequence[Dato], payload: str, r: Rotulos | None = None) -> Confirmacion:
    """El mensaje de confirmación de UNA petición. `payload` es el id firmado del botón."""
    rot = r if r is not None else rotulos()
    lineas: list[str] = []
    vistos: set[str] = set()
    # Los montos del servidor van primero: el recorte por largo quita datos desde el final y
    # nunca un monto.
    for dato in sorted(datos, key=lambda d: not d.es_monto):
        if dato.nombre in vistos or len(lineas) >= MAX_LINEAS:
            continue
        rotulo, valor = _rotulo(dato.nombre), _valor(dato, rot)
        if rotulo is None or valor is None:
            continue
        vistos.add(dato.nombre)
        lineas.append(rot.textos["linea"].format(rotulo=rotulo, valor=valor))
    encabezado = rot.textos["encabezado"].format(titulo=limpiar(titulo, 120))

    def cuerpo(instruccion: str) -> str:
        # Demasiado largo: se recortan líneas de datos desde el final (nunca la instrucción).
        propias = list(lineas)
        while propias and len("\n".join([encabezado, *propias, "", instruccion])) > MAX_CUERPO:
            propias.pop()
        return "\n".join([encabezado, *propias, "", instruccion])[:MAX_CUERPO]

    return Confirmacion(
        cuerpo=cuerpo(rot.textos["instruccion_boton"]),
        payload=payload,
        titulo_boton=rot.textos["boton"],
        texto=cuerpo(rot.textos["instruccion_texto"]),
    )


def sin_resumen(r: Rotulos | None = None) -> Confirmacion:
    """Texto fijo, sin datos ni botón, para cuando el kernel no puede armar un resumen fiel (los
    args no están, un monto es ambiguo o choca con un argumento): nunca vuelve el texto del
    modelo pidiendo confirmar algo que compromete."""
    rot = r if r is not None else rotulos()
    texto = rot.textos["sin_resumen"]
    return Confirmacion(cuerpo=texto, payload="", titulo_boton="", texto=texto)


def datos_de_args(
    args: Mapping[str, Any], declaradas: Sequence[str], ocultas: frozenset[str] = frozenset()
) -> list[Dato]:
    """Los argumentos que el servidor va a ejecutar, en el orden del schema de la acción.

    Solo las propiedades que el `input_schema` de la acción declara (una clave extra que invente
    el modelo no se muestra: la acción la ignora), sin credenciales, claves reservadas ni las de
    `ocultas` (los datos `payment` del procedimiento)."""
    from core.agents.credenciales import es_sensible

    return [
        Dato(k, args[k])
        for k in list(declaradas)[:40]
        if k in args and not k.startswith("_") and k not in ocultas and not es_sensible(k)
    ]


__all__ = [
    "Confirmacion", "Dato", "Rotulos", "armar", "datos_de_args", "limpiar", "parsear_rotulos", "rotulos", "sin_resumen",
]
