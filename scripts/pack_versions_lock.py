"""Regenera `packages/pack-versions.lock.json` (HU-AOS-14, ADR-0118 §1).

El lock guarda `{pack_id: {version: manifest_hash}}` de cada versión de una habilidad de la
plataforma. `tests/test_pack_versions_lock.py` falla si el contenido de un pack (playbooks,
`flow.json`, acciones) cambia sin subir su versión: la versión registrada en `pack_versions` es
inmutable y el build de releases exige la registrada. Uso, tras subir la versión del pack:

    python scripts/pack_versions_lock.py

Conserva las versiones viejas (son historia). No toca la base de datos.
"""

from __future__ import annotations

import json


def main() -> int:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import (
        LOCK_PATH,
        es_de_plataforma,
        leer_lock,
        lock_actualizado,
        manifiestos,
    )

    vigentes = manifiestos(m for m in plugin_manager.scan() if es_de_plataforma(m))
    actual = leer_lock()
    for pid, man in sorted(vigentes.items()):
        previo = actual.get(pid, {}).get(man.version)
        if previo is not None and previo != man.manifest_hash:
            print(
                f"ERROR: {pid} {man.version} cambió de contenido sin subir la versión: súbela primero."
            )
            return 1
    nuevo = lock_actualizado(actual, vigentes)
    LOCK_PATH.write_text(
        json.dumps(nuevo, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Lock escrito: {LOCK_PATH} ({sum(len(v) for v in nuevo.values())} versiones)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
