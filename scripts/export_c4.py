"""Regenera los diagramas Mermaid desde el modelo C4 canónico (ADR-0189).

Uso:
    python scripts/export_c4.py [archivo.md ...]

Valida `docs/architecture/workspace.dsl` y reemplaza, en `docs/architecture/c4.md` y en
los archivos indicados, cada bloque entre `<!-- c4:VISTA -->` y `<!-- /c4:VISTA -->`.
Requiere el MCP de Structurizr; ver cómo levantarlo en `docs/architecture/c4.md`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DSL = ROOT / "docs" / "architecture" / "workspace.dsl"
C4_MD = ROOT / "docs" / "architecture" / "c4.md"
MCP_URL = os.environ.get("STRUCTURIZR_MCP_URL", "http://127.0.0.1:3333/mcp")

BLOQUE = re.compile(r"(<!-- c4:([\w-]+) -->\n).*?(<!-- /c4:\2 -->)", re.DOTALL)


def llamar(tool: str, argumentos: dict) -> str:
    cuerpo = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": argumentos},
        }
    ).encode()
    peticion = urllib.request.Request(
        MCP_URL,
        cuerpo,
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(peticion, timeout=120) as respuesta:
        datos = json.loads(respuesta.read().decode())
    if "error" in datos:
        raise SystemExit(f"{tool}: {datos['error']}")
    resultado = datos["result"]
    texto = "\n".join(c.get("text", "") for c in resultado.get("content", []))
    if resultado.get("isError"):
        raise SystemExit(f"{tool}: {texto}")
    return texto


def exportar(dsl: str, vista: str) -> str:
    definicion = json.loads(llamar("export-mermaid", {"dsl": dsl, "viewKey": vista}))["definition"]
    # Structurizr pinta en blanco el texto de las flechas y Mermaid lo hace invisible.
    definicion = definicion.replace("  linkStyle default fill:#ffffff\n", "", 1)
    # El marco externo ignora el `graph LR` y saldría una sola fila muy ancha.
    definicion = re.sub(
        r"(subgraph diagram \[[^\n]*\n)", r"\1    direction LR\n", definicion, count=1
    )
    return definicion.strip()


def actualizar(archivo: Path, dsl: str) -> int:
    contenido = archivo.read_text(encoding="utf-8")
    total = 0

    def reemplazar(coincidencia: re.Match[str]) -> str:
        nonlocal total
        total += 1
        apertura, vista, cierre = coincidencia.groups()
        return f"{apertura}```mermaid\n{exportar(dsl, vista)}\n```\n{cierre}"

    archivo.write_text(BLOQUE.sub(reemplazar, contenido), encoding="utf-8")
    return total


def main() -> None:
    dsl = DSL.read_text(encoding="utf-8")
    # `validate` devuelve el error como texto, sin marcar `isError`.
    validacion = llamar("validate", {"dsl": dsl})
    if validacion != "OK":
        raise SystemExit(f"{DSL}: {validacion}")
    for archivo in [C4_MD, *(Path(a).resolve() for a in sys.argv[1:])]:
        print(f"{archivo}: {actualizar(archivo, dsl)} vistas")


if __name__ == "__main__":
    main()
