"""agent-core qa: catálogo del centro de pruebas (SPEC-30, HU-QA-02).

El catálogo vive en solvershub-docs (`11-pruebas/catalogo.yaml`) y se versiona con
git. Este comando lo importa como una versión nueva; el ciclo abierto pasa a usarla
y los ciclos cerrados conservan la suya. Importar el mismo contenido dos veces no
duplica nada.

Uso:
  agent-core qa importar ../solvershub-docs/11-pruebas/catalogo.yaml --commit abc1234
  agent-core qa validar ../solvershub-docs/11-pruebas/catalogo.yaml
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()


def _leer(archivo: Path) -> bytes:
    if not archivo.is_file():
        console.print(f"[red]No existe el archivo:[/red] {archivo}")
        raise typer.Exit(1)
    return archivo.read_bytes()


@app.command("validar")
def validar(archivo: Annotated[Path, typer.Argument(help="Ruta a catalogo.yaml")]) -> None:
    """Valida el catálogo sin tocar la base."""
    from core.qa.catalogo import CatalogoInvalido, parse_catalogo

    try:
        cat = parse_catalogo(_leer(archivo))
    except CatalogoInvalido as exc:
        for error in exc.errores:
            console.print(f"[red]ERROR[/red] {error}")
        raise typer.Exit(1) from exc
    por_seccion: dict[str, int] = {}
    for item in cat.items:
        por_seccion[item.seccion] = por_seccion.get(item.seccion, 0) + 1
    console.print(f"[green]OK[/green] versión {cat.version} ({cat.fecha}) · {por_seccion}")


@app.command("importar")
def importar(
    archivo: Annotated[Path, typer.Argument(help="Ruta a catalogo.yaml")],
    commit: Annotated[str, typer.Option("--commit", help="Commit de solvershub-docs del que sale")] = "",
) -> None:
    """Importa el catálogo como versión nueva (idempotente)."""
    from core.qa.catalogo import CatalogoInvalido
    from core.qa.service import importar_catalogo

    contenido = _leer(archivo)
    try:
        res = asyncio.run(importar_catalogo(contenido, admin_id=None, commit_docs=commit or None))
    except CatalogoInvalido as exc:
        for error in exc.errores:
            console.print(f"[red]ERROR[/red] {error}")
        raise typer.Exit(1) from exc
    estado = "importada" if res.creada else "ya estaba importada"
    console.print(
        f"[green]Versión {res.version} {estado}[/green] · {res.items} ítems · ciclo abierto: {res.ciclo_nombre}"
    )
