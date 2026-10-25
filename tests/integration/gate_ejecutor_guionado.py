"""Ejecutor guionado del gate de pass^k para los tests (HU-AOS-21 parte 3b): sin LLM ni red.

Lo carga `evals/gate_release.py` por `EVAL_GATE_EJECUTOR=gate_ejecutor_guionado:ejecutor` (solo en
entornos de prueba). En vez de correr el journey, comprueba que la release candidata quedó MONTADA en el
negocio efímero de la base de eval (runtime v3, release activa, procedimientos del foco) y devuelve lo que
dicta el guion (`EVAL_GATE_GUION`, un JSON `{nombre_del_escenario: [ok por intento]}`), dejando una fila
de gasto por intento en el `usage_ledger` del efímero para que el corredor la sume.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from sqlalchemy import text


async def ejecutor(*, tenant_id: uuid.UUID, set_id: str, nombre: str, ruta: Any, intento: int, pagos: Any) -> dict[str, Any]:
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    if os.environ.get("EVAL_GATE_EXPLOTA") == nombre:
        raise RuntimeError("falla guionada")
    guion = json.loads(open(os.environ["EVAL_GATE_GUION"], encoding="utf-8").read())
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            runtime, procs = (
                await session.execute(text("select aos_runtime, aos_procedures from tenants where id = :t"), {"t": tenant_id})
            ).one()
            activa = (
                await session.execute(
                    text("select active_release_id from channel_connections where tenant_id = :t and active_release_id is not null"),
                    {"t": tenant_id},
                )
            ).scalar_one_or_none()
            release = await cargar_release(session, tenant_id, activa) if activa else None
            foco = release.foco() if release else None
            declarados = sorted(json.loads(ruta.read_text(encoding="utf-8")).get("procedimientos") or [])
            montada = bool(
                runtime == "v3"
                and procs
                and release is not None
                and foco is not None
                and set(declarados) <= set(release.procedimientos_de(foco.instance_id))
            )
            await session.execute(
                text(
                    "insert into usage_ledger (id, tenant_id, model, input_tokens, output_tokens, cost_usd, kind) "
                    "values (gen_random_uuid(), :t, 'modelo-guionado', 100, 50, :c, 'turn')"
                ),
                {"t": tenant_id, "c": float(os.environ.get("EVAL_GATE_COSTO_INTENTO", "0.01"))},
            )
    if not montada:
        return {"ok": False, "detalle": "la release no quedó montada en el efímero"}
    ok = bool(guion.get(nombre, [True] * 4)[intento])
    return {"ok": ok, "detalle": "" if ok else f"guion: {nombre} falla en el intento {intento + 1}"}
