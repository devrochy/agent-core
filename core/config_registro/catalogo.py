"""Catálogo de opciones que ve un usuario: filtrado por rol y con las listas del negocio."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from core.config_registro.modelo import Opcion, ValorPosible
from core.config_registro.opciones import OPCIONES, POR_CLAVE


class Catalogo(BaseModel):
    # Huella del contenido: el cliente (APK) sabe si su copia guardada sigue vigente.
    version: str
    opciones: list[Opcion]


def _huella(opciones: list[Opcion]) -> str:
    cuerpo = json.dumps([o.model_dump(mode="json") for o in opciones], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(cuerpo.encode()).hexdigest()[:12]


# Los interruptores del verificador del core (ADR-0119) los explica el core; los guardrails de cada
# pack traen su propio texto al registrarse (`register_guardrail(..., etiqueta=, explicacion=)`).
# Es texto para personas, nunca entra al contexto del modelo (invariante 3).
INFO_VERIFICADOR: dict[str, tuple[str, str]] = {
    "verificador_cierre": (
        "Verificador de cierres",
        "Evita que el asistente diga que ya hizo algo (confirmó un pedido, envió un link de pago) si ninguna acción lo respalda.",
    ),
    "verificador_links": (
        "Verificador de enlaces",
        "Quita de la respuesta los enlaces que el asistente no recibió de una acción ni de la conversación.",
    ),
    "verificador_montos": (
        "Verificador de montos",
        "Evita que el asistente diga una cifra de dinero distinta de las que le devolvieron sus acciones.",
    ),
}


def catalogo_para(
    *,
    es_master: bool,
    guardrails_disponibles: list[str],
    guardrails_info: dict[str, tuple[str, str]] | None = None,
) -> Catalogo:
    """Las opciones que puede ver el usuario, con los `valores` que dependen del negocio ya puestos.

    - Las del master (`editable_por="master"`) solo se incluyen si `es_master`.
    - `disabled_guardrails` lista los guardrails de los packs habilitados del negocio más los
      interruptores del verificador (`_available_guardrails` en el router de configuración).
    """
    info = {**INFO_VERIFICADOR, **{k: v for k, v in (guardrails_info or {}).items() if v[0] or v[1]}}
    visibles: list[Opcion] = []
    for opcion in OPCIONES:
        if opcion.editable_por == "master" and not es_master:
            continue
        if opcion.valores_calculados:
            opcion = opcion.model_copy(
                update={
                    "valores": [
                        ValorPosible(valor=g, etiqueta=info.get(g, (g, ""))[0] or g, nota=info.get(g, (g, ""))[1])
                        for g in sorted(set(guardrails_disponibles))
                    ]
                }
            )
        # Copia propia: lo que sale al cliente nunca comparte listas con el registro global.
        visibles.append(opcion if opcion.valores_calculados else opcion.model_copy(deep=True))
    return Catalogo(version=_huella(visibles), opciones=visibles)


def opcion(clave: str) -> Opcion:
    """La opción registrada con esa clave (KeyError si no existe)."""
    return POR_CLAVE[clave]
