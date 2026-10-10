"""Editor de tools declarativas sin secretos en claro (ADR-0052, ADR-0113 §2, HU-AOS-12).

Lógica única de los dos editores (`/api/v1/admin/settings/declarative-tools` y el SSR que se
retira): lo que se guarda en `extensions.tools` del AgentSpec v2 activo (lo que lee el loop
legacy) ya no lleva valores literales en los headers sensibles, sino `{"secret": "<nombre>"}`,
y el valor queda cifrado en la instalación `core` (`core.agents.secretos`).

- `enmascarar(tools)`: lo que se muestra. Un header sensible literal (de un spec v2 viejo) sale
  como `MASCARA`; nunca se devuelve un valor.
- `preparar(...)`: valida lo que llega y arma lo que se guarda:
  - un header sensible con un valor literal se rechaza; con `MASCARA`, el literal que ya estaba
    guardado se migra a un secreto (así se puede editar una tool vieja sin reescribir su clave);
  - una referencia exige el secreto configurado o enviado en la misma petición, y que los
    dominios de la tool sean un subconjunto de aquellos a los que el secreto puede viajar;
  - cada secreto nuevo queda atado a los dominios de las tools que lo usan;
  - cada tool pasa además por el contrato `DeclarativeTool` de `agentspec/3` (el mismo
    validador del CLI y de CI), y su nombre no puede sombrear una acción del binario.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentcore_contracts import SENSITIVE_HEADERS, DeclarativeTool
from pydantic import SecretStr, ValidationError

from core.agents.secretos import SECRET_NAME_RE, SecretoDeclarativo
from core.mcp.declarative import rejected_domains, secret_ref, validate_declarative_tools

MASCARA = "••••••••"
MAX_TOOLS = 50
_SECRET_UNSAFE_RE = re.compile(r"[^a-z0-9_]+")


def _sensible(header: str) -> bool:
    return header.lower() in SENSITIVE_HEADERS


def enmascarar(tools: Any) -> list[dict[str, Any]]:
    """Las tools del spec para mostrarlas: headers sensibles literales → `MASCARA`."""
    salida: list[dict[str, Any]] = []
    for tool in validate_declarative_tools(tools):
        headers = {
            k: (MASCARA if _sensible(k) and secret_ref(v) is None else v) for k, v in tool["headers"].items()
        }
        salida.append(tool | {"headers": headers})
    return salida


def literales_sin_migrar(tools: Any) -> list[str]:
    """`tool.header` de los headers sensibles que todavía tienen un valor literal guardado."""
    return [
        f"{t['name']}.{k}"
        for t in validate_declarative_tools(tools)
        for k, v in t["headers"].items()
        if _sensible(k) and secret_ref(v) is None
    ]


@dataclass
class Preparadas:
    tools: list[dict[str, Any]] = field(default_factory=list)
    secretos: dict[str, SecretoDeclarativo] = field(default_factory=dict)
    quitar: list[str] = field(default_factory=list)
    errores: dict[str, str] = field(default_factory=dict)


def _nombre_para(tool: str, header: str, usados: set[str]) -> str:
    base = _SECRET_UNSAFE_RE.sub("_", f"{tool}_{header}".lower()).strip("_")[:52] or "secreto"
    nombre, n = base, 2
    while nombre in usados:
        nombre, n = f"{base}_{n}", n + 1
    usados.add(nombre)
    return nombre


def _a_contrato(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "method": tool["method"],
        "url": tool["url"],
        "allowedDomains": tool["allowed_domains"],
        "headers": tool["headers"],
        "parameters": tool["parameters"],
        "body": tool["body"],
    }


def preparar(
    nuevas: Any,
    actuales: Any,
    secretos_enviados: Mapping[str, str | None],
    configurados: Mapping[str, list[str]],
    acciones_del_binario: set[str],
) -> Preparadas:
    """Valida el JSON del editor y arma tools con referencias más los secretos a guardar.
    Nunca pone un valor en `errores`."""
    res = Preparadas()
    if not isinstance(nuevas, list):
        res.errores["tools"] = "Se espera una lista de tools."
        return res
    if len(nuevas) > MAX_TOOLS:
        res.errores["tools"] = f"Máximo {MAX_TOOLS} tools declarativas."
        return res
    if rejected_domains(nuevas):
        res.errores["tools"] = "allowed_domains no permitidos: usa dominios públicos (ni IPs ni nombres internos)."
        return res
    normalizadas = validate_declarative_tools(nuevas)
    if len(normalizadas) != len(nuevas):
        res.errores["tools"] = (
            f"{len(nuevas) - len(normalizadas)} tool(s) inválida(s): cada una necesita name "
            "(letras, números y _), url http(s) y method GET o POST."
        )
        return res

    guardadas = {t["name"]: t for t in validate_declarative_tools(actuales)}
    usados = set(configurados) | {n for n in secretos_enviados if n}
    nuevos: dict[str, str] = {}
    for nombre, valor in secretos_enviados.items():
        if not SECRET_NAME_RE.fullmatch(nombre):
            res.errores[f"secrets.{nombre[:60]}"] = "Nombre inválido: minúsculas, números y _ (hasta 60)."
        elif valor is None:
            res.quitar.append(nombre)
        else:
            nuevos[nombre] = valor
    dominios_por_secreto: dict[str, set[str]] = {}
    vistos: set[str] = set()

    for i, tool in enumerate(normalizadas):
        ruta = f"tools[{i}]"
        if tool["name"] in vistos:
            res.errores[f"{ruta}.name"] = f"La tool '{tool['name']}' está repetida."
            continue
        vistos.add(tool["name"])
        if tool["name"] in acciones_del_binario:
            res.errores[f"{ruta}.name"] = f"'{tool['name']}' es una acción del binario: la declarativa nunca correría."
            continue
        headers: dict[str, Any] = {}
        errores_previos = False
        for header, valor in tool["headers"].items():
            ref = secret_ref(valor)
            if ref is None and _sensible(header):
                guardado = next(
                    (
                        v
                        for k, v in (guardadas.get(tool["name"], {}).get("headers") or {}).items()
                        if k.lower() == header.lower() and secret_ref(v) is None
                    ),
                    None,
                )
                dominios_guardados = set(guardadas.get(tool["name"], {}).get("allowed_domains") or [])
                if valor == MASCARA and guardado is not None:
                    if not set(tool["allowed_domains"]) <= dominios_guardados:
                        # Sin el valor, la máscara no puede llevar el secreto a un dominio nuevo.
                        res.errores[f"{ruta}.headers.{header}"] = (
                            "El valor guardado no puede viajar a los dominios nuevos de la tool: vuelve a escribirlo."
                        )
                        errores_previos = True
                        continue
                    # El literal guardado de un spec v2 viejo pasa a un secreto.
                    ref = _nombre_para(tool["name"], header, usados)
                    nuevos[ref] = str(guardado)
                else:
                    res.errores[f"{ruta}.headers.{header}"] = (
                        'Este header no admite valores literales: decláralo como {"secret": "<nombre>"} '
                        "y envía el valor en `secrets`."
                    )
                    errores_previos = True
                    continue
            if ref is not None:
                if ref in res.quitar:
                    res.errores[f"{ruta}.headers.{header}"] = f"El secreto '{ref}' se está quitando y esta tool lo usa."
                    errores_previos = True
                    continue
                if ref not in nuevos and ref not in configurados:
                    res.errores[f"{ruta}.headers.{header}"] = f"El secreto '{ref}' no está configurado: envía su valor."
                    errores_previos = True
                    continue
                if ref not in nuevos and not set(tool["allowed_domains"]) <= set(configurados[ref]):
                    res.errores[f"{ruta}.headers.{header}"] = (
                        f"El secreto '{ref}' no puede viajar a los dominios nuevos de la tool: vuelve a escribirlo."
                    )
                    errores_previos = True
                    continue
                dominios_por_secreto.setdefault(ref, set()).update(tool["allowed_domains"])
                headers[header] = {"secret": ref}
            else:
                headers[header] = valor
        if errores_previos:
            continue
        tool = tool | {"headers": headers}
        try:
            DeclarativeTool.model_validate(_a_contrato(tool))
        except ValidationError as exc:
            for error in exc.errors(include_url=False, include_input=False):
                campo = ".".join(str(p) for p in error["loc"] if p not in ("str", "SecretRef"))
                res.errores.setdefault(f"{ruta}.{campo}" if campo else ruta, str(error["msg"]).removeprefix("Value error, "))
            continue
        res.tools.append(tool)

    for nombre, valor in nuevos.items():
        dominios = dominios_por_secreto.get(nombre)
        if not dominios:
            res.errores[f"secrets.{nombre}"] = "Ninguna tool usa este secreto (o sus tools no tienen allowed_domains)."
            continue
        res.secretos[nombre] = SecretoDeclarativo(valor=SecretStr(valor), dominios=tuple(sorted(dominios)))
    return res


__all__ = ["MASCARA", "Preparadas", "enmascarar", "literales_sin_migrar", "preparar"]
