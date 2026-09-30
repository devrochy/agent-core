"""Exporta el contrato OpenAPI de `/api/v1` sin levantar el servidor.

Uso:
    python scripts/export_openapi.py [salida]

Por defecto escribe `docs/openapi.json` (commiteado: es la fuente para generar el
cliente y los tipos TS del panel y la app, ADR-0077). No requiere BD ni Redis.

Construye un app efímero que monta SOLO `core.api.api_router`, para que el
contrato no arrastre las rutas SSR del panel ni los webhooks internos.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build_schema() -> dict:
    from fastapi import FastAPI

    from core.api import api_router
    from core.plugin_manager import plugin_manager

    app = FastAPI(title="SolversHub API", version="1.0.0", openapi_url=None)
    app.include_router(api_router)
    # API admin de packs (ADR-0079), si sus módulos están instalados.
    plugin_manager.scan()
    for module in plugin_manager.list_modules():
        for router in module.admin_api_routes():
            if getattr(router, "prefix", "").startswith("/api/v1/pack/"):
                app.include_router(router)
    return app.openapi()


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "openapi.json"
    schema = build_schema()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    paths = len(schema.get("paths", {}))
    schemas = len(schema.get("components", {}).get("schemas", {}))
    print(f"✓ {out} · {paths} rutas · {schemas} esquemas")


if __name__ == "__main__":
    main()