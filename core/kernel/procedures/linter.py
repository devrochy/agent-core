"""Validador y linter de `procedure/1` (ADR-0113 §1, HU-AOS-03).

- `validar(raw)`: la forma del contrato (pydantic), con un mensaje en español por error.
- `lint(proc, catalogo)`: lo que la forma no ve, también un mensaje por problema:
  acciones que el binario no trae, etapas inalcanzables, acciones `irreversible` sin
  confirmación, afirmaciones de dinero sin fuente del servidor y referencias rotas.
- `revisar(raw, catalogo)`: las dos cosas. Es lo que corren el CLI, el CI y la API.

Puro: no lee base, red ni entorno. El catálogo lo arma quien llama (`catalogo.py`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from agentcore_contracts import Procedure, Stage
from pydantic import ValidationError

from core.kernel.procedures.afirmaciones import accion_de, acciones_que_respaldan, respaldo_de
from core.kernel.procedures.catalogo import CORE_OWNER, Catalogo

# Prefijo del botón de confirmación del motor (`confirmar:<task_id>:<seq>`, HU-AOS-05): ningún
# procedimiento lo puede declarar como suyo. Igual a `motor.PREFIJO_CONFIRMAR`.
PREFIJO_CONFIRMAR = "confirmar:"

# Prefijos de los botones de las opciones directas (SPEC-40, ADR-0172 §3): tocar una opción, confirmarla y
# cancelarla. Los atiende el resolver del core ANTES que el motor de procedimientos, así que ningún
# procedimiento puede declararlos (el payload `opt:…` nunca le llegaría, o peor, lo leería como un dato).
PREFIJOS_OPCIONES = ("opt:", "optc:", "optn:")


def choca_con_opciones(prefijo: str) -> bool:
    """¿Este prefijo de payload de un procedimiento tapa o lee los botones de las opciones directas?
    El router compara con `payload.startswith(prefijo)`, así que choca el que es igual a uno reservado,
    el que lo precede (`o`, `op`) y el que lo extiende (`opt:x`). No choca `options:` ni `opcion:`."""
    return bool(prefijo) and any(r.startswith(prefijo) or prefijo.startswith(r) for r in PREFIJOS_OPCIONES)

# Tope de un procedimiento serializado (CLI y API). Holgado: el más grande de los
# críticos ocupa unos 4 KiB.
MAX_PROCEDURE_BYTES = 64 * 1024
# Tope de problemas que se devuelven: un JSON con miles de errores no debe producir una
# respuesta de miles de líneas. El último avisa cuántos quedaron fuera.
MAX_ISSUES = 50
# Largo máximo de un segmento de la ruta que viene del documento (nombres de datos o de
# campos desconocidos): la ruta se devuelve en la API y se imprime en el CLI.
_MAX_SEGMENTO = 60


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


# ------------------------------------------------------------------ forma
_PISTAS: dict[str, str] = {
    "id": "se espera <dueño>.<nombre> en minúsculas, p. ej. pack_commerce.contratar_plan",
    "version": "se espera una versión X.Y.Z",
    "setBy": "se espera action:<acción>, interactive:<prefijo>: o content:<location|image|document|audio>",
    "interactivePayloadPrefix": "se espera un prefijo terminado en dos puntos, p. ej. plan:",
    "resolver": "se espera <dueño>.<función>",
    "source": "se espera action:<acción>[.<campo>] o resolver:<dueño>.<función>",
    "mustMatch": "se espera action:<acción>[.<campo>] o slot:<dato>",
    "requires": "se espera action:<acción>[.<campo>] o slot:<dato>",
    "actions": "se espera un nombre de acción (letras, números y _)",
    "preconditions": "se espera un nombre de acción (letras, números y _)",
    "action": "se espera un nombre de acción (letras, números y _)",
    "events": "se espera <dueño>.<evento>",
    "evals": "se espera un id de escenario (minúsculas, números, - y _)",
}
_NOMBRE = "se espera un nombre en minúsculas con números y _ (hasta 40)"


def _segmento(texto: str) -> str:
    return texto if len(texto) <= _MAX_SEGMENTO else texto[: _MAX_SEGMENTO - 1] + "…"


def _ruta(loc: tuple[int | str, ...]) -> str:
    ruta = ""
    for parte in loc:
        if isinstance(parte, int):
            ruta += f"[{parte}]"
        elif parte == "[key]":
            ruta += " (nombre)"
        else:
            parte = _segmento(parte)
            ruta += f".{parte}" if ruta else parte
    return ruta or "(raíz)"


def con_tope(issues: list[Issue]) -> list[Issue]:
    """Los primeros `MAX_ISSUES` problemas y, si hay más, uno final que dice cuántos."""
    if len(issues) <= MAX_ISSUES:
        return issues
    resto = len(issues) - MAX_ISSUES
    return [
        *issues[:MAX_ISSUES],
        Issue("demasiados_errores", "(raíz)", f"Y {resto} problema(s) más: corrige estos y vuelve a validar."),
    ]


def _campo(loc: tuple[int | str, ...]) -> str:
    textos = [p for p in loc if isinstance(p, str) and p != "[key]"]
    return textos[-1] if textos else ""


def _mensaje(error: Any, contrato: str = "procedure/1") -> str:
    """Un mensaje en español por error de pydantic. `contrato` nombra el contrato en el
    mensaje de campo desconocido (también lo usa el validador de AgentSpec v3/KernelSpec)."""
    tipo = error["type"]
    ctx = error.get("ctx") or {}
    loc = tuple(error["loc"])
    if tipo == "missing":
        return "Falta este campo obligatorio."
    if tipo == "extra_forbidden":
        return f"Campo desconocido: {contrato} no lo define."
    if tipo == "string_pattern_mismatch":
        if loc[:1] == ("requires",):
            return "Formato inválido: se espera el id de una habilidad, p. ej. pack_payments."
        return f"Formato inválido: {_PISTAS.get(_campo(loc), _NOMBRE)}."
    if tipo == "literal_error":
        return f"Valor no permitido; se espera {ctx.get('expected', 'otro valor')}."
    if tipo == "too_long":
        return f"Tiene demasiados elementos (máximo {ctx.get('max_length')})."
    if tipo == "too_short":
        return f"Tiene muy pocos elementos (mínimo {ctx.get('min_length')})."
    if tipo == "string_too_long":
        return f"Texto demasiado largo (máximo {ctx.get('max_length')} caracteres)."
    if tipo == "string_too_short":
        return f"Texto demasiado corto (mínimo {ctx.get('min_length')} caracteres sin contar espacios)."
    if tipo in ("greater_than_equal", "less_than_equal"):
        return "Número fuera de rango."
    if tipo in ("dict_type", "model_type", "model_attributes_type"):
        return "Se espera un objeto."
    if tipo == "list_type":
        return "Se espera una lista."
    if tipo in ("string_type", "bool_type", "int_type", "float_type"):
        return "Tipo de valor inválido."
    return f"Valor inválido ({tipo})."


def validar(raw: Any) -> tuple[Procedure | None, list[Issue]]:
    """Valida la forma. Nunca lanza: devuelve el modelo o los errores."""
    if not isinstance(raw, dict):
        return None, [Issue("forma", "(raíz)", "El procedimiento debe ser un objeto JSON.")]
    try:
        return Procedure.model_validate(raw), []
    except ValidationError as exc:
        return None, [
            Issue("forma", _ruta(tuple(e["loc"])), _mensaje(e))
            for e in exc.errors(include_url=False, include_input=False)
        ]


# ------------------------------------------------------------------ linter
_TIPO_POR_CONTENIDO = {
    "location": "location",
    "image": "media",
    "document": "media",
    "audio": "media",
}


def _alcanzables(stages: list[Stage]) -> set[int]:
    """Índices de las etapas a las que se llega desde la primera.

    Una etapa con `exitWhen` lleva a su `next` o, si no lo tiene, a la siguiente de la
    lista. Sin `exitWhen` es terminal."""
    indice = {s.id: i for i, s in reversed(list(enumerate(stages)))}
    vistas: set[int] = set()
    pendientes = [0]
    while pendientes:
        i = pendientes.pop()
        if i in vistas:
            continue
        vistas.add(i)
        stage = stages[i]
        if stage.exitWhen is None:
            continue
        destino = indice.get(stage.next) if stage.next else i + 1
        if destino is not None and destino < len(stages):
            pendientes.append(destino)
    return vistas


class _Linter:
    def __init__(self, proc: Procedure, catalogo: Catalogo) -> None:
        self.proc = proc
        self.catalogo = catalogo
        self.issues: list[Issue] = []
        self.acciones_de_etapas = {a for s in proc.stages for a in s.actions}
        # ADR-0113 §1: acciones del core, de la habilidad y, por nombre, de las que declara.
        self.duenos_permitidos = {CORE_OWNER, proc.owner, *proc.requires}

    def add(self, code: str, path: str, message: str) -> None:
        self.issues.append(Issue(code, path, message))

    # -- referencias
    def accion(self, nombre: str, path: str, *, en_etapas: bool = False) -> None:
        info = self.catalogo.get(nombre)
        if info is None:
            self.add(
                "accion_inexistente",
                path,
                f"La acción '{nombre}' no existe: ni el core ni ninguna habilidad la trae.",
            )
            return
        if info.owner not in self.duenos_permitidos:
            self.add(
                "habilidad_no_declarada",
                path,
                f"La acción '{nombre}' es de '{info.owner}': declárala en `requires` para usarla "
                "por nombre (nunca se importa otra habilidad).",
            )
        if en_etapas and nombre not in self.acciones_de_etapas:
            self.add(
                "accion_fuera_de_etapas",
                path,
                f"La acción '{nombre}' no está en ninguna etapa: el procedimiento nunca la llama.",
            )

    def slot(self, nombre: str, path: str) -> bool:
        if nombre in self.proc.slots:
            return True
        self.add("slot_desconocido", path, f"El dato '{nombre}' no está declarado en `slots`.")
        return False

    def fuente(self, fuente: str, path: str) -> None:
        accion = accion_de(fuente)
        if accion is not None:
            self.accion(accion, path, en_etapas=True)
        elif fuente.startswith("slot:"):
            self.slot(fuente.removeprefix("slot:"), path)

    def resolver(self, nombre: str, path: str) -> None:
        dueno = nombre.split(".", 1)[0]
        if dueno not in (self.proc.owner, CORE_OWNER):
            self.add(
                "resolver_ajeno",
                path,
                f"El resolver '{nombre}' es de '{dueno}': un procedimiento solo usa resolvers "
                f"de su habilidad ('{self.proc.owner}') o del core.",
            )

    # -- reglas
    def habilidades(self) -> None:
        conocidas = {info.owner for info in self.catalogo.values()}
        for i, dueno in enumerate(self.proc.requires):
            if dueno in (CORE_OWNER, self.proc.owner):
                self.add(
                    "requires_redundante",
                    f"requires[{i}]",
                    f"'{dueno}' no hace falta en `requires`: sus acciones ya están permitidas.",
                )
            elif dueno not in conocidas:
                self.add(
                    "habilidad_desconocida",
                    f"requires[{i}]",
                    f"La habilidad '{dueno}' no trae acciones en este binario.",
                )

    def etapas(self) -> None:
        stages = self.proc.stages
        vistos: set[str] = set()
        ids = {s.id for s in stages}
        for i, stage in enumerate(stages):
            base = f"stages[{i}]"
            if stage.id in vistos:
                self.add("etapa_repetida", f"{base}.id", f"La etapa '{stage.id}' está repetida.")
            vistos.add(stage.id)
            for j, accion in enumerate(stage.actions):
                self.accion(accion, f"{base}.actions[{j}]")
                info = self.catalogo.get(accion)
                confirmada = stage.confirmation is not None or accion in stage.confirmActions
                if info is not None and info.side_effect == "irreversible" and not confirmada:
                    self.add(
                        "irreversible_sin_confirmacion",
                        f"{base}.actions[{j}]",
                        f"La acción '{accion}' es irreversible y la etapa '{stage.id}' no pide "
                        "confirmación: declara `confirmation` en la etapa o la acción en `confirmActions`.",
                    )
            for j, accion in enumerate(stage.confirmActions):
                if accion not in stage.actions:
                    self.add(
                        "confirmacion_de_accion_ajena",
                        f"{base}.confirmActions[{j}]",
                        f"La acción '{accion}' no está entre las acciones de la etapa '{stage.id}'.",
                    )
            for j, accion in enumerate(stage.preconditions):
                self.accion(accion, f"{base}.preconditions[{j}]")
            if stage.next is not None and stage.next not in ids:
                self.add("next_desconocido", f"{base}.next", f"La etapa '{stage.next}' no existe.")
            salida = stage.exitWhen
            if salida is None:
                continue
            if not (salida.slots or salida.confirmed or salida.action):
                self.add(
                    "salida_vacia",
                    f"{base}.exitWhen",
                    "La condición de salida no pide nada: declara `slots`, `confirmed` o `action`, "
                    "o quita `exitWhen` si la etapa es terminal.",
                )
            for j, nombre in enumerate(salida.slots):
                self.slot(nombre, f"{base}.exitWhen.slots[{j}]")
            for j, nombre in enumerate(salida.confirmed):
                if self.slot(nombre, f"{base}.exitWhen.confirmed[{j}]") and not self.proc.slots[nombre].confirm:
                    self.add(
                        "confirmacion_sin_dato",
                        f"{base}.exitWhen.confirmed[{j}]",
                        f"El dato '{nombre}' no pide confirmación (`confirm: true`): nunca quedaría confirmado.",
                    )
            if salida.action is not None:
                if salida.action not in self.catalogo:
                    self.accion(salida.action, f"{base}.exitWhen.action")
                elif salida.action not in stage.actions:
                    self.add(
                        "accion_fuera_de_etapa",
                        f"{base}.exitWhen.action",
                        f"La acción de salida '{salida.action}' no está entre las acciones de la etapa "
                        f"'{stage.id}'.",
                    )
        alcanzables = _alcanzables(stages)
        for i, stage in enumerate(stages):
            if i not in alcanzables:
                self.add(
                    "etapa_inalcanzable",
                    f"stages[{i}]",
                    f"La etapa '{stage.id}' es inalcanzable: ninguna etapa anterior lleva a ella "
                    "(la anterior es terminal o su `next` apunta a otra).",
                )

    def datos(self) -> None:
        prefijos: dict[str, str] = {}
        for nombre, slot in self.proc.slots.items():
            base = f"slots.{nombre}"
            if slot.enum is not None and slot.type not in (None, "string"):
                self.add("tipo_con_enum", f"{base}.type", "Un dato con `enum` es de tipo string.")
            if slot.enum is not None and slot.default is not None and slot.default not in slot.enum:
                self.add("default_invalido", f"{base}.default", "El valor por defecto no está en `enum`.")
            if slot.requiredWhen is not None:
                cond = slot.requiredWhen.slot
                if cond == nombre:
                    self.add("condicion_circular", f"{base}.requiredWhen.slot", "El dato no puede depender de sí mismo.")
                else:
                    self.slot(cond, f"{base}.requiredWhen.slot")
            if slot.resolver is not None:
                self.resolver(slot.resolver, f"{base}.resolver")
            if slot.setBy is not None:
                origen, _, valor = slot.setBy.partition(":")
                if origen == "action":
                    self.accion(valor, f"{base}.setBy", en_etapas=True)
                elif origen == "interactive":
                    if valor == PREFIJO_CONFIRMAR:
                        self.add(
                            "prefijo_reservado",
                            f"{base}.setBy",
                            f"El prefijo '{valor}' es del core (botón de confirmación del motor).",
                        )
                    elif choca_con_opciones(valor):
                        self.add(
                            "prefijo_reservado",
                            f"{base}.setBy",
                            f"El prefijo '{valor}' es del core (botones de las opciones directas).",
                        )
                    if slot.candidates is None and slot.enum is None and slot.resolver is None:
                        self.add(
                            "dato_interactivo_sin_restriccion",
                            f"{base}.setBy",
                            "Un dato que llena un botón acepta lo que mande el cliente: declara "
                            "`candidates`, `enum` o un `resolver` que lo valide.",
                        )
                    if slot.setBy in prefijos:
                        self.add(
                            "prefijo_interactivo_repetido",
                            f"{base}.setBy",
                            f"El prefijo '{valor}' ya llena el dato '{prefijos[slot.setBy]}': "
                            "un payload solo puede llenar un dato.",
                        )
                    prefijos.setdefault(slot.setBy, nombre)
                elif origen == "content" and slot.type != _TIPO_POR_CONTENIDO[valor]:
                    self.add(
                        "tipo_incompatible_con_contenido",
                        f"{base}.type",
                        f"Un dato que llena el contenido '{valor}' es de tipo "
                        f"'{_TIPO_POR_CONTENIDO[valor]}'.",
                    )
            if slot.candidates is not None:
                fuente = slot.candidates.source
                if fuente.startswith("resolver:"):
                    self.resolver(fuente.removeprefix("resolver:"), f"{base}.candidates.source")
                else:
                    self.fuente(fuente, f"{base}.candidates.source")

    def afirmaciones(self) -> None:
        for i, claim in enumerate(self.proc.claims):
            base = f"claims[{i}]"
            for j, fuente in enumerate(claim.mustMatch):
                self.fuente(fuente, f"{base}.mustMatch[{j}]")
            if claim.requires is not None:
                self.fuente(claim.requires, f"{base}.requires")
            if claim.kind == "amount":
                sin_servidor = [f for f in claim.mustMatch if not acciones_que_respaldan(self.proc, f)]
                if not claim.mustMatch or sin_servidor:
                    detalle = f" ('{sin_servidor[0]}' no viene del servidor)" if sin_servidor else ""
                    self.add(
                        "afirmacion_de_dinero_sin_fuente",
                        f"{base}.mustMatch",
                        "La afirmación de dinero no tiene fuente del servidor"
                        f"{detalle}: declara en `mustMatch` una acción del procedimiento "
                        "(`action:<acción>.<campo>`) o un dato con `setBy: action:`.",
                    )
            elif claim.kind == "link" and not claim.mustMatch:
                self.add(
                    "afirmacion_sin_fuente",
                    f"{base}.mustMatch",
                    "La afirmación de link no dice de dónde sale: declara en `mustMatch` la acción "
                    "o el dato que lo trae.",
                )
            if claim.kind == "status" and not claim.phrases:
                self.add(
                    "afirmacion_sin_frases",
                    f"{base}.phrases",
                    "La afirmación de estado no declara las frases que la dan por hecha.",
                )
            if claim.phrases and not respaldo_de(self.proc, claim):
                self.add(
                    "afirmacion_sin_respaldo",
                    f"{base}.requires",
                    "Ninguna acción respalda estas frases: el verificador las rechazaría siempre. "
                    "Declara en `requires` una acción o un dato con `setBy: action:`.",
                )


    def disparadores_y_cierre(self) -> None:
        if self.proc.triggers.interactivePayloadPrefix == PREFIJO_CONFIRMAR:
            self.add(
                "prefijo_reservado",
                "triggers.interactivePayloadPrefix",
                f"El prefijo '{PREFIJO_CONFIRMAR}' es del core (botón de confirmación del motor).",
            )
        prefijo = self.proc.triggers.interactivePayloadPrefix
        if prefijo is not None and choca_con_opciones(prefijo):
            self.add(
                "prefijo_reservado",
                "triggers.interactivePayloadPrefix",
                f"El prefijo '{prefijo}' es del core (botones de las opciones directas).",
            )
        if self.proc.completion is None:
            return
        for i, evento in enumerate(self.proc.completion.events):
            dueno = evento.split(".", 1)[0]
            if dueno not in self.duenos_permitidos:
                self.add(
                    "evento_de_habilidad_no_declarada",
                    f"completion.events[{i}]",
                    f"El evento '{evento}' es de '{dueno}': el dueño de un evento de cierre es el core, "
                    "la habilidad del procedimiento o una de `requires`.",
                )


def lint(proc: Procedure, catalogo: Catalogo) -> list[Issue]:
    """Problemas del procedimiento contra el catálogo de acciones del binario."""
    linter = _Linter(proc, catalogo)
    linter.habilidades()
    linter.disparadores_y_cierre()
    linter.etapas()
    linter.datos()
    linter.afirmaciones()
    return linter.issues


def revisar(raw: Any, catalogo: Catalogo) -> list[Issue]:
    """Valida la forma y, si es válida, aplica el linter. Lista vacía = sin problemas.
    Devuelve a lo sumo `MAX_ISSUES` problemas más uno que cuenta el resto."""
    proc, issues = validar(raw)
    if proc is None:
        return con_tope(issues)
    return con_tope(lint(proc, catalogo))
