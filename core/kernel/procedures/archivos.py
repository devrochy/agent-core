"""Procedimientos que una habilidad publica como datos (HU-AOS-07, ADR-0113 §1, ADR-0118 §1).

Un procedimiento `procedure/1` es dato de la habilidad, no código (invariante 3): vive en
`packages/pack-*/procedures/*.json`, un archivo por procedimiento. Este es el único lector:

- `leer_crudos(carpeta)`: el JSON tal cual, ordenado por nombre de archivo. Lo usa el manifiesto
  de la versión del pack (`core/releases/packs.py`), así lo que se congela en `pack_versions` es
  exactamente lo que se registra. Un JSON roto lanza (el CI del lock lo detecta).
- `leer_procedimientos(carpeta)`: los mismos, validados contra el contrato. Lo usa la habilidad
  en `on_activate` (`MCPHost.register_procedures`). Un archivo inválido se salta con un log: un
  procedimiento mal escrito no puede dejar sin tools a todos los negocios del proceso (el linter
  completo corre en CI y en runtime, `registro.py`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentcore_contracts import Procedure
from pydantic import ValidationError

from core.shared.logger import get_logger

log = get_logger(__name__)

CARPETA = "procedures"
# Tope por archivo: un procedimiento es una definición, no un documento (igual que el linter).
MAX_BYTES = 64 * 1024


def _archivos(carpeta: Path | None) -> list[Path]:
    if carpeta is None or not carpeta.is_dir():
        return []
    return sorted(carpeta.glob("*.json"))


def _leer(archivo: Path) -> Any:
    texto = archivo.read_text(encoding="utf-8")
    if len(texto.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"procedimiento demasiado grande: {archivo.name}")
    return json.loads(texto)


def leer_crudos(carpeta: Path | None) -> list[Any]:
    """El JSON de cada `procedures/*.json`, en orden de archivo. Lanza si uno no es JSON."""
    return [_leer(archivo) for archivo in _archivos(carpeta)]


def leer_procedimientos(carpeta: Path | None) -> list[Procedure]:
    """Los procedimientos válidos de la carpeta; los inválidos quedan en el log."""
    salida: list[Procedure] = []
    for archivo in _archivos(carpeta):
        try:
            salida.append(Procedure.model_validate(_leer(archivo)))
        except (OSError, ValueError, ValidationError) as exc:
            log.error("procedimiento_archivo_invalido", archivo=archivo.name, error=type(exc).__name__)
    return salida
