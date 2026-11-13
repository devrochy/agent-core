"""Los negocios nacen en `aos_runtime = 'v3'` y el escritor de banderas ya no acepta `legacy` (ADR-0191, HU-AOS-69).

1. `tenants.aos_runtime` cambia su valor por defecto de `'legacy'` a `'v3'`: un negocio nuevo nace en el
   Agent OS sin el paso «nacer en legacy y migrar» del alta.
2. El trigger `tenants_aos_banderas_guard` (0097) exige que un negocio nazca con `('v3', false, 'off')`
   en vez de `('legacy', false, 'off')` cuando quien escribe no es el dueño de `tenants`.
3. `aos_fijar_banderas` (0097), el único escritor de las banderas, rechaza `p_runtime = 'legacy'`
   (`invalid_parameter_value`): entre esta migración y la que suelta la columna nadie puede dejar un
   negocio en un runtime que ya no existe en el código.

No toca datos: que no queden negocios en `legacy` se comprueba con un comando antes de desplegar, no
con un precheck aquí. El CHECK `ck_tenants_aos_runtime` (0070) sigue aceptando `legacy` hasta que se
suelte la columna (otra migración, después de desplegar esta). Las funciones se reescriben a partir del
texto de 0097, cambiando solo esas dos condiciones. Sin reescritura de tabla: `SET DEFAULT` y
`CREATE OR REPLACE FUNCTION` (`lock_timeout` de 5 s). Re-ejecutable; el downgrade vuelve al texto de
0097 y al valor por defecto `'legacy'`.

Revision ID: 0126
Revises: 0125
Create Date: 2026-11-12
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

_NACE_LEGACY = "IS DISTINCT FROM ('legacy', false, 'off');"
_NACE_V3 = "IS DISTINCT FROM ('v3', false, 'off');"
_RUNTIME_CON_LEGACY = "p_runtime NOT IN ('legacy', 'v3')"
_RUNTIME_SOLO_V3 = "p_runtime NOT IN ('v3')"


def _de_0097() -> ModuleType:
    ruta = Path(__file__).with_name("0097_aos_banderas_desde_el_panel.py")
    spec = importlib.util.spec_from_file_location("_migracion_0097", ruta)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _reemplazar(texto: str, viejo: str, nuevo: str) -> str:
    if texto.count(viejo) != 1:
        raise RuntimeError(f"0126: el texto de 0097 cambió; no se encontró una sola vez {viejo!r}")
    return texto.replace(viejo, nuevo)


def upgrade() -> None:
    m = _de_0097()
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE public.tenants ALTER COLUMN aos_runtime SET DEFAULT 'v3'")
    op.execute(_reemplazar(m._FUNCION, _RUNTIME_CON_LEGACY, _RUNTIME_SOLO_V3))
    op.execute(_reemplazar(m._GUARDIA, _NACE_LEGACY, _NACE_V3))
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    m = _de_0097()
    op.execute("SET lock_timeout = '5s'")
    op.execute(m._FUNCION)
    op.execute(m._GUARDIA)
    op.execute("ALTER TABLE public.tenants ALTER COLUMN aos_runtime SET DEFAULT 'legacy'")
    op.execute("RESET lock_timeout")
