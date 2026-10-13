"""Reglas puras de estado del centro de pruebas (D-SPEC-30-5).

- **Paso:** `ok`, `falla`, `bloqueado`, `omitido` o sin marcar (`None`).
- **Prueba en una ejecución:** se deriva de sus pasos. Un paso en falla la deja
  en `falla` aunque falten pasos; luego `bloqueada`; con todos los pasos
  marcados es `ok` si alguno se aprobó u `omitida` si todos se omitieron; con
  algunos marcados, `en_curso`; sin ninguno, `sin_correr`.
- **Prueba en el ciclo:** la consolidación de todas las ejecuciones toma el
  estado de mayor rango (`falla` > `bloqueada` > `ok` > `en_curso` > `omitida` >
  `sin_correr`): si un tester la falló, el ciclo la muestra en falla.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

ESTADOS_PASO = ("ok", "falla", "bloqueado", "omitido")
ESTADOS_PRUEBA = ("sin_correr", "en_curso", "omitida", "ok", "bloqueada", "falla")
TERMINADOS = frozenset({"ok", "falla", "bloqueada", "omitida"})
ESTADOS_CONFIG = ("pendiente", "en_curso", "listo", "no_aplica")

_RANGO = {"sin_correr": 0, "omitida": 1, "en_curso": 2, "ok": 3, "bloqueada": 4, "falla": 5}


def estado_de_prueba(total_pasos: int, pasos: Mapping[int, str | None]) -> str:
    """Estado de una prueba a partir de sus pasos marcados (`{n: estado}`, n desde 1)."""
    marcados = [e for n, e in pasos.items() if e and 1 <= n <= total_pasos]
    if not marcados:
        return "sin_correr"
    if "falla" in marcados:
        return "falla"
    if "bloqueado" in marcados:
        return "bloqueada"
    if len(marcados) < total_pasos:
        return "en_curso"
    return "ok" if "ok" in marcados else "omitida"


def consolidar(estados: Iterable[str]) -> str:
    """Estado de una prueba en el ciclo a partir de las ejecuciones de cada tester."""
    mejor = "sin_correr"
    for estado in estados:
        if _RANGO.get(estado, 0) > _RANGO[mejor]:
            mejor = estado
    return mejor


def progreso(estados_por_prueba: Mapping[str, str], total: int) -> dict[str, int]:
    """Conteo por estado para `total` pruebas; las que no aparecen cuentan como sin correr."""
    conteo = {estado: 0 for estado in ESTADOS_PRUEBA}
    for estado in estados_por_prueba.values():
        if estado in conteo:
            conteo[estado] += 1
    vistos = sum(conteo.values())
    conteo["sin_correr"] += max(total - vistos, 0)
    terminadas = sum(conteo[e] for e in TERMINADOS)
    return {"total": total, "terminadas": terminadas, **conteo}
