#!/usr/bin/env python3
"""Reporte de paridad del panel clásico contra la superficie nueva.

Extrae las rutas del panel SSR clásico (core + packs), las rutas de la API
(`docs/openapi.json`) y las páginas del panel nuevo, y emite un markdown con la
clasificación por dominio: **migrada**, **retirada** (ya no existe en el clásico)
o **pendiente** (con su motivo).

Uso:
    python scripts/parity-report.py [--panel ../solvershub-panel/apps/panel-web]
                                    [--out ../solvershub-comercial/PARIDAD-PANEL.md]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# (archivo, prefijo del router): las rutas del código son relativas al prefijo.
CLASICO: list[tuple[Path, str]] = [
    (RAIZ / "core/admin/views.py", "/admin"),
    (RAIZ / "packages/pack-commerce/pack_commerce/module.py", "/crm"),
    (RAIZ / "packages/pack-calendar/pack_calendar/module.py", "/calendar"),
    (RAIZ / "packages/pack-calendar/pack_calendar/admin.py", "/calendar"),
    (RAIZ / "packages/pack-cobranza/pack_cobranza/admin.py", "/cobranza"),
]
RUTA = re.compile(r'@(?:router|admin_router|oauth_router)\.(?:get|post|put|delete)\("([^"]+)"')

# Clasificación curada por prefijo del clásico (equivalente nuevo o motivo).
ESTADOS: list[tuple[str, str, str]] = [
    ("/cobranza", "migrada", "API /pack/cobranza/* + secciones del panel"),
    ("/crm", "migrada", "API /pack/commerce/* + secciones del panel (incluye CSV)"),
    ("/calendar", "migrada", "API /pack/calendar/* + panel (config, tipos, citas, OAuth)"),
    ("/admin/plugins/pack_calendar/oauth", "retirada", "ahora en /api/v1/pack/calendar/oauth/*"),
    ("/admin/asistente", "retirada", "API /admin/playground + panel /asistente"),
    ("/admin/dlq", "retirada", "API /admin/dlq + panel /dlq"),
    ("/admin/plantillas", "retirada", "API de plantillas + panel /plantillas"),
    ("/admin/conexiones/meta-requisitos", "retirada", "API /admin/meta/checklist/requirements + página"),
    ("/admin/conexiones/produccion", "retirada", "API /admin/meta/checklist/production + página"),
    ("/admin/conexiones/webhooks", "retirada", "panel: Conexiones → Meta"),
    ("/admin/configuracion?tab=avanzado", "parcial", "spec crudo migrado; herramientas declarativas pendientes"),
    ("/admin/configuracion", "migrada", "panel /configuracion (incluye el spec crudo; herramientas declarativas pendientes)"),
    ("/admin/conexiones/wizard/whatsapp", "pendiente", "Bloque B: wizard en el panel nuevo (ADR-0080, con mocks)"),
    ("/admin/conexiones/manual", "pendiente", "Bloque B: alta manual dentro del wizard"),
    ("/admin/conexiones/meta", "migrada", "panel: Conexiones → Meta (números, plantillas, salud)"),
    ("/admin/conexiones/waweb", "migrada", "panel /conexiones/waweb (QR, riesgo, reinicio)"),
    ("/admin/conexiones", "migrada", "panel /conexiones (catálogo y alta)"),
    ("/admin/tenants", "migrada", "panel /tenants (alta incluida)"),
    ("/admin/colaboradores", "migrada", "panel /colaboradores (roles, permisos, contraseña)"),
    ("/admin/admins", "migrada", "cubierto por Colaboradores"),
    ("/admin/plugins", "migrada", "panel /habilidades (activar, config)"),
    ("/admin/conversaciones", "migrada", "panel /conversaciones (+ ficha lateral)"),
    ("/admin/escalaciones", "migrada", "panel /escalaciones"),
    ("/admin/leads", "migrada", "panel /leads"),
    ("/admin/costos", "migrada", "panel /costos"),
    ("/admin/plan", "migrada", "panel /plan"),
    ("/admin/spec", "retirada", "redirect histórico; el editor vive en Configuración → Avanzado"),
    ("/admin/login", "migrada", "panel /login + /api/session"),
    ("/admin/logout", "migrada", "panel: cerrar sesión"),
    ("/admin/switch-tenant", "migrada", "panel: selector de negocio (/api/tenant)"),
    ("/admin/packs", "migrada", "los packs con UI tienen página propia; el resto se configura en Habilidades"),
    ("/admin/", "migrada", "panel /dashboard"),
]


def clasificar(ruta: str) -> tuple[str, str]:
    for prefijo, estado, nota in ESTADOS:
        if ruta.startswith(prefijo):
            return estado, nota
    return "revisar", "sin clasificar: revisar manualmente"


def rutas_clasicas() -> dict[str, list[str]]:
    por_archivo: dict[str, list[str]] = {}
    for archivo, prefijo in CLASICO:
        if not archivo.exists():
            continue
        rutas = sorted({(prefijo + r).replace("//", "/") for r in RUTA.findall(archivo.read_text())})
        if rutas:
            por_archivo[str(archivo.relative_to(RAIZ))] = rutas
    return por_archivo


def rutas_api() -> list[str]:
    datos = json.loads((RAIZ / "docs/openapi.json").read_text())
    return sorted(datos.get("paths", {}))


def paginas_panel(panel: Path) -> list[str]:
    if not panel.exists():
        return []
    paginas = []
    for page in panel.glob("app/(app)/**/page.tsx"):
        rel = page.relative_to(panel / "app/(app)").parent.as_posix()
        paginas.append("/" + (rel if rel != "." else ""))
    return sorted(paginas)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", default=str(RAIZ.parent / "solvershub-panel/apps/panel-web"))
    parser.add_argument("--out", default=str(RAIZ.parent / "solvershub-comercial/PARIDAD-PANEL.md"))
    args = parser.parse_args()

    clasicas = rutas_clasicas()
    api = rutas_api()
    panel = paginas_panel(Path(args.panel))

    filas: list[tuple[str, str, str, str]] = []
    for archivo, rutas in clasicas.items():
        for ruta in rutas:
            estado, nota = clasificar(ruta)
            filas.append((archivo, ruta, estado, nota))

    conteo: dict[str, int] = {}
    for _, _, estado, _ in filas:
        conteo[estado] = conteo.get(estado, 0) + 1

    lineas = [
        "# Paridad panel clásico ↔ panel nuevo",
        "",
        f"- **Generado:** {date.today().isoformat()} (script `scripts/parity-report.py`)",
        f"- **Rutas del clásico (core + packs):** {len(filas)}",
        f"- **Rutas de la API (`/api/v1`):** {len(api)}",
        f"- **Páginas del panel nuevo:** {len(panel)}",
        "",
        "## Resumen",
        "",
        "| Estado | Rutas |",
        "|---|---|",
    ]
    for estado in ("migrada", "retirada", "parcial", "pendiente", "revisar"):
        if conteo.get(estado):
            lineas.append(f"| {estado} | {conteo[estado]} |")
    lineas += ["", "## Detalle", "", "| Archivo | Ruta | Estado | Nota |", "|---|---|---|---|"]
    for archivo, ruta, estado, nota in sorted(filas, key=lambda f: (f[2], f[1])):
        lineas.append(f"| `{archivo}` | `{ruta}` | **{estado}** | {nota} |")

    pendientes = [f for f in filas if f[2] in ("pendiente", "parcial", "revisar")]
    lineas += ["", "## Pendientes", ""]
    if pendientes:
        for archivo, ruta, estado, nota in pendientes:
            lineas.append(f"- **{estado}** `{ruta}` (`{archivo}`) — {nota}")
    else:
        lineas.append("- Ninguno fuera del wizard de WhatsApp (Bloque B).")
    lineas += [
        "",
        "## Retiro del clásico",
        "",
        "- Las rutas marcadas **retirada** ya no existen en el backend.",
        "- Las **migradas** siguen vivas en el clásico (su menú interno aún las enlaza):",
        "  el retiro físico de sus plantillas es el paso final del ADR-0076, cuando el",
        "  clásico deje de usarse.",
        "",
    ]

    salida = Path(args.out)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text("\n".join(lineas))
    print(f"✓ {salida} · rutas clásicas {len(filas)} · API {len(api)} · panel {len(panel)}")
    print("  conteo:", conteo)


if __name__ == "__main__":
    main()
