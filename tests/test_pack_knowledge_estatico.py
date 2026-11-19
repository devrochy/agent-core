"""El pack de conocimiento no lee el entorno ni sale a la red por su cuenta (ADR-0145 §3 y §7;
invariante nº6): los embeddings los pide el servicio del core y la configuración llega por el
`ToolContext` o por el core."""

from __future__ import annotations

import ast
from pathlib import Path

PACK = Path(__file__).resolve().parents[1] / "packages" / "pack-knowledge" / "pack_knowledge"
PROHIBIDOS_IMPORT = {"httpx", "requests", "aiohttp", "urllib.request", "core.config"}


def _archivos() -> list[Path]:
    archivos = sorted(PACK.rglob("*.py"))
    assert archivos, PACK
    return archivos


def test_sin_entorno_ni_settings_ni_clientes_http() -> None:
    hallazgos: list[str] = []
    for archivo in _archivos():
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                hallazgos += [f"{archivo.name}: import {a.name}" for a in nodo.names if a.name in PROHIBIDOS_IMPORT]
            elif isinstance(nodo, ast.ImportFrom) and nodo.module in PROHIBIDOS_IMPORT:
                hallazgos.append(f"{archivo.name}: from {nodo.module}")
            elif isinstance(nodo, ast.Attribute) and nodo.attr in ("environ", "getenv"):
                hallazgos.append(f"{archivo.name}:{nodo.lineno} os.{nodo.attr}")
            elif isinstance(nodo, ast.Name) and nodo.id == "get_settings":
                hallazgos.append(f"{archivo.name}:{nodo.lineno} get_settings")
    assert hallazgos == []
