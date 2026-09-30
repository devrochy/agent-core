"""Los nombres de playbook deben ser ÚNICOS entre core y packs.

`load_playbook` resuelve por NOMBRE sobre `playbook_roots()` (core + packs) y
devuelve el primer `{name}.md` que encuentra: si dos packs (o un pack y el core)
comparten nombre, gana el primero por orden alfabético de directorio y el otro
queda inalcanzable en silencio. Este test lo bloquea en CI.
"""

from __future__ import annotations

from core.agents.playbooks import playbook_roots


def test_nombres_de_playbook_unicos() -> None:
    seen: dict[str, list[str]] = {}
    for root in playbook_roots():
        for file in root.glob("*.md"):
            seen.setdefault(file.stem, []).append(str(root))
    dupes = {name: roots for name, roots in seen.items() if len(roots) > 1}
    assert not dupes, f"nombres de playbook duplicados entre roots: {dupes}"
