"""Guardrails y tools declarativas de un agente desde su editor (HU-AOS-97, D-78 decisión 4).

Reemplaza las pantallas de Configuración del AgentSpec v2 que retiró HU-AOS-91: lo que editaban vive ahora en cada
agente (`AgentSpecV3.guardrails` y `.declarativeTools`) y el turno lo aplica desde la release (HU-AOS-90).

Funciones puras: arman el contenido candidato del borrador y lo validan sin tocar la base. La ruta escribe después,
en este orden, para que un 422 no deje nada guardado:

1. `preparar_declarativas` valida el contrato y el linter (literales en headers sensibles, dominios internos,
   nombres de acciones del binario, https con secretos), que cada referencia a un secreto tenga su valor nuevo o
   uno ya configurado que cubra los dominios de la tool, y que cada valor nuevo lo use alguna tool.
2. `guardar_secretos_declarativos` (`core/agents/secretos.py`) cifra los valores nuevos en la instalación `core`.
3. `versiones.guardar_borrador` guarda el borrador (vuelve a validar, ya con los secretos).

**Dominios de un secreto que se escribe:** los que ya tenía más los `allowedDomains` de las tools de este agente que
lo usan. Los secretos son del negocio (los comparten sus agentes): reemplazarlos por los dominios de un solo agente
rompería las tools de los demás. Sumar un dominio exige escribir el valor, así que nadie que no lo conozca puede
apuntar una referencia a un dominio suyo para sacarlo (ADR-0113 §2).

Una tool con referencias a secretos en la URL o en el body se guarda, pero el turno no la ofrece (ADR-0206 §2): se
devuelve en `omitidas` para que el editor avise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentcore_contracts import AgentSpecV3
from pydantic import SecretStr

from core.agents.contrato_v3 import revisar_spec
from core.agents.credenciales import referencias_de_tool, referencias_en_url
from core.agents.secretos import (
    SECRET_NAME_RE,
    SecretoDeclarativo,
    SecretoInvalido,
    validar_secretos,
)
from core.agents.turno import declarativas_omitidas
from core.agents.versiones import ErroresDeContrato
from core.kernel.procedures import Catalogo, Issue
from core.mcp.types import public_fqdn

CONTRATO = "agentspec/3"


@dataclass(frozen=True)
class DeclarativasPreparadas:
    contenido: dict[str, Any]
    secretos: dict[str, SecretoDeclarativo]
    omitidas: list[str]


def _normalizar_dominios(tools: Sequence[Any]) -> list[Any]:
    """Los dominios públicos en minúsculas y sin punto final, como los guarda el secreto: así `API.X.co` cubre
    `api.x.co`. Un dominio que no es público se deja tal cual para que el linter lo marque en su campo."""
    salida: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("allowedDomains"), list):
            dominios = [
                (public_fqdn(d) or d) if isinstance(d, str) else d for d in tool["allowedDomains"]
            ]
            tool = {**tool, "allowedDomains": dominios}
        salida.append(tool)
    return salida


def _ruta_de_referencia(i: int, tool: Any, nombre: str) -> str:
    """El campo donde está la referencia (para el error por campo del editor)."""
    for header, valor in tool.headers.items():
        if not isinstance(valor, str) and valor.secret == nombre:
            return f"declarativeTools[{i}].headers.{header}"
    if nombre in referencias_en_url(tool.url):
        return f"declarativeTools[{i}].url"
    return f"declarativeTools[{i}].body"


def preparar_guardrails(contenido: Mapping[str, Any], guardrails: Sequence[Any]) -> dict[str, Any]:
    """El contenido del borrador con las reglas nuevas. Lo valida `guardar_borrador` (contrato y linter: la acción
    de `required_tool` tiene que existir en el binario o entre las declarativas del agente)."""
    return {**dict(contenido), "guardrails": list(guardrails)}


def preparar_declarativas(
    contenido: Mapping[str, Any],
    tools: Sequence[Any],
    secretos: Mapping[str, SecretStr],
    configurados: Mapping[str, Sequence[str]],
    catalogo: Catalogo,
) -> DeclarativasPreparadas:
    """Valida las tools y los secretos nuevos sin escribir nada. Lanza `ErroresDeContrato` con un `Issue` por campo
    (`declarativeTools[i]...` o `secrets.<nombre>`); los mensajes nunca llevan valores de secretos."""
    candidato = {**dict(contenido), "declarativeTools": _normalizar_dominios(tools)}
    issues = revisar_spec(candidato, catalogo, CONTRATO)
    if issues:
        raise ErroresDeContrato(issues)
    spec = AgentSpecV3.model_validate(candidato)

    usados: dict[str, set[str]] = {}
    for i, tool in enumerate(spec.declarativeTools):
        for nombre in sorted(referencias_de_tool(tool)):
            usados.setdefault(nombre, set()).update(tool.allowedDomains)
            if nombre in secretos:
                continue
            if nombre not in configurados:
                issues.append(
                    Issue(
                        "secreto_inexistente",
                        _ruta_de_referencia(i, tool, nombre),
                        f"El secreto '{nombre}' no está configurado: escribe su valor.",
                    )
                )
            elif not set(tool.allowedDomains) <= set(configurados[nombre]):
                issues.append(
                    Issue(
                        "secreto_fuera_de_dominio",
                        f"declarativeTools[{i}].allowedDomains",
                        f"El secreto '{nombre}' no puede viajar a todos estos dominios: vuelve a escribir su valor "
                        "para sumarlos.",
                    )
                )

    nuevos: dict[str, SecretoDeclarativo] = {}
    for nombre, valor in secretos.items():
        campo = f"secrets.{nombre[:60]}"
        if not SECRET_NAME_RE.fullmatch(nombre):
            issues.append(Issue("secreto_invalido", campo, "Nombre de secreto inválido: minúsculas, números y _."))
            continue
        if nombre not in usados:
            issues.append(Issue("secreto_sin_uso", campo, f"Ninguna tool usa el secreto '{nombre}'."))
            continue
        secreto = SecretoDeclarativo(valor, tuple(sorted(usados[nombre] | set(configurados.get(nombre, ())))))
        try:
            validar_secretos({nombre: secreto})
        except SecretoInvalido as exc:
            issues.append(Issue("secreto_invalido", campo, str(exc)))
            continue
        nuevos[nombre] = secreto
    if issues:
        raise ErroresDeContrato(issues)
    return DeclarativasPreparadas(
        contenido=spec.model_dump(mode="json", by_alias=True), secretos=nuevos, omitidas=declarativas_omitidas(spec)
    )


__all__ = ["DeclarativasPreparadas", "preparar_declarativas", "preparar_guardrails"]
