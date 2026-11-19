"""Regenera `packages/catalogo-nombres.lock.json` (HU-AOS-59, ADR-0184).

El lock guarda los nombres de las acciones y los procedimientos que el binario trae
(`{"acciones": [...], "procedimientos": [...]}`, solo core y habilidades de la plataforma).
`tests/test_catalogo_retiros.py` falla si un nombre desaparece sin entrada en el registro de retiros
(`core/agents/retiros.py`) o si aparece uno nuevo sin pasar por el lock. Uso, tras agregar o quitar
una acción o un procedimiento:

    python scripts/catalogo_nombres_lock.py

Si quitaste algo, agrega primero su entrada al registro: el script se niega a olvidarlo. No toca la
base de datos.
"""

from __future__ import annotations

import json


def main() -> int:
    from core.agents.referencias_retiradas import nombres_del_binario
    from core.agents.retiros import LOCK_PATH, desaparecidos, leer_lock, problemas_del_lock
    from core.plugin_manager import plugin_manager

    actual = nombres_del_binario(plugin_manager.scan())
    previo = leer_lock()
    sin_entrada = [
        p for p in problemas_del_lock(previo, actual) if "no tiene entrada en el registro" in p
    ]
    if sin_entrada:
        print("ERROR: hay nombres retirados sin entrada en el registro de retiros:")
        for problema in sin_entrada:
            print(f"  - {problema}")
        return 1
    LOCK_PATH.write_text(json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    quitados = desaparecidos(previo, actual)
    print(
        f"Lock escrito: {LOCK_PATH} ({len(actual['acciones'])} acciones, "
        f"{len(actual['procedimientos'])} procedimientos"
        + (f"; salen {len(quitados)} con entrada en el registro" if quitados else "")
        + ")"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
