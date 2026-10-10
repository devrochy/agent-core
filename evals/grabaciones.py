"""Cableado del modo grabar/reproducir del LLM en los runners de evals (adenda de ADR-0119).

`evals/runner.py` y `evals/journeys.py` aceptan `--grabacion off|record|replay` (o la variable
`LLM_CASSETTE`) y `--grabaciones DIR` (o `LLM_CASSETTE_DIR`). La lógica vive en
`core/llm/cassette.py`; aquí solo se resuelven rutas y se enciende el modo. Ver `evals/README.md`.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
CASSETTES_DIR = EVALS_DIR / "cassettes"
REPO = EVALS_DIR.parent
# Código de salida cuando la corrida se corta por la grabación (falta, desactualizada o
# prohibida): distinto de 1 (escenarios fallidos) para que el CI lo distinga.
SALIDA_GRABACION = 3


def agregar_argumentos(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--grabacion",
        default=os.environ.get("LLM_CASSETTE") or "off",
        help="off (LLM real, por defecto), record (LLM real y graba: gasta saldo, requiere OK del dueño) "
        "o replay (sin red ni LLM_API_KEY). También LLM_CASSETTE.",
    )
    parser.add_argument(
        "--grabaciones",
        default=os.environ.get("LLM_CASSETTE_DIR") or "",
        help="Raíz de las grabaciones (por defecto evals/cassettes, o TENANTS_ROOT/<slug>/evals/cassettes "
        "con --tenant). También LLM_CASSETTE_DIR.",
    )


def activar(modo: str) -> None:
    """Enciende el modo antes de tocar la base o el LLM. Con record/replay apaga los embeddings
    de memoria: con `openai` saldrían a la red y con `local` podrían bajar un modelo; además el
    recuerdo cambia el prompt y rompería la reproducción."""
    from core.llm import cassette

    cassette.activar(modo)  # primero el candado de entorno
    if modo != "off":
        os.environ["EMBEDDING_PROVIDER"] = "none"
        from core.config import get_settings

        get_settings.cache_clear()
    if modo == "record":
        print("Modo record: llama al LLM real y graba (gasta saldo; solo con OK del dueño).")
    elif modo == "replay":
        print("Modo replay: respuestas desde las grabaciones, sin red. No mide la consistencia del modelo "
              "y la corrida no se guarda en eval_runs (ADR-0119).")


def raiz(grabaciones: str, evals_del_negocio: Path | None) -> Path:
    """Raíz de las grabaciones. `evals_del_negocio` es `TENANTS_ROOT/<slug>/evals` calculado
    ANTES de la réplica de journeys (que cambia `TENANTS_ROOT` a un temporal que se borra).

    Las de un negocio nunca quedan en el repo del framework, salvo bajo `tenants/` (gitignored,
    el `TENANTS_ROOT` por defecto en desarrollo). Con una raíz explícita van en `<raíz>/<slug>/`."""
    if grabaciones:
        ruta = Path(grabaciones).resolve()
        if evals_del_negocio is not None:
            # Una raíz explícita (o `LLM_CASSETTE_DIR` fija en la shell) separa por negocio: dos
            # negocios corridos en serie nunca comparten ni pisan grabaciones.
            ruta = ruta / evals_del_negocio.parent.name
    elif evals_del_negocio is not None:
        ruta = evals_del_negocio.resolve() / "cassettes"
    else:
        return CASSETTES_DIR
    if evals_del_negocio is not None and ruta.is_relative_to(REPO) and not ruta.is_relative_to(REPO / "tenants"):
        raise SystemExit(f"Las grabaciones de un negocio no van en el repo del framework ({ruta}): usa su repo.")
    return ruta


def comando_regrabar(script: str, nombre: str, extra: str = "") -> str:
    partes = ["python", script, nombre, extra, "--grabacion record"]
    return " ".join(p for p in partes if p)
