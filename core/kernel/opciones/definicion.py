"""Definición de una plantilla con opciones (SPEC-40, ADR-0172 §1).

Es la forma que guarda `option_template_versions.definition` (jsonb). Pydantic valida solo la FORMA:
tipos, claves conocidas y topes duros que acotan el tamaño. Los límites del producto y de WhatsApp
(20 caracteres del título, 10 opciones, tools elegibles…) los aplica el lint (`lint.py`), que devuelve
un hallazgo por cada falla con su `path`, en vez de lanzar en el primero.

Puro: no lee base, red ni entorno.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

# ---- límites del producto (los aplica el lint)
MAX_OPCIONES = 10          # lista interactiva de WhatsApp; la plantilla admite 10 botones
MAX_BOTONES = 3            # hasta 3 opciones: botones de respuesta; de 4 a 10: lista
MAX_TITULO = 20            # botón interactivo (20) y texto de QUICK_REPLY de plantilla (25): cubre los dos
MAX_ID_OPCION = 12         # presupuesto del payload firmado (ver `payload.py`)
MAX_CUERPO = 1024          # cuerpo interactivo y BODY de plantilla
MAX_PIE = 60               # FOOTER de plantilla e interactivo
MAX_RESPUESTA = 1024
MAX_RESULT_KEYS = 5
MAX_TTL_HORAS = 720        # 30 días (D-SPEC-40-4)
MAX_ARGS_FIJOS_BYTES = 2000

ID_OPCION = re.compile(r"^[a-z0-9_]{1,12}$")
NOMBRE_TOOL = re.compile(r"^[a-zA-Z0-9_]{1,80}$")
# `cliente.nombre`, `pedido.numero`: segmentos en minúsculas separados por punto.
RUTA_VARIABLE = re.compile(r"^[a-z][a-z0-9_]{0,29}(\.[a-z][a-z0-9_]{0,29}){0,3}$")
CLAVE_RESULTADO = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# `{cliente.nombre}`, `{ctx.pedido.numero}`, `{result.estado}`.
LLAVES = re.compile(r"\{([^{}]*)\}")

# Topes duros de la forma (anti-abuso de tamaño): el lint reporta los límites reales.
_Texto = Annotated[str, StringConstraints(max_length=4096)]


class _Estricta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Accion(_Estricta):
    """La tool que ejecuta la opción. Los argumentos salen SOLO de aquí: `args_fijos` (los escribe el
    dueño) y `args_contexto` (argumento → ruta de variable que arma el servidor). Nunca del texto del
    cliente ni del LLM (invariante 7, ADR-0108)."""

    tool: Annotated[str, StringConstraints(max_length=120)]
    args_fijos: dict[Annotated[str, StringConstraints(max_length=80)], Any] = Field(default_factory=dict, max_length=30)
    args_contexto: dict[Annotated[str, StringConstraints(max_length=80)], Annotated[str, StringConstraints(max_length=120)]] = Field(
        default_factory=dict, max_length=30
    )
    confirmar: bool = False


class Opcion(_Estricta):
    id: Annotated[str, StringConstraints(max_length=64)]
    titulo: Annotated[str, StringConstraints(max_length=100)]
    accion: Accion
    respuesta_ok: _Texto
    respuesta_error: _Texto
    result_keys: list[Annotated[str, StringConstraints(max_length=60)]] = Field(default_factory=list, max_length=20)


class Definicion(_Estricta):
    body: _Texto
    footer: Annotated[str, StringConstraints(max_length=200)] | None = None
    variables: list[Annotated[str, StringConstraints(max_length=120)]] = Field(default_factory=list, max_length=30)
    # Una vez por envío (D-SPEC-40-2): tocar una opción agota el envío entero.
    exclusivo: bool = True
    ttl_horas: int = Field(default=24, ge=0, le=100_000)
    opciones: list[Opcion] = Field(max_length=50)


def leer(crudo: Any) -> Definicion:
    """Valida la FORMA; lanza `ValidationError`. Para el guardado ya validado por el lint."""
    return Definicion.model_validate(crudo)


def hallazgos_de_forma(exc: ValidationError) -> list[tuple[str, str]]:
    """`(path, mensaje)` por cada error de forma, con rutas como `opciones[1].accion.tool`."""
    salida: list[tuple[str, str]] = []
    for e in exc.errors():
        salida.append((ruta(e.get("loc", ())), _mensaje(e)))
    return salida


def ruta(loc: tuple[Any, ...]) -> str:
    texto = ""
    for parte in loc:
        texto += f"[{parte}]" if isinstance(parte, int) else (f".{parte}" if texto else str(parte))
    return texto or "definicion"


def _mensaje(e: Any) -> str:
    tipo = str(e.get("type", ""))
    if tipo == "missing":
        return "Falta este campo."
    if tipo == "extra_forbidden":
        return "Campo desconocido."
    if tipo.startswith("string_too_long") or tipo == "too_long":
        return "Es demasiado largo."
    return str(e.get("msg", "Valor inválido."))[:200]


def llaves_de(texto: str) -> list[str]:
    """Contenido de cada `{…}` del texto, en orden."""
    return LLAVES.findall(texto)


def tiene_llaves_sueltas(texto: str) -> bool:
    """Una `{` o `}` que no cierra un `{…}` (el texto de un cliente no se interpreta, pero un
    texto del dueño con llaves sueltas casi siempre es una variable mal escrita)."""
    return "{" in LLAVES.sub("", texto) or "}" in LLAVES.sub("", texto)
