"""La etapa `confirmar` de contratar_plan (smoke de test 2026-10-20): la tarjeta le dice al modelo
que llame la acción y que el resumen y el «¿confirmas?» los pone el sistema."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agentcore_contracts.procedure import Procedure

from core.kernel.procedures import motor
from core.kernel.state import Proyeccion
from core.kernel.tarjeta import renderizar_tarjeta

T0 = datetime(2026, 10, 20, 5, 14, tzinfo=UTC)
RAIZ = Path(__file__).resolve().parents[1]
PLAN = Procedure.model_validate(
    json.loads((RAIZ / "tests/fixtures/procedures/contratar_plan_comercio.json").read_text(encoding="utf-8"))
)


def _tarea(pendiente: dict[str, object] | None = None) -> Proyeccion:
    datos = {"variant_id": "v", "quote_id": "q", "business_name": "Tienda la rebaja"}
    return Proyeccion(
        procedure_id=PLAN.id, procedure_version=PLAN.version, status="active", stage="confirmar", stack_position=0,
        last_seq=27, created_at=T0, updated_at=T0, pending_confirmation=pendiente,
        slots={n: {"value": v, "source": "llm", "confirmed": False, "data_class": "public", "seq": 2} for n, v in datos.items()},
    )


def test_sin_peticion_la_tarjeta_dice_que_el_sistema_pide_la_confirmacion() -> None:
    tarjeta = renderizar_tarjeta([_tarea()], procedimientos=lambda *_: PLAN)
    assert "la acción confirmar_cotizacion con resultado correcto: llámala tú" in tarjeta
    assert "no escribas tu propio resumen ni le preguntes si confirma" in tarjeta


def test_con_la_peticion_vigente_sale_la_espera_de_siempre() -> None:
    tarea = _tarea({"kind": "action", "name": "confirmar_cotizacion", "seq": 28})
    tarjeta = renderizar_tarjeta([tarea], procedimientos=lambda *_: PLAN)
    assert "Espera la confirmación explícita del cliente: acción confirmar_cotizacion" in tarjeta
    assert "llámala tú" not in tarjeta


def test_una_accion_sin_confirmacion_no_cambia() -> None:
    etapa = motor.etapa(PLAN, "confirmar")
    assert etapa is not None
    sin_confirmacion = etapa.model_copy(update={"confirmation": None})
    assert not motor.requiere_confirmacion(sin_confirmacion, "confirmar_cotizacion")
    tarjeta = renderizar_tarjeta(
        [_tarea()], procedimientos=lambda *_: PLAN.model_copy(update={"stages": [
            sin_confirmacion if st.id == "confirmar" else st for st in PLAN.stages
        ]}),
    )
    assert "la acción confirmar_cotizacion con resultado correcto" in tarjeta and "llámala tú" not in tarjeta
