"""Junta los fragmentos de `changelog.d/` en `CHANGELOG.md` (skill `release`).

Cada PR agrega su fragmento (`changelog.d/AAAA-MM-DD-<tema>.md`) en vez de editar el
CHANGELOG: así los PRs en paralelo no chocan en la cabeza del archivo.

  python scripts/changelog.py                                   # lista lo pendiente
  python scripts/changelog.py --version v0.8.0 --titulo "..."   # consolida y borra los fragmentos

La consolidación toma también las secciones `## Sin versión — …` que ya estaban en el
CHANGELOG (las de antes de los fragmentos) y las pone bajo la versión nueva.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CHANGELOG = RAIZ / "CHANGELOG.md"
FRAGMENTOS = RAIZ / "changelog.d"
_SIN_VERSION = re.compile(r"^## Sin versión — (.+)$")
_VERSION = re.compile(r"^## v\d+\.\d+\.\d+")


def fragmentos(carpeta: Path = FRAGMENTOS) -> list[Path]:
    """Fragmentos pendientes, del más nuevo al más viejo (por nombre: `AAAA-MM-DD-…`)."""
    return sorted((p for p in carpeta.glob("*.md") if p.name != "README.md"), reverse=True)


def _como_subseccion(texto: str, origen: str) -> str:
    """`## Título (fecha)` → `### Título (fecha)`. Falla si el fragmento no tiene encabezado."""
    lineas = texto.strip().splitlines()
    if not lineas or not lineas[0].startswith("## "):
        raise ValueError(f"{origen}: la primera línea tiene que ser un encabezado `## Título (AAAA-MM-DD)`")
    titulo = _SIN_VERSION.sub(r"\1", lineas[0])  # admite el formato viejo
    titulo = titulo[3:] if titulo.startswith("## ") else titulo
    return "\n".join([f"### {titulo}", *lineas[1:]]).rstrip() + "\n"


def _separar(changelog: str) -> tuple[str, list[str], str]:
    """(cabecera, secciones "Sin versión", resto desde la primera versión)."""
    lineas = changelog.splitlines(keepends=True)
    cabecera: list[str] = []
    secciones: list[list[str]] = []
    i = 0
    while i < len(lineas) and not lineas[i].startswith("## "):
        cabecera.append(lineas[i])
        i += 1
    while i < len(lineas) and not _VERSION.match(lineas[i]):
        if lineas[i].startswith("## "):
            secciones.append([])
        if secciones:
            secciones[-1].append(lineas[i])
        i += 1
    return "".join(cabecera), ["".join(s) for s in secciones], "".join(lineas[i:])


def consolidar(changelog: str, textos: list[tuple[str, str]], version: str, titulo: str, hoy: date) -> str:
    """CHANGELOG con la versión nueva arriba. `textos`: [(origen, contenido)] de los fragmentos."""
    cabecera, sin_version, resto = _separar(changelog)
    partes = [_como_subseccion(t, o) for o, t in textos]
    partes += [_como_subseccion(s, "CHANGELOG.md") for s in sin_version]
    if not partes:
        raise ValueError("No hay fragmentos ni secciones 'Sin versión' para consolidar.")
    bloque = f"## {version} — {titulo} ({hoy.isoformat()})\n\n" + "\n".join(partes)
    return cabecera.rstrip() + "\n\n" + bloque.rstrip() + "\n\n" + resto.lstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", help="vX.Y.Z: consolida en esa versión y borra los fragmentos")
    parser.add_argument("--titulo", default="", help="Título de la versión")
    args = parser.parse_args(argv)

    pendientes = fragmentos()
    if not args.version:
        for p in pendientes:
            print(f"- {p.name}: {p.read_text(encoding='utf-8').strip().splitlines()[0]}")
        print(f"{len(pendientes)} fragmento(s) pendiente(s).")
        return 0
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.version) or not args.titulo.strip():
        print("Uso: --version vX.Y.Z --titulo '<título>'", file=sys.stderr)
        return 2
    textos = [(p.name, p.read_text(encoding="utf-8")) for p in pendientes]
    nuevo = consolidar(CHANGELOG.read_text(encoding="utf-8"), textos, args.version, args.titulo.strip(), date.today())
    CHANGELOG.write_text(nuevo, encoding="utf-8")
    for p in pendientes:
        p.unlink()
    print(f"{args.version}: {len(textos)} fragmento(s) consolidados; revisa el diff y commitea.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
