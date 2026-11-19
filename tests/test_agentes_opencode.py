"""Paridad de los subagentes entre Claude Code y OpenCode.

Cada subagente existe en `.claude/agents` y en `.opencode/agents`. El frontmatter difiere
(cada herramienta tiene el suyo), pero el cuerpo debe ser idéntico: CLAUDE.md pide replicar
a mano cada cambio en su par. Este test detecta cuando se olvida, como pasó con
`customizer`, `migrator` y `optimizer`, que quedaron con rutas y tablas viejas en OpenCode.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
CLAUDE = RAIZ / ".claude" / "agents"
OPENCODE = RAIZ / ".opencode" / "agents"
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)

PARES = sorted(p.name for p in CLAUDE.glob("*.md") if (OPENCODE / p.name).exists())


def _cuerpo(ruta: Path) -> str:
    return FRONTMATTER.sub("", ruta.read_text(encoding="utf-8"), count=1).strip()


def test_hay_pares_que_comparar() -> None:
    assert PARES, "no se encontró ningún subagente en las dos carpetas"


@pytest.mark.parametrize("nombre", PARES)
def test_cuerpo_igual_en_claude_y_opencode(nombre: str) -> None:
    assert _cuerpo(CLAUDE / nombre) == _cuerpo(OPENCODE / nombre), (
        f"{nombre}: el cuerpo difiere entre .claude/agents y .opencode/agents; "
        "replica el cambio en el par"
    )
