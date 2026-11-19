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
MAX_LINEAS_COMPLETO = 25
# Con documentos que se aceptan (adenda 2026-10-13): si no caben en el cuerpo del botón, sale como
# texto (tope de WhatsApp 4096, con margen); si tampoco caben, el resumen no sale (`sin_resumen`).
MAX_TEXTO = 4000
MAX_ENLACE = 500
_SECCIONES = (
    "encabezado", "linea", "monto", "verdadero", "falso", "boton", "instruccion_boton", "instruccion_texto",
    "sin_resumen", "instruccion_dato", "encabezado_dato", "encabezado_documentos", "encabezado_pregunta", "vineta", "cierre_pregunta", "instruccion_boton_documentos",
    "instruccion_texto_documentos",
)
_CAMPOS = {
    "encabezado": ("titulo",), "encabezado_dato": ("titulo",), "linea": ("rotulo", "valor"), "monto": ("valor",),
    "vineta": ("numero", "rotulo"),
}
# Enlace de un documento que se acepta: https, ASCII visible, sin espacios (como los valida
# `core/provisioning/legal.py` al publicarlos).
_ENLACE = re.compile(r"^https://[\x21-\x7e]{1,500}$")  # MAX_ENLACE
# Un identificador interno no le dice nada al cliente (uuid, o ids tipo `Q-9f3a…` largos).
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Dato:
    nombre: str
    valor: Any
    es_monto: bool = False
    # Enlace de un documento que el cliente acepta al confirmar (adenda 2026-10-13).
    documento_aceptado: bool = False


class ResumenInvalido(ValueError):
    """No hay un resumen fiel que mostrar (un documento sin enlace válido o un texto que no cabe):
    quien llama manda `sin_resumen`."""


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
def rotulos_del_playbook() -> Rotulos:
    from core.agents.playbooks import load_core_playbook

    return parsear_rotulos(load_core_playbook("confirmacion_boton"))


# Marcas de formato de WhatsApp (negrita, cursiva, tachado, monoespaciado): un valor no puede
# tachar ni resaltar lo que el cliente confirma.
_MARCAS_WHATSAPP = str.maketrans("", "", "*_~`")


def limpiar(valor: Any, limite: int = MAX_VALOR, *, marcas: bool = True) -> str:
    """Una línea visible y acotada: sin controles ni formato Unicode (saltos, bidi, ancho cero:
    categorías Cc, Cf, Zl y Zp), sin marcas de formato de WhatsApp (`marcas=False` las deja: un
    correo `juan_perez@…` se muestra tal cual) y sin espacios repetidos."""
    crudo = str(valor).translate(_MARCAS_WHATSAPP) if marcas else str(valor)
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


# Valor de un dato del resumen único: se muestra entero o el resumen no sale.
MAX_VALOR_COMPLETO = 200


def _valor_completo(dato: Dato, r: Rotulos) -> str | None:
    """Un valor del resumen único (adenda 2026-10-13): el cliente confirma TODO lo que ve, así que
    se muestra tal cual (solo sin controles, en una línea). Lanza `ResumenInvalido` si no se puede
    mostrar entero: más largo que `MAX_VALOR_COMPLETO`, con forma de uuid o de tarjeta. None solo
    para lo que nunca se muestra (listas, objetos, None)."""
    v = dato.valor
    if isinstance(v, str):
        limpio = limpiar(v, MAX_VALOR_COMPLETO + 1, marcas=False)
        if not limpio or len(limpio) > MAX_VALOR_COMPLETO or limpio != " ".join(v.split()) \
                or _UUID.fullmatch(limpio) or _parece_tarjeta(limpio):
            raise ResumenInvalido("un dato no se puede mostrar entero")
        return limpio
    if isinstance(v, bool | int | float):
        visible = _valor(dato, r)
        if visible is None:
            raise ResumenInvalido("un dato no se puede mostrar")
        return visible
    return None


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


def _rotulo(nombre: str, rotulos: Mapping[str, str] | None = None) -> str | None:
    """El rótulo que publicó la habilidad (`MCPHost.register_rotulos`, ya saneado) o el nombre con
    espacios: `metodo_entrega` → "Metodo entrega". Los ids no se muestran."""
    if nombre == "id" or nombre.endswith("_id") or nombre.endswith("_ids"):
        return None
    propio = (rotulos or {}).get(nombre)
    if propio:
        return limpiar(propio, 40) or None
    return limpiar(nombre.replace("_", " ").strip().capitalize(), 40) or None


def armar(
    titulo: str,
    datos: Sequence[Dato],
    payload: str,
    r: Rotulos | None = None,
    *,
    es_dato: bool = False,
    rotulos: Mapping[str, str] | None = None,
    completo: bool = False,
    instrucciones: tuple[str, str] | None = None,
) -> Confirmacion:
    """El mensaje de confirmación de UNA petición. `payload` es el id firmado del botón.

    Formato (adenda 2026-10-13 de ADR-0113): encabezado breve, una viñeta por dato, los documentos
    que se aceptan en su propio bloque y la instrucción de respuesta en la última línea. Sin
    `completo`, los montos van al final y nunca se recortan: si el cuerpo no cabe, se quitan datos
    desde el final. Con `completo` o con documentos no se recorta nada: si no cabe en el botón
    sale como texto. Lanza `ResumenInvalido` si un documento no trae un enlace https válido o si
    el resumen no cabe sin recortar.

    `es_dato`: la petición es de un dato (adenda 2026-10-12): sale siempre como texto y con la
    instrucción `instruccion_dato` (basta un "sí"). `rotulos`: los de la habilidad del recorrido.
    `completo`: el resumen único de la etapa (con los datos del cliente, adenda 2026-10-13): el
    cliente confirma TODO lo que ve, así que nada se recorta; si no cabe en el botón, sale como
    texto, y si tampoco cabe ahí, `ResumenInvalido`. El orden es el de `datos`.
    `instrucciones`: `(con botón, como texto)` en lugar de las del playbook (las opciones directas,
    ADR-0172, confirman con «Confirmar» o «Cancelar» y no admiten pedir cambios por escrito)."""
    rot = r if r is not None else rotulos_del_playbook()
    vistos: set[str] = set()
    normales: list[str] = []
    montos: list[str] = []
    reservadas: list[str] = []  # montos que van en su lugar (resumen completo): no cuentan para recortar
    documentos: list[str] = []
    for dato in datos:
        if dato.documento_aceptado:
            enlace = dato.valor if isinstance(dato.valor, str) else ""
            nombre = limpiar(dato.nombre, 60)
            if not nombre or not _ENLACE.fullmatch(enlace):
                raise ResumenInvalido("documento sin enlace válido")
            documentos.append(rot.textos["linea"].format(rotulo=nombre, valor=enlace))
            continue
        if dato.nombre in vistos and not completo:
            continue
        rotulo = _rotulo(dato.nombre, rotulos)
        if rotulo is None:
            continue  # un id (`variant_id`): nunca se muestra
        valor = _valor_completo(dato, rot) if completo else _valor(dato, rot)
        # Un valor de una lista (`pago_en_linea`) con rótulo de la habilidad (`payment_method.pago_en_linea`).
        propio = (rotulos or {}).get(f"{dato.nombre}.{dato.valor}") if isinstance(dato.valor, str) else None
        if propio and valor is not None:
            valor = limpiar(propio, MAX_VALOR) or valor
        if valor is None:
            continue
        vistos.add(dato.nombre)
        linea = rot.textos["linea"].format(rotulo=rotulo, valor=valor)
        if dato.es_monto and not completo:
            montos.append(linea)
        else:
            normales.append(linea)
            if dato.es_monto:
                reservadas.append(linea)
    # Los montos tienen su lugar reservado frente al tope de líneas.
    sin_recorte = completo or bool(documentos)
    if sin_recorte and len(normales) + len(montos) > MAX_LINEAS_COMPLETO:
        raise ResumenInvalido("el resumen tiene demasiadas líneas")
    tope = MAX_LINEAS - len(montos)
    if not sin_recorte and len(normales) > tope:
        sobran = len(normales) - max(0, tope)
        quitar = [ln for ln in reversed(normales) if ln not in reservadas][:sobran]
        normales = [ln for ln in normales if ln not in quitar]
    clave_encabezado = "encabezado_dato" if es_dato else "encabezado"
    encabezado = rot.textos[clave_encabezado].format(titulo=limpiar(titulo, 120))

    def cuerpo(instruccion: str, propias: list[str]) -> str:
        bloques = [encabezado, *propias, *montos]
        if documentos:
            bloques += ["", rot.textos["encabezado_documentos"], *documentos]
        return "\n".join([*bloques, "", instruccion])

    def recortado(instruccion: str) -> str:
        if sin_recorte:
            texto = cuerpo(instruccion, normales)
            if len(texto) > MAX_TEXTO:
                raise ResumenInvalido("el resumen no cabe")
            return texto  # más largo que el botón: el envío lo manda como texto (sender.interactivo_valido)
        # Demasiado largo: se recortan datos desde el final (nunca montos ni la instrucción).
        propias = list(normales)
        while propias and len(cuerpo(instruccion, propias)) > MAX_CUERPO:
            propias.pop()
        return cuerpo(instruccion, propias)[:MAX_CUERPO]

    if es_dato:
        texto = recortado(rot.textos["instruccion_dato"])
        return Confirmacion(cuerpo=texto, payload="", titulo_boton="", texto=texto)
    sufijo = "_documentos" if documentos else ""
    con_boton, como_texto = instrucciones or (
        rot.textos[f"instruccion_boton{sufijo}"],
        rot.textos[f"instruccion_texto{sufijo}"],
    )
    return Confirmacion(
        cuerpo=recortado(con_boton),
        payload=payload,
        titulo_boton=rot.textos["boton"],
        texto=recortado(como_texto),
    )


def pregunta(datos: Sequence[str], rotulos: Mapping[str, str] | None = None, r: Rotulos | None = None) -> str:
    """La pregunta del kernel por los datos que faltan (adenda 2026-10-13: el turno agotó sus
    vueltas con una tarea en foco). Encabezado y una viñeta por dato, con el rótulo de la
    habilidad (o el nombre con espacios), numerada, y el cierre. Los nombres sin rótulo mostrable se omiten; sin ninguno,
    lanza `ResumenInvalido` (quien llama escala como antes)."""
    rot = r if r is not None else rotulos_del_playbook()
    visibles = [x for n in datos if (x := _rotulo(n, rotulos)) is not None]
    if not visibles:
        raise ResumenInvalido("nada que preguntar")
    lineas = [rot.textos["vineta"].format(numero=i, rotulo=x) for i, x in enumerate(visibles, start=1)]
    return "\n".join([rot.textos["encabezado_pregunta"], *lineas, "", rot.textos["cierre_pregunta"]])


def sin_resumen(r: Rotulos | None = None) -> Confirmacion:
    """Texto fijo, sin datos ni botón, para cuando el kernel no puede armar un resumen fiel (los
    args no están, un monto es ambiguo o choca con un argumento): nunca vuelve el texto del
    modelo pidiendo confirmar algo que compromete."""
    rot = r if r is not None else rotulos_del_playbook()
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


# Nombre de siempre del lector del playbook (tests y llamadas existentes).
rotulos = rotulos_del_playbook

__all__ = [
    "Confirmacion", "Dato", "ResumenInvalido", "Rotulos", "armar", "datos_de_args", "limpiar", "parsear_rotulos",
    "pregunta", "rotulos", "rotulos_del_playbook", "sin_resumen",
]
