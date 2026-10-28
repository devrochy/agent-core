"""Aviso de tratamiento de datos del cliente (HU-CRM-16, ADR-0179 decisión 8).

El texto lo escribe el negocio (o usa el borrador del kernel, marcado «sin validar legalmente»). Apagado
por defecto. La versión es una huella corta del texto: cambiar el texto vuelve a pedir la respuesta.
Este módulo no afirma cumplimiento de ninguna ley: activar el aviso en un negocio real exige la
revisión del abogado (P-37).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from core.datos_cliente.ajustes import aviso_de


@dataclass(frozen=True)
class Aviso:
    activo: bool
    texto: str
    version: str
    es_borrador: bool  # usa el texto del kernel, sin validar legalmente


def borrador() -> str:
    from core.agents.playbooks import load_core_playbook

    contenido = load_core_playbook("aviso-datos")
    return contenido.split("## borrador", 1)[1].strip()


def version_de(texto: str) -> str:
    return hashlib.sha256(texto.strip().encode("utf-8")).hexdigest()[:12]


def desde(behavior: dict[str, Any] | None) -> Aviso:
    guardado = aviso_de(behavior)
    texto = guardado["texto"].strip()
    es_borrador = not texto
    if es_borrador:
        texto = borrador()
    return Aviso(activo=guardado["activo"], texto=texto, version=version_de(texto), es_borrador=es_borrador)
