"""Conexión con su release 1 para el negocio de un test (ADR-0191).

Todo turno corre sobre la release v3 de su conexión: un test que arma su negocio y sus
conversaciones a mano las ata a la conexión que monta aquí, con el mismo camino que los journeys
(`evals/journeys.py::encender_runtime_v3`)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

_RAIZ = Path(__file__).resolve().parents[2]


def _journeys() -> Any:
    if "journeys" not in sys.modules:
        spec = importlib.util.spec_from_file_location("journeys", _RAIZ / "evals" / "journeys.py")
        assert spec and spec.loader
        modulo = importlib.util.module_from_spec(spec)
        sys.modules["journeys"] = modulo
        spec.loader.exec_module(modulo)
    return sys.modules["journeys"]


async def montar_release(tenant_id: uuid.UUID) -> uuid.UUID:
    """Conexión conversacional del negocio con su release 1 activa; deja el negocio en v3."""
    return await _journeys().encender_runtime_v3(tenant_id)


def conexion_de(tenant_id: uuid.UUID) -> uuid.UUID | None:
    """La conexión que montó `montar_release` para el negocio (None si no se montó)."""
    return _journeys()._CONEXION_V3.get(tenant_id)
