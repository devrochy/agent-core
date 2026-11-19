#!/usr/bin/env python3
"""Chequeos previos a abrir un PR de agent-core. SOLO LECTURA (salvo `git fetch`).

Compara la rama actual contra la base (por defecto `origin/develop`) y contra los PRs
abiertos. Lo que revisa es lo que ya rompió PRs o `develop` en paralelo:

  - árbol limpio, rama distinta de develop/main, commits propios
  - fragmento de changelog nuevo en `changelog.d/` (y con encabezado `##`)
  - migraciones nuevas: número libre en develop y en PRs abiertos, una sola cabeza de
    Alembic, y la regla de tenant_id + RLS (mismo criterio que el hook guards.py)
  - ADR nuevos: número libre en develop y en PRs abiertos
  - archivos de test nuevos con nombre repetido en `tests/` (rompe la colección de pytest)
  - cambios en la API sin `docs/openapi.json`, y `.env` agregados
  - título en formato convencional (si se pasa --titulo)

Uso: python3 verificar_pr.py [--base origin/develop] [--titulo "feat(x): …"] [--sin-fetch]
Sale con 1 si hay algún ERROR; los AVISO no cortan.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

TITULO_RE = re.compile(r"^(feat|fix|docs|chore|ci|refactor|test|perf|style|build|revert)(\([^)]+\))?!?: .{5,}")
MIGRACIONES_RE = re.compile(r"^(alembic/versions|packages/pack-[^/]+/alembic/versions)/(\d{4})_[^/]+\.py$")
ADR_RE = re.compile(r"^docs/adr/(\d{4})-[^/]+\.md$")
REV_RE = re.compile(r"""^revision(?:\s*:\s*[^=]+)?\s*=\s*["']([^"']+)["']""", re.M)
DOWN_RE = re.compile(r"""^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+)$""", re.M)

resultados: list[tuple[str, str]] = []


def ok(msg: str) -> None:
    resultados.append(("OK", msg))


def aviso(msg: str) -> None:
    resultados.append(("AVISO", msg))


def error(msg: str) -> None:
    resultados.append(("ERROR", msg))


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return r.stdout.strip()


def gh_prs_abiertos(rama_propia: str) -> list[dict]:
    r = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", "number,headRefName,files"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        aviso("no pude leer los PRs abiertos con gh: la verificación de números en paralelo es parcial")
        return []
    return [p for p in json.loads(r.stdout or "[]") if p["headRefName"] != rama_propia]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/develop")
    ap.add_argument("--titulo", default="")
    ap.add_argument("--sin-fetch", action="store_true")
    args = ap.parse_args()

    raiz = git("rev-parse", "--show-toplevel")
    if not raiz:
        print("No estás dentro de un repo git.")
        sys.exit(2)
    import os
    os.chdir(raiz)

    if not args.sin_fetch:
        subprocess.run(["git", "fetch", "--quiet", "origin"], check=False)

    rama = git("branch", "--show-current")
    if not rama or rama in ("develop", "main"):
        error(f"la rama actual es '{rama or 'detached HEAD'}': abre el PR desde una rama de trabajo")
    propios = git("rev-list", "--count", f"{args.base}..HEAD")
    if propios in ("", "0"):
        error(f"no hay commits propios frente a {args.base}")
    else:
        ok(f"{propios} commit(s) propios frente a {args.base}")
    atras = git("rev-list", "--count", f"HEAD..{args.base}")
    if atras not in ("", "0"):
        aviso(f"la rama está {atras} commit(s) detrás de {args.base}; considera rebase antes del PR")
    if git("status", "--porcelain"):
        error("hay cambios sin commitear o archivos sin seguimiento")
    else:
        ok("árbol de trabajo limpio")

    # Archivos del PR: (estado, ruta)
    cambios: list[tuple[str, str]] = []
    for linea in git("diff", "--name-status", "--no-renames", f"{args.base}...HEAD").splitlines():
        estado, _, ruta = linea.partition("\t")
        cambios.append((estado[:1], ruta))
    nuevos = [r for e, r in cambios if e == "A"]
    todos = [r for _, r in cambios]

    # --- título
    if args.titulo:
        if TITULO_RE.match(args.titulo):
            ok("título en formato convencional")
        else:
            error("el título no sigue «tipo(ámbito): resumen» (feat|fix|docs|chore|ci|refactor|test|perf…)")

    # --- changelog
    fragmentos = [r for r in nuevos if r.startswith("changelog.d/") and r.endswith(".md") and not r.endswith("README.md")]
    if not fragmentos:
        error("falta un fragmento NUEVO en changelog.d/AAAA-MM-DD-<tema>.md (no edites CHANGELOG.md)")
    else:
        for f in fragmentos:
            if not re.match(r"^changelog\.d/\d{4}-\d{2}-\d{2}-[^/]+\.md$", f):
                error(f"{f}: el nombre debe ser AAAA-MM-DD-<tema>.md")
            elif not Path(f).read_text(encoding="utf-8").lstrip().startswith("## "):
                error(f"{f}: debe empezar con un encabezado «## »")
            else:
                ok(f"changelog: {f}")
    if "CHANGELOG.md" in todos:
        aviso("el PR edita CHANGELOG.md: usa un fragmento en changelog.d/ para no chocar con PRs en paralelo")

    prs = gh_prs_abiertos(rama)

    # --- migraciones
    mig_nuevas = [(MIGRACIONES_RE.match(r), r) for r in nuevos]
    mig_nuevas = [(m, r) for m, r in mig_nuevas if m]
    for m, ruta in mig_nuevas:
        carpeta, num = m.group(1), m.group(2)
        en_base = [
            p for p in git("ls-tree", "-r", "--name-only", args.base, "--", carpeta).splitlines()
            if Path(p).name.startswith(num + "_")
        ]
        if en_base:
            error(f"{ruta}: el número {num} ya existe en {args.base} ({Path(en_base[0]).name}); renumera")
        for pr in prs:
            choque = [f["path"] for f in pr["files"] if f["path"].startswith(carpeta + "/" + num + "_")]
            if choque:
                error(f"{ruta}: el número {num} lo usa el PR abierto #{pr['number']} ({Path(choque[0]).name}); coordina o renumera")
    if mig_nuevas:
        carpetas = {m.group(1) for m, _ in mig_nuevas}
        for carpeta in sorted(carpetas):
            revs, bajas = set(), set()
            for p in git("ls-tree", "-r", "--name-only", "HEAD", "--", carpeta).splitlines():
                if not p.endswith(".py"):
                    continue
                texto = git("show", f"HEAD:{p}")
                for r in REV_RE.findall(texto):
                    revs.add(r)
                for d in DOWN_RE.findall(texto):
                    bajas.update(re.findall(r"""["']([^"']+)["']""", d))
            cabezas = sorted(revs - bajas)
            if len(cabezas) == 1:
                ok(f"{carpeta}: una sola cabeza de Alembic ({cabezas[0]})")
            else:
                error(f"{carpeta}: {len(cabezas)} cabezas de Alembic {cabezas}; encadena down_revision")
        # Un archivo NUEVO debe cumplir completo. `guards.py --migraciones` compara contra HEAD
        # (sirve al pre-commit) y daría por buena una migración ya commiteada, así que se usa
        # directamente su regla sobre el contenido.
        guard = Path(".claude/hooks/guards.py")
        if guard.exists():
            import importlib.util

            spec = importlib.util.spec_from_file_location("guards", guard)
            guards = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(guards)
            fallos = False
            for _, ruta in mig_nuevas:
                faltan = guards.missing_isolation(Path(ruta).read_text(encoding="utf-8"))
                if faltan:
                    fallos = True
                    error(f"{ruta}: crea tablas y le falta: " + "; ".join(faltan))
            if not fallos:
                ok("migraciones nuevas cumplen tenant_id + RLS forzada + policy")

    # --- ADR
    for r in nuevos:
        m = ADR_RE.match(r)
        if not m:
            continue
        num = m.group(1)
        en_base = [p for p in git("ls-tree", "-r", "--name-only", args.base, "--", "docs/adr").splitlines() if Path(p).name.startswith(num + "-")]
        if en_base:
            error(f"{r}: el ADR {num} ya existe en {args.base} ({Path(en_base[0]).name}); renumera (solo las líneas de tu commit)")
        for pr in prs:
            choque = [f["path"] for f in pr["files"] if re.match(rf"^docs/adr/{num}-", f["path"])]
            if choque:
                error(f"{r}: el ADR {num} lo usa el PR abierto #{pr['number']} ({Path(choque[0]).name})")

    # --- tests con nombre repetido
    tests_nuevos = [r for r in nuevos if re.match(r"^tests/.*test_[^/]*\.py$", r)]
    if tests_nuevos:
        por_nombre: dict[str, list[str]] = defaultdict(list)
        for p in git("ls-files", "tests").splitlines():
            if re.search(r"/test_[^/]*\.py$", p):
                por_nombre[Path(p).name].append(p)
        for nuevo in tests_nuevos:
            otros = [p for p in por_nombre[Path(nuevo).name] if p != nuevo]
            if otros and not all(Path(Path(p).parent, "__init__.py").exists() for p in [nuevo, *otros]):
                error(f"{nuevo}: el nombre ya existe en {otros[0]} y esas carpetas no son paquetes: pytest -q falla con «import file mismatch»")

    # --- contrato y secretos
    toca_api = [r for r in todos if r.startswith("core/api/") or re.match(r"^packages/pack-[^/]+/.*(router|routes|api)", r)]
    if toca_api and "docs/openapi.json" not in todos:
        aviso("el PR toca rutas/schemas de la API pero no docs/openapi.json: si cambió el contrato, regenéralo (ver «Contrato» en la skill)")
    elif "docs/openapi.json" in todos:
        aviso("el PR cambia docs/openapi.json: revisa compatibilidad con el subagente contrato-api-reviewer")
    for r in nuevos:
        if re.search(r"(^|/)\.env($|\.)", r) and not r.endswith(".env.example"):
            error(f"{r}: no se versionan archivos .env")

    # --- salida
    simbolo = {"OK": "✓", "AVISO": "!", "ERROR": "✗"}
    for nivel, msg in resultados:
        print(f"{simbolo[nivel]} [{nivel}] {msg}")
    n_err = sum(1 for n, _ in resultados if n == "ERROR")
    n_av = sum(1 for n, _ in resultados if n == "AVISO")
    print(f"\n{n_err} error(es), {n_av} aviso(s) · rama {rama} · base {args.base}")
    sys.exit(1 if n_err else 0)


if __name__ == "__main__":
    main()
