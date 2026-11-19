"""Los nombres de playbook deben ser ÚNICOS entre core y packs, y caber en el tope.

`load_playbook` resuelve por NOMBRE sobre `playbook_roots()` (core + packs) y
devuelve el primer `{name}.md` que encuentra: si dos packs (o un pack y el core)
comparten nombre, gana el primero por orden alfabético de directorio y el otro
queda inalcanzable en silencio. Este test lo bloquea en CI.
"""

from __future__ import annotations

from core.agents.playbooks import MAX_SIZE_BYTES, playbook_roots


def test_nombres_de_playbook_unicos() -> None:
    seen: dict[str, list[str]] = {}
    for root in playbook_roots():
        for file in root.glob("*.md"):
            seen.setdefault(file.stem, []).append(str(root))
    dupes = {name: roots for name, roots in seen.items() if len(roots) > 1}
    assert not dupes, f"nombres de playbook duplicados entre roots: {dupes}"


def test_ningun_playbook_pasa_el_tope_de_tamano() -> None:
    """`load_playbook` devuelve None si un playbook pasa `MAX_SIZE_BYTES`: el negocio se
    queda sin ese guion sin ningún error a la vista. `ventas.md` de pack-commerce está
    cerca del tope (crece con cada regla de un incidente)."""
    grandes = {
        str(file): size
        for root in playbook_roots()
        for file in root.glob("*.md")
        if (size := len(file.read_bytes())) > MAX_SIZE_BYTES
    }
    assert not grandes, f"playbooks por encima de {MAX_SIZE_BYTES} bytes: {grandes}"
