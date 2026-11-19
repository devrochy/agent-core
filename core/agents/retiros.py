"""Registro de retiros: lo que el binario dejó de traer y qué lo reemplaza (HU-AOS-59, ADR-0184).

Una versión de agente publicada y una release construida guardan nombres de acciones y de
procedimientos. Cuando una versión del código retira uno, esos agentes y releases siguen
nombrándolo: el build falla con `contrato_invalido` y el agente apunta a algo que no existe.
Este registro es parte del contrato del catálogo: **quitar una acción o un procedimiento del
binario exige una entrada aquí** (`tests/test_catalogo_retiros.py` lo hace cumplir contra la
instantánea `packages/catalogo-nombres.lock.json`).

Cada entrada dice qué pasa con quien todavía lo nombra:

- `reemplazo` con id y rango de versión: `migrar-referencias` cambia la referencia por el
  procedimiento nuevo, solo si el negocio tiene su habilidad;
- `reemplazo=None` («quitar»): `migrar-referencias` retira la referencia.

Las entradas son historia: no se borran al pasar el tiempo (un agente publicado hace un año puede
seguir nombrando lo retirado). Puro: sin base, red ni entorno.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentcore_contracts.agentspec import VERSION_RANGE_PATTERN
from agentcore_contracts.procedure import ACTION_PATTERN, PROCEDURE_ID_PATTERN

TipoDeNombre = Literal["accion", "procedimiento"]


@dataclass(frozen=True)
class Reemplazo:
    """Procedimiento que ocupa el lugar del retirado: `id` y rango de versión (`^2.0`)."""

    id: str
    version: str

    def __post_init__(self) -> None:
        if not re.fullmatch(PROCEDURE_ID_PATTERN, self.id):
            raise ValueError(f"el id del reemplazo no es un id de procedimiento: {self.id!r}")
        if not re.fullmatch(VERSION_RANGE_PATTERN, self.version):
            raise ValueError(f"la versión del reemplazo no es un rango válido: {self.version!r}")

    def __str__(self) -> str:
        return f"{self.id} {self.version}"


@dataclass(frozen=True)
class Retiro:
    tipo: TipoDeNombre
    nombre: str
    # Cuándo y por qué se retiró (para el informe y para quien revise el registro).
    retirado_el: str
    origen: str
    reemplazo: Reemplazo | None = None

    def __post_init__(self) -> None:
        patron = ACTION_PATTERN if self.tipo == "accion" else PROCEDURE_ID_PATTERN
        if not re.fullmatch(patron, self.nombre):
            raise ValueError(f"nombre inválido para {self.tipo}: {self.nombre!r}")
        if self.reemplazo is not None and self.tipo != "procedimiento":
            # Una acción suelta no tiene equivalente declarable: se quita.
            raise ValueError(f"solo un procedimiento puede tener reemplazo ({self.nombre})")
        if self.reemplazo is not None and self.reemplazo.id == self.nombre:
            raise ValueError(f"{self.nombre} no puede reemplazarse por sí mismo")

    @property
    def destino(self) -> str:
        """Lo que hará la migración, en palabras para la consola."""
        return f"reemplazar por {self.reemplazo}" if self.reemplazo else "quitar"


# El orden es cronológico. Agrega al final; no borres ni reescribas.
RETIROS: tuple[Retiro, ...] = (
    # SPEC-43 PR-4 (2026-11-03): los planes salieron de pack_commerce y pasaron a pack_subscriptions.
    Retiro(
        tipo="procedimiento",
        nombre="pack_commerce.contratar_plan",
        retirado_el="2026-11-03",
        origen="SPEC-43 PR-4",
        reemplazo=Reemplazo("pack_subscriptions.contratar_plan", "^2.0"),
    ),
    Retiro(
        tipo="accion",
        nombre="informar_plan",
        retirado_el="2026-11-03",
        origen="SPEC-43 PR-4",
    ),
)


def _indice(retiros: tuple[Retiro, ...]) -> dict[tuple[TipoDeNombre, str], Retiro]:
    indice: dict[tuple[TipoDeNombre, str], Retiro] = {}
    for retiro in retiros:
        clave = (retiro.tipo, retiro.nombre)
        if clave in indice:
            raise ValueError(f"entrada repetida en el registro de retiros: {retiro.tipo} {retiro.nombre}")
        indice[clave] = retiro
    return indice


_POR_NOMBRE = _indice(RETIROS)


def retiro_de(tipo: TipoDeNombre, nombre: str) -> Retiro | None:
    """La entrada del registro para ese nombre, o None si nadie lo declaró retirado."""
    return _POR_NOMBRE.get((tipo, nombre))


# ------------------------------------------------------------------ lock del CI
# Instantánea de los nombres que el binario trae (acciones y procedimientos de la plataforma). Vive
# junto a `pack-versions.lock.json`; `scripts/catalogo_nombres_lock.py` la regenera.
LOCK_PATH = Path(__file__).resolve().parent.parent.parent / "packages" / "catalogo-nombres.lock.json"
_CLAVES: tuple[tuple[str, TipoDeNombre], ...] = (("acciones", "accion"), ("procedimientos", "procedimiento"))


def leer_lock(path: Path = LOCK_PATH) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def desaparecidos(lock: Mapping[str, Sequence[str]], actual: Mapping[str, Sequence[str]]) -> list[tuple[TipoDeNombre, str]]:
    """Nombres de la instantánea que el binario de hoy ya no trae."""
    return [
        (tipo, nombre)
        for clave, tipo in _CLAVES
        for nombre in sorted(set(lock.get(clave, ())) - set(actual.get(clave, ())))
    ]


def problemas_del_lock(
    lock: Mapping[str, Sequence[str]],
    actual: Mapping[str, Sequence[str]],
    retiros: tuple[Retiro, ...] = RETIROS,
) -> list[str]:
    """Qué no cuadra entre el binario, la instantánea y el registro (vacío si todo está bien)."""
    indice = _indice(retiros)
    problemas: list[str] = []
    for tipo, nombre in desaparecidos(lock, actual):
        if (tipo, nombre) not in indice:
            problemas.append(
                f"{tipo} '{nombre}' ya no está en el binario y no tiene entrada en el registro de retiros: "
                "agrégala en core/agents/retiros.py (con reemplazo o sin él, «quitar»)"
            )
    for clave, tipo in _CLAVES:
        for nombre in sorted(set(actual.get(clave, ())) - set(lock.get(clave, ()))):
            problemas.append(
                f"{tipo} '{nombre}' es nuevo y no está en la instantánea: corre `python scripts/catalogo_nombres_lock.py`"
            )
    for retiro in retiros:
        clave = "acciones" if retiro.tipo == "accion" else "procedimientos"
        if retiro.nombre in actual.get(clave, ()):
            problemas.append(
                f"{retiro.tipo} '{retiro.nombre}' está en el registro de retiros pero el binario todavía lo trae"
            )
        if retiro.reemplazo is not None and retiro.reemplazo.id not in actual.get("procedimientos", ()):
            problemas.append(f"el reemplazo '{retiro.reemplazo.id}' de '{retiro.nombre}' no existe en el binario")
    return problemas


__all__ = [
    "LOCK_PATH",
    "RETIROS",
    "Reemplazo",
    "Retiro",
    "TipoDeNombre",
    "desaparecidos",
    "leer_lock",
    "problemas_del_lock",
    "retiro_de",
]
