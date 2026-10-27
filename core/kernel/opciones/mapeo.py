"""Mapeo puro de una `Definicion` a lo que sale por WhatsApp (SPEC-40, ADR-0172 §2).

- Mensaje interactivo (ventana de 24 h abierta): 3 opciones o menos → `button`; de 4 a 10 → `list`.
  La forma es la que valida `sender.interactivo_valido` (`type`, `body`, `buttons` | `button` + `rows`).
- Plantilla de Meta (fuera de la ventana): BODY, FOOTER y botones QUICK_REPLY. Cada `{ruta}` del cuerpo
  pasa a un parámetro NAMED (`{{cliente_nombre}}`) y el resultado se valida con
  `meta_templates.validate_template_components`. El render y los parámetros de envío reutilizan
  `event_notices.render_body` y `build_components` con un `MessageTemplate` transitorio (nunca se persiste).
- Texto de respaldo: cuerpo + lista de opciones. Sin botones no hay ejecución directa (D-SPEC-40-6).

El payload de cada botón (`opt:…`) lo arma quien envía (necesita la conversación y el envío) y llega aquí
como un mapa `option_id → payload`. Los `parameters` de tipo `payload` del QUICK_REPLY al enviar son del
PR de envío: aquí solo se declara el texto de cada botón, que es lo único que Meta pide al crear.

Puro: ni base, ni red, ni entorno.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.kernel.opciones.definicion import MAX_BOTONES, Definicion, llaves_de

TEXTO_BOTON_LISTA = "Ver opciones"
EJEMPLO_POR_DEFECTO = "ejemplo"
_LLAVE = re.compile(r"\{([^{}]*)\}")


def variables_del_cuerpo(cuerpo: str) -> list[str]:
    """Rutas de variable del cuerpo, en orden de aparición y sin repetir."""
    vistas: list[str] = []
    for ruta in llaves_de(cuerpo):
        if ruta not in vistas:
            vistas.append(ruta)
    return vistas


def nombre_de_parametro(ruta: str) -> str:
    """`cliente.nombre` → `cliente_nombre`: el nombre NAMED que Meta acepta (minúsculas, números, _)."""
    return ruta.replace(".", "_")


def renderizar(texto: str, valores: Mapping[str, str]) -> str:
    """Sustituye cada `{ruta}` por su valor saneado; una ruta sin valor queda como está."""
    from core.gateway.event_notices import clean_param

    return _LLAVE.sub(lambda m: clean_param(valores[m.group(1)]) if m.group(1) in valores else m.group(0), texto)


# ------------------------------------------------------------------ interactivo
def a_interactivo(definicion: Definicion, cuerpo: str, payloads: Mapping[str, str]) -> dict[str, Any]:
    """Payload interactivo de la definición. `cuerpo` ya viene renderizado; `payloads` da el id firmado de
    cada opción. Lanza `KeyError` si falta el de una opción (no se envía un botón sin payload)."""
    opciones = definicion.opciones
    interactivo: dict[str, Any]
    if len(opciones) <= MAX_BOTONES:
        interactivo = {
            "type": "button",
            "body": cuerpo,
            "buttons": [{"id": payloads[o.id], "title": o.titulo} for o in opciones],
        }
    else:
        interactivo = {
            "type": "list",
            "body": cuerpo,
            "button": TEXTO_BOTON_LISTA,
            "rows": [{"id": payloads[o.id], "title": o.titulo} for o in opciones],
        }
    if definicion.footer:
        interactivo["footer"] = definicion.footer
    return interactivo


def texto_de_respaldo(definicion: Definicion, cuerpo: str) -> str:
    """Canal sin botones o payload interactivo fuera de topes: el cuerpo y las opciones numeradas. Nada de esto se
    ejecuta por lo que escriba el cliente (el número que responda llega como texto al agente)."""
    lineas = [cuerpo.strip(), ""]
    lineas += [f"{i}. {o.titulo}" for i, o in enumerate(definicion.opciones, 1)]
    return "\n".join(lineas).strip()


# ------------------------------------------------------------------ plantilla de Meta
def cuerpo_para_meta(cuerpo: str) -> str:
    """`Hola {cliente.nombre}` → `Hola {{cliente_nombre}}`."""
    return _LLAVE.sub(lambda m: "{{" + nombre_de_parametro(m.group(1)) + "}}", cuerpo)


def componentes_meta(definicion: Definicion, ejemplos: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Componentes para `meta_templates.create_template` (parameter_format NAMED). `ejemplos` da el valor
    de muestra de cada ruta que Meta exige al revisar la plantilla."""
    rutas = variables_del_cuerpo(definicion.body)
    cuerpo: dict[str, Any] = {"type": "BODY", "text": cuerpo_para_meta(definicion.body)}
    if rutas:
        cuerpo["example"] = {
            "body_text_named_params": [
                {"param_name": nombre_de_parametro(r), "example": (ejemplos or {}).get(r, EJEMPLO_POR_DEFECTO)} for r in rutas
            ]
        }
    componentes: list[dict[str, Any]] = [cuerpo]
    if definicion.footer:
        componentes.append({"type": "FOOTER", "text": definicion.footer})
    componentes.append({"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": o.titulo} for o in definicion.opciones]})
    return componentes


def plantilla_transitoria(definicion: Definicion, ejemplos: Mapping[str, str] | None = None) -> Any:
    """`MessageTemplate` en memoria (nunca se añade a una sesión) para reutilizar `event_notices.render_body`
    y `build_components`, que reciben la fila de Meta."""
    from core.db.models import MessageTemplate

    return MessageTemplate(components=componentes_meta(definicion, ejemplos), parameter_format="NAMED")


def valores_para_meta(definicion: Definicion, valores: Mapping[str, str]) -> dict[str, str]:
    """`ruta → valor` a `parámetro NAMED → valor`, saneado con `clean_param` (`render_body` no sanea).
    `KeyError` si falta el valor de una variable del cuerpo."""
    from core.gateway.event_notices import clean_param

    return {nombre_de_parametro(r): clean_param(valores[r]) for r in variables_del_cuerpo(definicion.body)}


def parametros_de_cuerpo_meta(definicion: Definicion, valores: Mapping[str, str]) -> list[dict[str, Any]]:
    """El componente `body` de un envío de plantilla (`type: "body"`, parámetros NAMED saneados)."""
    from core.gateway.event_notices import build_components

    return build_components(plantilla_transitoria(definicion), valores_para_meta(definicion, valores))


def cuerpo_de_plantilla_renderizado(definicion: Definicion, valores: Mapping[str, str]) -> str:
    """Lo que ve el cliente en la plantilla (para el panel y para guardar `content`)."""
    from core.gateway.event_notices import render_body

    return render_body(plantilla_transitoria(definicion), valores_para_meta(definicion, valores))
