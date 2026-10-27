"""`tenants.settings` es un jsonb que varios caminos reescriben ENTERO (presets, suscripciones, alta).

Para que ninguno reponga una bandera que el master apagó (`settings.opciones_directas`, SPEC-40; también el gate y
las banderas de colaboración) todo camino de leer-modificar-escribir tiene que leer la fila con `FOR UPDATE`: el
`UPDATE … jsonb_set` del master espera ese candado y se aplica encima. Esta prueba vigila que un escritor nuevo no
se olvide del candado (el caso real está en `tests/integration/test_opciones_bandera_platform.py`).
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# `settings` de OTRA tabla (la conexión del chat web, no el negocio): no pasa por `tenants.settings`.
OTRAS_TABLAS = {"core/gateway/chat_web_conexion.py"}


def _asigna_settings(nodo: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "settings" for t in n.targets)
        for n in ast.walk(nodo)
    )


def _pide_candado(nodo: ast.AST) -> bool:
    return any(
        isinstance(n, ast.keyword) and n.arg == "with_for_update" and isinstance(n.value, ast.Constant) and n.value.value is True
        for n in ast.walk(nodo)
    )


def test_todo_camino_que_reescribe_tenants_settings_lo_lee_con_for_update() -> None:
    escritores: list[str] = []
    sin_candado: list[str] = []
    for ruta in (RAIZ / "core").rglob("*.py"):
        rel = ruta.relative_to(RAIZ).as_posix()
        if rel in OTRAS_TABLAS:
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef) and _asigna_settings(nodo):
                escritores.append(f"{rel}::{nodo.name}")
                if not _pide_candado(nodo):
                    sin_candado.append(f"{rel}::{nodo.name}")
    assert escritores, "la prueba dejó de ver a los escritores: revisa el patrón"
    assert sin_candado == [], (
        f"reescriben tenants.settings sin `with_for_update=True` (podrían pisar una bandera del master): {sin_candado}"
    )
