"""Lint de una definición con opciones (SPEC-40, ADR-0172 §5, HU-OPC-01).

`lintar(crudo, contexto)` devuelve un hallazgo `{path, code, severity, mensaje}` por cada falla (no se
detiene en la primera). Una versión solo se publica sin hallazgos de severidad `error`; los `warning`
informan (una variable declarada y sin usar, una confirmación que se impone aunque la definición no la
pida).

Puro: el contexto lo arma quien llama con lo que ya sabe del negocio (fichas de las tools, variables
permitidas). Nada de base, red ni entorno. Las tools elegibles son FAIL-CLOSED: sin ficha, sin la marca
`apta_para_opcion`, sin pack activo, sin binding explícito y habilitado, o gobernada por un procedimiento
habilitado, la opción se rechaza con su motivo. El resolver del toque (PR siguiente) vuelve a lintar al
ejecutar: un procedimiento publicado después puede volver obsoleta una opción.

El contrato de entrada es UNA ficha por tool y negocio: quien llama decide de qué AgentSpec salen los
bindings (el del agente que envía). El resolver nunca lee `Accion.confirmar` directamente: usa
`confirmacion_efectiva`.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from core.kernel.opciones import esquema
from core.kernel.opciones.definicion import (
    CLAVE_RESULTADO,
    ID_OPCION,
    MAX_ARGS_FIJOS_BYTES,
    MAX_CUERPO,
    MAX_ID_OPCION,
    MAX_OPCIONES,
    MAX_PIE,
    MAX_RESPUESTA,
    MAX_RESULT_KEYS,
    MAX_TITULO,
    MAX_TTL_HORAS,
    NOMBRE_TOOL,
    RUTA_VARIABLE,
    Accion,
    Definicion,
    Opcion,
    hallazgos_de_forma,
    llaves_de,
    tiene_llaves_sueltas,
)
from core.kernel.opciones.mapeo import componentes_meta, nombre_de_parametro

Severidad = Literal["error", "warning"]


@dataclass(frozen=True)
class Hallazgo:
    path: str
    code: str
    severity: Severidad
    mensaje: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class FichaTool:
    """Lo que el lint necesita saber de UNA tool, ya resuelto para el negocio. Lo arma `fichas_de_tools`."""

    nombre: str
    parameters: Mapping[str, Any] = field(default_factory=dict)  # JSON Schema de sus argumentos
    apta_para_opcion: bool = False   # `MCPTool.apta_para_opcion`
    sensible: bool = False           # `MCPTool.sensible`
    pack_activo: bool = False        # el pack de la tool está habilitado para el negocio
    binding_habilitado: bool | None = None  # None = sin binding EXPLÍCITO (no vale el fail-open de `_tool_denial`)
    binding_pide_confirmacion: bool = False  # `ToolBinding.requiresConfirmation`
    gobernada_por_procedimiento: bool = False  # la nombra un procedimiento habilitado


@dataclass(frozen=True)
class ContextoLint:
    herramientas: Mapping[str, FichaTool]
    variables: Collection[str]  # rutas permitidas (catálogo del servidor)


# ------------------------------------------------------------------ fichas desde el runtime
def acciones_de_procedimientos(procedimientos: Iterable[Any]) -> frozenset[str]:
    """Acciones que nombran los procedimientos dados (etapas, precondiciones y confirmaciones por acción).
    Recibe SOLO los habilitados del negocio: quien llama decide cuáles lo son."""
    nombres: set[str] = set()
    for proc in procedimientos:
        for etapa in proc.stages:
            nombres.update(etapa.actions)
            nombres.update(etapa.preconditions)
            nombres.update(etapa.confirmActions)
    return frozenset(nombres)


def fichas_de_tools(
    tools: Iterable[Any],
    bindings: Iterable[Any],
    packs_activos: Collection[str],
    gobernadas: Collection[str] = frozenset(),
) -> dict[str, FichaTool]:
    """Fichas desde `MCPTool`s (incluidas las declarativas del negocio, `plugin_id="declarative"`, que
    cuentan como pack activo si `packs_activos` lo incluye), los `ToolBinding` del AgentSpec y las
    acciones que gobierna un procedimiento habilitado."""
    por_tool = {b.tool: b for b in bindings}
    fichas: dict[str, FichaTool] = {}
    for tool in tools:
        binding = por_tool.get(tool.name)
        fichas[tool.name] = FichaTool(
            nombre=tool.name,
            parameters=tool.parameters or {},
            apta_para_opcion=bool(getattr(tool, "apta_para_opcion", False)),
            sensible=bool(getattr(tool, "sensible", False)),
            pack_activo=tool.plugin_id in packs_activos,
            binding_habilitado=None if binding is None else bool(binding.enabled),
            binding_pide_confirmacion=bool(binding is not None and binding.requiresConfirmation),
            gobernada_por_procedimiento=tool.name in gobernadas,
        )
    return fichas


def confirmacion_efectiva(accion: Accion, ficha: FichaTool | None) -> bool:
    """¿La opción exige la confirmación del cliente (`optc`/`optn`)? Sí si la definición la pide, si la tool es
    `sensible` o si el binding tiene `requiresConfirmation`. Sin ficha: sí (fail-closed). Es la que usa el
    resolver, no el `confirmar` crudo de la definición."""
    if accion.confirmar or ficha is None:
        return True
    return ficha.sensible or ficha.binding_pide_confirmacion


# ------------------------------------------------------------------ lint
def lintar(crudo: Any, contexto: ContextoLint) -> list[Hallazgo]:
    """Todos los hallazgos de la definición `crudo` (el jsonb tal cual). Vacío = nada que reportar."""
    try:
        definicion = Definicion.model_validate(crudo)
    except ValidationError as exc:
        return [Hallazgo(p, "esquema", "error", m) for p, m in hallazgos_de_forma(exc)]
    out: list[Hallazgo] = []
    _lintar_cabecera(definicion, contexto, out)
    ids: dict[str, int] = {}
    titulos: dict[str, int] = {}
    for i, opcion in enumerate(definicion.opciones):
        base = f"opciones[{i}]"
        _lintar_identidad(opcion, base, i, ids, titulos, out)
        _lintar_accion(opcion.accion, f"{base}.accion", contexto, out)
        _lintar_respuestas(opcion, base, contexto, out)
    _lintar_meta(definicion, out)
    return out


def hay_errores(hallazgos: Iterable[Hallazgo]) -> bool:
    return any(h.severity == "error" for h in hallazgos)


def _error(out: list[Hallazgo], path: str, code: str, mensaje: str) -> None:
    out.append(Hallazgo(path, code, "error", mensaje))


def _aviso(out: list[Hallazgo], path: str, code: str, mensaje: str) -> None:
    out.append(Hallazgo(path, code, "warning", mensaje))


def _lintar_cabecera(d: Definicion, contexto: ContextoLint, out: list[Hallazgo]) -> None:
    if not d.body.strip():
        _error(out, "body", "cuerpo_vacio", "El mensaje necesita un cuerpo.")
    elif len(d.body) > MAX_CUERPO:
        _error(out, "body", "cuerpo_largo", f"El cuerpo admite {MAX_CUERPO} caracteres; tiene {len(d.body)}.")
    if d.footer is not None and len(d.footer) > MAX_PIE:
        _error(out, "footer", "pie_largo", f"El pie admite {MAX_PIE} caracteres; tiene {len(d.footer)}.")
    if not 1 <= d.ttl_horas <= MAX_TTL_HORAS:
        _error(out, "ttl_horas", "ttl_invalido", f"La vigencia va de 1 a {MAX_TTL_HORAS} horas.")
    if not d.opciones:
        _error(out, "opciones", "sin_opciones", "Agrega al menos una opción.")
    elif len(d.opciones) > MAX_OPCIONES:
        _error(out, "opciones", "demasiadas_opciones", f"Una plantilla admite {MAX_OPCIONES} opciones; tiene {len(d.opciones)}.")

    permitidas = set(contexto.variables)
    declaradas: set[str] = set()
    for i, ruta in enumerate(d.variables):
        path = f"variables[{i}]"
        if not RUTA_VARIABLE.match(ruta):
            _error(out, path, "variable_invalida", f"«{ruta}» no es una ruta de variable (p. ej. cliente.nombre).")
        elif ruta not in permitidas:
            _error(out, path, "variable_fuera_de_catalogo", f"La variable «{ruta}» no está en el catálogo.")
        if ruta in declaradas:
            _error(out, path, "variable_repetida", f"La variable «{ruta}» está repetida.")
        declaradas.add(ruta)

    if tiene_llaves_sueltas(d.body):
        _error(out, "body", "llaves_sueltas", "Hay una llave { o } sin cerrar: las variables se escriben {cliente.nombre}.")
    usadas = llaves_de(d.body)
    for ruta in dict.fromkeys(usadas):
        if not RUTA_VARIABLE.match(ruta):
            _error(out, "body", "variable_invalida", f"«{{{ruta}}}» no es una variable válida.")
        elif ruta not in declaradas:
            _error(out, "body", "variable_no_declarada", f"El cuerpo usa «{{{ruta}}}» y no está en `variables`.")
    for ruta in sorted(declaradas - set(usadas)):
        _aviso(out, "variables", "variable_sin_usar", f"La variable «{ruta}» se declara y el cuerpo no la usa.")
    nombres: dict[str, str] = {}
    for ruta in dict.fromkeys(usadas):
        if RUTA_VARIABLE.match(ruta):
            previa = nombres.setdefault(nombre_de_parametro(ruta), ruta)
            if previa != ruta:
                _error(out, "body", "variable_colision", f"«{previa}» y «{ruta}» darían el mismo parámetro en Meta.")


def _lintar_identidad(
    o: Opcion, base: str, i: int, ids: dict[str, int], titulos: dict[str, int], out: list[Hallazgo]
) -> None:
    if not ID_OPCION.match(o.id):
        _error(
            out,
            f"{base}.id",
            "id_invalido",
            f"El id admite minúsculas, números y _ (1 a {MAX_ID_OPCION}): firma el botón y no cambia entre versiones.",
        )
    if o.id in ids:
        _error(out, f"{base}.id", "id_repetido", f"El id «{o.id}» ya lo usa la opción {ids[o.id] + 1}.")
    ids.setdefault(o.id, i)
    titulo = o.titulo.strip()
    if not titulo:
        _error(out, f"{base}.titulo", "titulo_vacio", "La opción necesita un título.")
    elif len(o.titulo) > MAX_TITULO:
        _error(out, f"{base}.titulo", "titulo_largo", f"El título admite {MAX_TITULO} caracteres; tiene {len(o.titulo)}.")
    if titulo and titulo.casefold() in titulos:
        _aviso(out, f"{base}.titulo", "titulo_repetido", f"Otra opción ya se llama «{titulo}».")
    titulos.setdefault(titulo.casefold(), i)


def _lintar_accion(a: Accion, base: str, contexto: ContextoLint, out: list[Hallazgo]) -> None:
    if not NOMBRE_TOOL.match(a.tool):
        _error(out, f"{base}.tool", "tool_invalida", "El nombre de la acción solo admite letras, números y _.")
        return
    ficha = contexto.herramientas.get(a.tool)
    if ficha is None:
        _error(out, f"{base}.tool", "tool_inexistente", f"La acción «{a.tool}» no existe en este negocio.")
        return
    elegible = True
    if not ficha.apta_para_opcion:
        elegible = False
        _error(out, f"{base}.tool", "tool_no_apta", f"«{a.tool}» no está habilitada para opciones: su autor no la marcó como apta.")
    if not ficha.pack_activo:
        elegible = False
        _error(out, f"{base}.tool", "pack_inactivo", f"El pack de «{a.tool}» no está activo en este negocio.")
    if ficha.binding_habilitado is None:
        elegible = False
        _error(out, f"{base}.tool", "tool_sin_binding", f"«{a.tool}» no tiene un permiso explícito en el asistente: habilítala primero.")
    elif not ficha.binding_habilitado:
        elegible = False
        _error(out, f"{base}.tool", "tool_binding_deshabilitado", f"«{a.tool}» está deshabilitada en el asistente.")
    if ficha.gobernada_por_procedimiento:
        elegible = False
        _error(
            out,
            f"{base}.tool",
            "tool_en_procedimiento",
            f"«{a.tool}» la gobierna un procedimiento habilitado: una opción no puede saltarse sus pasos.",
        )
    if elegible:
        _lintar_argumentos(a, base, ficha, contexto, out)
    if not a.confirmar and confirmacion_efectiva(a, ficha):
        _aviso(
            out,
            f"{base}.confirmar",
            "confirmacion_obligatoria",
            "Esta acción pide la confirmación del cliente aunque no la marques (es sensible o su permiso la exige).",
        )


def _lintar_argumentos(a: Accion, base: str, ficha: FichaTool, contexto: ContextoLint, out: list[Hallazgo]) -> None:
    schema = dict(ficha.parameters or {})
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if len(json.dumps(a.args_fijos, ensure_ascii=False, default=str)) > MAX_ARGS_FIJOS_BYTES:
        _error(out, f"{base}.args_fijos", "args_demasiado_grandes", f"Los valores fijos admiten {MAX_ARGS_FIJOS_BYTES} caracteres.")
        return
    permitidas = set(contexto.variables)
    for nombre in sorted(set(a.args_fijos) & set(a.args_contexto)):
        _error(out, f"{base}.args_contexto.{nombre}", "arg_duplicado", f"«{nombre}» está como valor fijo y como dato del contexto.")
    for nombre, valor in a.args_fijos.items():
        if nombre not in props:
            _error(out, f"{base}.args_fijos.{nombre}", "arg_desconocido", f"«{a.tool}» no recibe «{nombre}».")
            continue
        for path, mensaje in esquema.errores(valor, props[nombre], f"{base}.args_fijos.{nombre}"):
            _error(out, path, "arg_invalido", mensaje)
    for nombre, ruta in a.args_contexto.items():
        path = f"{base}.args_contexto.{nombre}"
        if nombre not in props:
            _error(out, path, "arg_desconocido", f"«{a.tool}» no recibe «{nombre}».")
        if not RUTA_VARIABLE.match(ruta) or ruta not in permitidas:
            _error(out, path, "variable_fuera_de_catalogo", f"«{ruta}» no es un dato del contexto disponible.")
    for nombre in schema.get("required") or []:
        if nombre not in a.args_fijos and nombre not in a.args_contexto:
            _error(
                out,
                f"{base}.args_fijos.{nombre}",
                "arg_requerido_faltante",
                f"«{a.tool}» necesita «{nombre}»: dale un valor fijo o un dato del contexto.",
            )


def _lintar_respuestas(o: Opcion, base: str, contexto: ContextoLint, out: list[Hallazgo]) -> None:
    claves: set[str] = set()
    if len(o.result_keys) > MAX_RESULT_KEYS:
        _error(out, f"{base}.result_keys", "result_keys_demasiadas", f"Se pueden citar hasta {MAX_RESULT_KEYS} datos del resultado.")
    for j, clave in enumerate(o.result_keys):
        if not CLAVE_RESULTADO.match(clave):
            _error(out, f"{base}.result_keys[{j}]", "result_key_invalida", f"«{clave}» no es un nombre de dato válido.")
        if clave in claves:
            _error(out, f"{base}.result_keys[{j}]", "result_key_repetida", f"«{clave}» está repetida.")
        claves.add(clave)
    permitidas = set(contexto.variables)
    for campo, texto, con_resultado in (("respuesta_ok", o.respuesta_ok, True), ("respuesta_error", o.respuesta_error, False)):
        path = f"{base}.{campo}"
        if not texto.strip():
            _error(out, path, "respuesta_vacia", "Escribe lo que verá el cliente.")
            continue
        if len(texto) > MAX_RESPUESTA:
            _error(out, path, "respuesta_larga", f"La respuesta admite {MAX_RESPUESTA} caracteres; tiene {len(texto)}.")
        if tiene_llaves_sueltas(texto):
            _error(out, path, "llaves_sueltas", "Hay una llave { o } sin cerrar: usa {ctx.dato} o {result.dato}.")
        for llave in dict.fromkeys(llaves_de(texto)):
            origen, _, resto = llave.partition(".")
            if origen == "ctx" and resto in permitidas:
                continue
            if origen == "result" and con_resultado and resto in claves:
                continue
            if origen == "result" and not con_resultado:
                _error(out, path, "result_en_error", f"«{{{llave}}}»: si la acción falla no hay resultado que citar.")
            elif origen == "result":
                _error(out, path, "result_no_declarado", f"«{{{llave}}}» no está en `result_keys`.")
            elif origen == "ctx":
                _error(out, path, "variable_fuera_de_catalogo", f"«{{{llave}}}» no es un dato del contexto disponible.")
            else:
                _error(out, path, "placeholder_desconocido", f"«{{{llave}}}» no es válido: usa {{ctx.dato}} o {{result.dato}}.")


_FORMA_ROTA = {"cuerpo_vacio", "sin_opciones", "variable_invalida", "llaves_sueltas", "variable_colision"}


def _lintar_meta(d: Definicion, out: list[Hallazgo]) -> None:
    """Las reglas de la plantilla las reutiliza de `meta_templates` (las mismas con las que el panel crea
    plantillas hoy): no se inventan límites de Meta que el repo no valide. Se salta si la forma ya falló."""
    if any(h.code in _FORMA_ROTA for h in out):
        return
    from core.gateway.meta_templates import TemplateValidationError, validate_template_components

    try:
        validate_template_components("UTILITY", componentes_meta(d), "NAMED")
    except TemplateValidationError as exc:
        _error(out, "body", "meta_componentes", str(exc))
