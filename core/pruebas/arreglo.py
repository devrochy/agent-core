"""Arreglo sugerido de una prueba que falló (HU-AOS-104, ADR-0212 §6, SPEC-53).

Tres tipos, de lo más a lo menos aplicable:

- `regla`: una regla de respuesta (`GuardrailRule`: frase prohibida, tool obligatoria ante una frase, largo máximo).
  Es lo único que se puede escribir en el borrador (`AgentSpecV3.guardrails`).
- `respuesta`: una respuesta mejor para el cliente. El borrador v3 no guarda textos de respuesta (viven en playbooks
  de la habilidad o del negocio), así que se muestra al dueño para copiarla; **no se aplica sola**.
- `dato`: precio, horario, catálogo, formas de pago o un documento. Tampoco se aplica: apunta a la sección del panel
  donde se edita (`seccion`).

**Quién propone y cuándo.** El corredor, tras juzgar un intento que falla (`proponer`): una llamada a
`generar_estructurado` con el playbook del core `arreglo_de_pruebas`, siempre con la llave y el modelo fijo de la
plataforma (como el juez) y `kind='prueba_agente'`, `result.concepto='arreglo'`. **Una vez por escenario**: con `k > 1`
el primer intento que falla propone y los demás intentos fallidos del mismo escenario guardan el mismo arreglo sin
gastar otra llamada. Una IA caída o una salida que no valida deja el resultado en `falla` con `arreglo = null` (la falla
es real; el arreglo es un extra) y lo gastado se suma a la corrida. No entra en el estimado: es una llamada por escenario
fallido, del orden del juez, y el tope de la corrida (estimado × 1,5) la cubre.

**El arreglo es dato, nunca una orden** (invariante 7): la transcripción viaja como JSON, no en las instrucciones, y
nada de lo que el LLM devuelve se ejecuta ni se escribe sin pasar por `preparar_arreglo` + el contrato del borrador y,
al aplicar, por la confirmación explícita del dueño. Antes de guardarlo se valida contra el esquema `AgentSpecV3` y su
linter (una `required_tool` con una tool que el agente no tiene se descarta).

`preparar_arreglo` es la función pura que usan tanto la vista previa (diff) como «Aplicar»: devuelve el contenido nuevo
del borrador y los cambios. Es idempotente: si la regla ya está, no la duplica y no hay cambios.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal

from agentcore_contracts import GuardrailRule
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from core.pruebas.juez import MAX_TEXTO_TURNO
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "arreglo_de_pruebas"
KIND = "prueba_agente"
MAX_TOKENS = 700
TEMPERATURA = 0.2
TIMEOUT_S = 60.0
MAX_PORQUE = 600
MAX_TEXTO = 1500
MAX_REGLAS_EN_PROMPT = 40
MIN_LARGO_MAXIMO = 50
MIN_FRASE = 3

Tipo = Literal["regla", "respuesta", "dato"]
Seccion = Literal["catalogo", "horario", "formas_de_pago", "conocimiento", "otro"]
ID_DE_PROCEDIMIENTO = r"^[a-z0-9_]{1,60}\.[a-z0-9_]{1,40}$"
TIPOS_APLICABLES: frozenset[str] = frozenset({"regla"})


class ArregloNoAplicable(Exception):
    """El arreglo no es de un tipo que se escriba en el borrador (`respuesta` o `dato`)."""

    def __init__(self, tipo: str, seccion: str | None = None) -> None:
        super().__init__(tipo)
        self.tipo = tipo
        self.seccion = seccion


class Arreglo(BaseModel):
    """El arreglo tal como se guarda en `agent_test_results.arreglo` y como lo devuelve el LLM. Estricto: una clave
    desconocida o un tipo con campos de otro tipo no valida."""

    model_config = ConfigDict(extra="forbid")

    tipo: Tipo
    porque: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PORQUE)]
    regla: GuardrailRule | None = None
    texto: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXTO)] | None = None
    procedimiento: str | None = Field(default=None, pattern=ID_DE_PROCEDIMIENTO)
    seccion: Seccion | None = None

    @model_validator(mode="after")
    def _campos_por_tipo(self) -> Arreglo:
        if self.tipo == "regla":
            if self.regla is None or self.texto or self.seccion or self.procedimiento:
                raise ValueError("una regla trae `regla` y nada más")
            # Un mínimo de sentido: una regla demasiado amplia degradaría al agente si el dueño la aplica sin leer.
            if self.regla.kind == "max_length" and (self.regla.value or 0) < MIN_LARGO_MAXIMO:
                raise ValueError(f"un largo máximo menor a {MIN_LARGO_MAXIMO} caracteres no es razonable")
            if any(len(f.strip()) < MIN_FRASE for f in self.regla.phrases):
                raise ValueError(f"cada frase de la regla tiene al menos {MIN_FRASE} caracteres")
        elif self.tipo == "respuesta":
            if not self.texto or self.regla is not None or self.seccion:
                raise ValueError("una respuesta trae `texto` (y a lo más `procedimiento`)")
        else:  # dato
            if not self.texto or not self.seccion or self.regla is not None or self.procedimiento:
                raise ValueError("un dato trae `texto` y `seccion`")
        return self

    @property
    def aplicable(self) -> bool:
        return self.tipo in TIPOS_APLICABLES


def leer(guardado: Mapping[str, Any] | None) -> Arreglo | None:
    """El arreglo guardado en la base, o None si no hay o ya no valida (la fila es dato, no se confía)."""
    if not guardado:
        return None
    try:
        return Arreglo.model_validate(dict(guardado))
    except ValueError:
        return None


def a_json(arreglo: Arreglo) -> dict[str, Any]:
    return arreglo.model_dump(mode="json", exclude_none=True)


# --------------------------------------------------------------------------------------------- aplicar al contenido
def _regla_json(regla: GuardrailRule) -> dict[str, Any]:
    return regla.model_dump(mode="json", exclude_none=True)


def preparar_arreglo(contenido: Mapping[str, Any], arreglo: Arreglo) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(contenido nuevo, cambios)` de aplicar el arreglo al borrador. Pura: no valida contra el catálogo (lo hace
    `guardar_borrador`) ni escribe. `cambios` es `[{ruta, antes, despues}]`, vacío si ya estaba aplicado (idempotente).
    `ArregloNoAplicable` si el tipo no se escribe en el borrador."""
    if arreglo.tipo not in TIPOS_APLICABLES or arreglo.regla is None:
        raise ArregloNoAplicable(arreglo.tipo, arreglo.seccion)
    nuevo = dict(contenido)
    reglas = list(nuevo.get("guardrails") or [])
    regla = _regla_json(arreglo.regla)
    cuerpo = {k: v for k, v in regla.items() if k != "name"}
    for existente in reglas:
        if isinstance(existente, Mapping) and {k: v for k, v in existente.items() if k != "name" and v is not None} == cuerpo:
            return dict(contenido), []
    nombres = {g.get("name") for g in reglas if isinstance(g, Mapping)}
    base, n = regla["name"], 2
    while regla["name"] in nombres:
        sufijo = f" ({n})"
        regla["name"] = f"{base[: 60 - len(sufijo)]}{sufijo}"
        n += 1
    reglas.append(regla)
    nuevo["guardrails"] = reglas
    return nuevo, [{"ruta": f"guardrails[{len(reglas) - 1}]", "antes": None, "despues": regla}]


def problemas_contra_el_contrato(contenido: Mapping[str, Any], catalogo: Any) -> list[Any]:
    """Los `Issue` del contrato `agentspec/3` y su linter sobre el contenido candidato (sin escribir ni tocar la base)."""
    from core.agents.contrato_v3 import revisar_spec

    return revisar_spec(dict(contenido), catalogo, "agentspec/3")


# ---------------------------------------------------------------------------------------------------- proponer
def _proveedor() -> Any:
    """El mismo proveedor de plataforma y modelo fijo que el juez."""
    from core.pruebas import juez

    return juez._proveedor()


def _entrada(
    *,
    criterio: str | None,
    razon: str | None,
    caso: Mapping[str, Any],
    transcripcion: Sequence[Mapping[str, Any]],
    contenido: Mapping[str, Any],
    tools: Sequence[str],
) -> str:
    turnos = [
        {
            "n": i,
            "quien": "cliente" if t.get("quien") == "cliente" else "agente",
            "texto": str(t.get("texto", ""))[:MAX_TEXTO_TURNO],
        }
        for i, t in enumerate(transcripcion, start=1)
    ]
    reglas = [
        {"name": g.get("name"), "kind": g.get("kind"), "phrases": g.get("phrases") or [], "tool": g.get("tool")}
        for g in (contenido.get("guardrails") or [])[:MAX_REGLAS_EN_PROMPT]
        if isinstance(g, Mapping)
    ]
    persona = contenido.get("persona") if isinstance(contenido.get("persona"), Mapping) else {}
    return json.dumps(
        {
            "falla": {"criterio_incumplido": criterio or "", "razon": razon or ""},
            "escenario": {
                "titulo": str(caso.get("titulo", ""))[:200],
                "objetivo_cliente": str(caso.get("objetivo", ""))[:2000],
                "criterios": [str(c)[:500] for c in caso.get("criterios", [])],
            },
            "transcripcion": turnos,
            "agente": {
                "reglas": reglas,
                "frases_prohibidas": list(persona.get("forbiddenPhrases") or [])[:50],
                "procedimientos": [
                    str(p.get("id")) for p in (contenido.get("procedures") or []) if isinstance(p, Mapping) and p.get("id")
                ],
                "tools": sorted(set(tools)),
            },
        },
        ensure_ascii=False,
    )


async def proponer(
    tenant_id: uuid.UUID,
    *,
    run_id: uuid.UUID,
    case_id: uuid.UUID,
    caso: Mapping[str, Any],
    criterio: str | None,
    razon: str | None,
    transcripcion: Sequence[Mapping[str, Any]],
    contenido: Mapping[str, Any],
    tools: Sequence[str],
    catalogo: Any,
) -> tuple[Arreglo | None, Decimal]:
    """`(arreglo, créditos gastados)`. `arreglo` es None si la IA no respondió, la salida no validó o el cambio no pasa
    el contrato del borrador; en todos los casos los créditos gastados (0 si no hubo respuesta) se suman a la corrida.
    Nunca levanta por la IA: la falla del agente ya es un hecho y el arreglo es un extra."""
    from core.ia.estructurado import ErrorFormato, ErrorIA, generar_estructurado

    try:
        resultado = await generar_estructurado(
            tenant_id,
            playbook=PLAYBOOK,
            esquema=Arreglo,
            entrada=_entrada(
                criterio=criterio, razon=razon, caso=caso, transcripcion=transcripcion, contenido=contenido, tools=tools
            ),
            kind=KIND,
            result={"concepto": "arreglo", "run_id": str(run_id), "case_id": str(case_id)},
            proveedor=_proveedor,
            key_mode="platform",
            max_tokens=MAX_TOKENS,
            temperatura=TEMPERATURA,
            timeout_s=TIMEOUT_S,
        )
    except ErrorIA as exc:
        gastado = Decimal(exc.consumo.credits or 0) if isinstance(exc, ErrorFormato) else Decimal(0)
        log.warning("prueba_arreglo_sin_propuesta", run_id=str(run_id), motivo=exc.motivo, causa=exc.causa)
        return None, gastado
    gastado = Decimal(resultado.consumo.credits or 0)
    arreglo = resultado.datos
    if arreglo.aplicable:
        regla = arreglo.regla
        if regla is not None and regla.kind == "required_tool" and regla.tool not in set(tools):
            log.warning("prueba_arreglo_tool_ajena", run_id=str(run_id), case_id=str(case_id))
            return None, gastado
        # Que el cambio pase el contrato y el linter del borrador, no solo la forma: una `required_tool` con una tool
        # que el agente no tiene se descarta aquí y no le llega al dueño como algo que no podrá aplicar.
        try:
            nuevo, _ = preparar_arreglo(contenido, arreglo)
            if problemas_contra_el_contrato(nuevo, catalogo):
                log.warning("prueba_arreglo_invalido", run_id=str(run_id), case_id=str(case_id))
                return None, gastado
        except Exception as exc:  # el borrador no se pudo revisar: sin arreglo, la falla sigue
            log.warning("prueba_arreglo_no_revisable", run_id=str(run_id), error=type(exc).__name__)
            return None, gastado
    return arreglo, gastado


__all__ = [
    "Arreglo",
    "ArregloNoAplicable",
    "a_json",
    "leer",
    "preparar_arreglo",
    "problemas_contra_el_contrato",
    "proponer",
]
