"""Comandos del hilo como tools del LLM (HU-AOS-06, design §5.2, ADR-0113).

El LLM propone y el kernel decide: el modelo ve los comandos como tools con JSON Schema estricto
y el motor (`ejecucion.ContextoMotor.aplicar_comando`) los aplica. Este módulo solo decide
**qué** comandos se ofrecen, arma sus schemas y valida la forma de los argumentos.

- Solo con el motor activo (`tenants.aos_procedures`). En sombra o con la bandera apagada no se
  ofrece ningún comando y el prompt es idéntico.
- Solo se ofrecen los que aplican al estado (demasiados comandos confunden al modelo):
  - `iniciar_procedimiento(procedure_id)`: sin tarea en foco y con candidatos; `procedure_id` es
    un enum de esos candidatos, y el motor rechaza cualquier otro id sin crear la tarea;
  - `actualizar_tarea(slots)`: con tarea en foco y datos que el modelo puede escribir (sin
    `setBy`, ni ubicación ni archivo); `slots` tiene una propiedad por dato, con su tipo, `enum`
    y `format`, y `additionalProperties: false`;
  - `confirmar_tarea()`: con tarea en foco y algo pendiente de confirmar
    (`confirmable_por_comando`, el punto que puede ajustar la confirmación con botón);
  - `cancelar_tarea()`: con tarea en foco.
- Sin `task_id` ni `motivo` (adenda de ADR-0113): actúan sobre la tarea en foco, porque el
  modelo no ve UUIDs y no hay dos tareas abiertas del mismo procedimiento; un motivo libre sería
  PII en `task_events`, que no se puede redactar. Pausar y retomar quedan para HU-AOS-20.
- Las descripciones viven en el playbook del kernel `comandos` (invariante 3); los schemas son
  estructura y van aquí. Los títulos y descripciones de los procedimientos son dato de la
  habilidad: van como cadenas JSON escapadas (`tarjeta.escapar`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any

from agentcore_contracts import Procedure, Slot

from core.kernel.state import Proyeccion
from core.kernel.tarjeta import RotulosInvalidos, escapar
from core.mcp.types import MCPTool, ToolContext
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "comandos"
INICIAR = "iniciar_procedimiento"
ACTUALIZAR = "actualizar_tarea"
CONFIRMAR = "confirmar_tarea"
CANCELAR = "cancelar_tarea"
# Nombres reservados: ninguna acción del binario puede llamarse así (lo cubre un test) y una
# tool declarativa del negocio con ese nombre no se ofrece mientras el motor esté activo.
NOMBRES = frozenset({INICIAR, ACTUALIZAR, CONFIRMAR, CANCELAR})
# Argumentos de primer nivel de cada comando (`additionalProperties: false`).
_ARGUMENTOS: dict[str, frozenset[str]] = {
    INICIAR: frozenset({"procedure_id"}),
    ACTUALIZAR: frozenset({"slots"}),
    CONFIRMAR: frozenset(),
    CANCELAR: frozenset(),
}
# Tipos que el modelo no escribe: los llena el contenido que envía el cliente (ADR-0113 §1).
_TIPOS_SIN_LLM = frozenset({"location", "media"})
_FORMATO_JSON = {"date": "date", "time": "time", "datetime": "date-time"}
_FORMATO_DE_DATO = {"email": "email", "uri": "uri"}

TEXTOS = {
    INICIAR: (),
    "iniciar_procedimiento_id": (),
    "candidatos": (),
    "candidato": ("id", "titulo", "descripcion"),
    ACTUALIZAR: (),
    "actualizar_tarea_slots": (),
    CONFIRMAR: (),
    CANCELAR: (),
    "comando_no_disponible": ("comando",),
    "argumentos_invalidos": ("comando",),
}
_SECCION = re.compile(r"^## ([a-z_]+)$")


def parsear_textos(contenido: str) -> dict[str, str]:
    """Secciones `## clave` del playbook → texto de una línea. Exige todas y sus campos."""
    secciones: dict[str, list[str]] = {}
    actual: str | None = None
    for linea in contenido.splitlines():
        m = _SECCION.match(linea)
        if m:
            actual = str(m.group(1))
            secciones[actual] = []
        elif actual is not None and linea.strip():
            secciones[actual].append(linea.strip())
    faltan = sorted(set(TEXTOS) - set(secciones))
    if faltan:
        raise RotulosInvalidos(f"faltan textos de los comandos: {faltan}")
    textos: dict[str, str] = {}
    for clave, campos in TEXTOS.items():
        lineas = secciones[clave]
        if len(lineas) != 1:
            raise RotulosInvalidos(f"el texto {clave} debe ser una sola línea")
        try:
            lineas[0].format(**{c: "x" for c in campos})
        except (KeyError, IndexError, ValueError) as exc:
            raise RotulosInvalidos(f"el texto {clave} usa un campo desconocido: {exc}") from exc
        textos[clave] = lineas[0]
    return textos


@cache
def textos() -> dict[str, str]:
    """Textos del playbook del kernel (fail-closed: si el archivo está roto, lanza)."""
    from core.agents.playbooks import load_core_playbook

    return parsear_textos(load_core_playbook(PLAYBOOK))


def texto(clave: str, **campos: str) -> str:
    return textos()[clave].format(**campos)


# ---------------------------------------------------------------- qué se ofrece


def escribibles(proc: Procedure) -> dict[str, Slot]:
    """Datos que el modelo puede fijar con `actualizar_tarea` (los demás los fija el servidor,
    un botón o el contenido que envía el cliente). Un dato con `candidates` se elige entre las
    opciones ofrecidas (botón o lista), no lo escribe el modelo. El schema del comando y el
    motor usan esta misma lista."""
    return {
        n: s
        for n, s in proc.slots.items()
        if s.setBy is None and s.type not in _TIPOS_SIN_LLM and s.candidates is None
    }


def confirmable_por_comando(tarea: Proyeccion, proc: Procedure) -> bool:
    """¿Se ofrece `confirmar_tarea`? Hoy, si la tarea tiene algo pendiente de confirmar. Es el
    punto que ajusta la confirmación con botón (HU-AOS-09) si lo irreversible deja de aceptar el
    sí escrito."""
    return tarea.pending_confirmation is not None


def aplicables(
    *, foco: tuple[Proyeccion, Procedure] | None, candidatos: Sequence[Procedure]
) -> list[str]:
    """Comandos que aplican al estado, en un orden fijo (determinista para el prompt)."""
    if foco is None:
        return [INICIAR] if candidatos else []
    tarea, proc = foco
    salida: list[str] = []
    if escribibles(proc):
        salida.append(ACTUALIZAR)
    if confirmable_por_comando(tarea, proc):
        salida.append(CONFIRMAR)
    salida.append(CANCELAR)
    return salida


# ---------------------------------------------------------------- schemas


def schema_de_dato(slot: Slot) -> dict[str, Any]:
    """JSON Schema de un dato (el motor vuelve a validar tipo, opciones, formato y resolver)."""
    tipo = slot.type or "string"
    schema: dict[str, Any] = {"type": tipo if tipo in ("string", "integer", "number", "boolean") else "string"}
    if tipo in _FORMATO_JSON:
        schema["format"] = _FORMATO_JSON[tipo]
    if slot.enum is not None:
        schema = {"type": "string", "enum": list(slot.enum)}
    if slot.format in _FORMATO_DE_DATO:
        schema["format"] = _FORMATO_DE_DATO[slot.format]
    return schema


async def _no_se_llama(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raise RuntimeError("los comandos del hilo los aplica el motor, no el host de tools")


def _tool(nombre: str, descripcion: str, propiedades: dict[str, Any], requeridos: list[str]) -> MCPTool:
    parametros: dict[str, Any] = {"type": "object", "properties": propiedades, "additionalProperties": False}
    if requeridos:
        parametros["required"] = requeridos
    return MCPTool(name=nombre, description=descripcion, parameters=parametros, handler=_no_se_llama, plugin_id="core")


# Tope de candidatos en la descripción de `iniciar_procedimiento` (crece con cada procedimiento y
# viaja en cada vuelta). Hasta el router de HU-AOS-17, los primeros por id.
MAX_CANDIDATOS = 12


def _descripcion_de_inicio(candidatos: Sequence[Procedure]) -> str:
    lineas = [texto(INICIAR), texto("candidatos")]
    for proc in candidatos:
        lineas.append(
            texto(
                "candidato",
                id=proc.id,
                titulo=escapar(proc.title),
                descripcion=escapar(proc.triggers.description),
            )
        )
    return "\n".join(lineas)


def herramientas(
    nombres: Sequence[str], *, foco: tuple[Proyeccion, Procedure] | None, candidatos: Sequence[Procedure]
) -> list[MCPTool]:
    """Los `MCPTool` de los comandos `nombres` (salida de `aplicables`)."""
    salida: list[MCPTool] = []
    for nombre in nombres:
        if nombre == INICIAR:
            if len(candidatos) > MAX_CANDIDATOS:
                log.warning("comandos_candidatos_recortados", cantidad=len(candidatos), tope=MAX_CANDIDATOS)
                candidatos = list(candidatos)[:MAX_CANDIDATOS]
            ids = [p.id for p in candidatos]
            salida.append(_tool(
                INICIAR,
                _descripcion_de_inicio(candidatos),
                {"procedure_id": {"type": "string", "enum": ids, "description": texto("iniciar_procedimiento_id")}},
                ["procedure_id"],
            ))
        elif nombre == ACTUALIZAR and foco is not None:
            datos = escribibles(foco[1])
            slots = {
                "type": "object",
                "description": texto("actualizar_tarea_slots"),
                "properties": {n: schema_de_dato(s) for n, s in datos.items()},
                "additionalProperties": False,
            }
            salida.append(_tool(ACTUALIZAR, texto(ACTUALIZAR), {"slots": slots}, ["slots"]))
        elif nombre in (CONFIRMAR, CANCELAR):
            salida.append(_tool(nombre, texto(nombre), {}, []))
    return salida


def argumentos_invalidos(nombre: str, args: Mapping[str, Any]) -> str | None:
    """Error claro si el modelo mandó argumentos fuera del schema del comando (el contenido de
    cada dato lo valida el motor)."""
    permitidos = _ARGUMENTOS.get(nombre)
    if permitidos is None or not isinstance(args, Mapping) or set(args) - permitidos:
        return texto("argumentos_invalidos", comando=nombre)
    if nombre == ACTUALIZAR and not isinstance(args.get("slots"), Mapping):
        return texto("argumentos_invalidos", comando=nombre)
    return None


def claves_auditables(nombre: str, args: Any) -> dict[str, Any]:
    """Lo que se audita de los argumentos: las claves del schema y cuántas sobraron."""
    permitidos = _ARGUMENTOS.get(nombre, frozenset())
    claves = set(args) if isinstance(args, Mapping) else set()
    return {"keys": sorted(claves & permitidos), "extra": len(claves - permitidos)}
