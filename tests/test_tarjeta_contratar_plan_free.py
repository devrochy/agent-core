"""La tarjeta de `contratar_plan` 2.0.1 en la prueba del Free por chat web en test (2026-11-15).

Lo que el modelo ve en el turno (el `goal` de las etapas no llega al prompt, por eso no alcanzó):
- en `elegir_plan`, el plan va con su rótulo («Plan») antes del nombre interno `plan_code`, y la
  tarjeta manda guardar con `actualizar_tarea` el plan que el cliente ya nombró en vez de pedirlo
  (respondía «1. Plan (el código del plan que quieres contratar)»);
- tras `sub_inscribir` (etapa `esperar_pago`) la tarjeta dice que no ofrezca mandar la
  confirmación por correo (ofrecía «¿Quieres que te envíe la confirmación al correo…?»).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentcore_contracts.procedure import Procedure
from pack_subscriptions.procedimientos import rotulos_de_datos

from core.kernel.state import Proyeccion
from core.kernel.tarjeta import renderizar_tarjeta

T0 = datetime(2026, 11, 15, 23, 45, tzinfo=UTC)
RAIZ = Path(__file__).resolve().parents[1]
PLAN = Procedure.model_validate(
    json.loads((RAIZ / "packages/pack-subscriptions/procedures/contratar_plan.json").read_text(encoding="utf-8"))
)


def _tarjeta(etapa: str, **datos: Any) -> str:
    tarea = Proyeccion(
        procedure_id=PLAN.id, procedure_version=PLAN.version, status="active", stage=etapa, stack_position=0,
        last_seq=31, created_at=T0, updated_at=T0, pending_confirmation=None,
        slots={
            n: {"value": v, "source": "action:sub_inscribir", "confirmed": False, "data_class": "public", "seq": 30}
            for n, v in datos.items()
        },
    )
    return renderizar_tarjeta([tarea], procedimientos=lambda *_: PLAN, rotulos_de=lambda _p: rotulos_de_datos())


def test_elegir_plan_pide_el_plan_por_su_rotulo_y_guarda_el_que_ya_nombro() -> None:
    tarjeta = _tarjeta("elegir_plan")
    pide = next(linea for linea in tarjeta.splitlines() if linea.startswith("Faltan estos datos"))
    assert '1. "Plan" (se guarda como plan_code)' in pide
    assert "plan_code (" not in pide  # el nombre interno ya no va primero
    assert "guárdalo primero con actualizar_tarea en esta misma vuelta" in pide
    assert "nunca su nombre interno ni un código" in pide


def test_tras_inscribir_no_ofrece_mandar_la_confirmacion_por_correo() -> None:
    datos = {"plan_code": "PLAN-FREE", "monto_a_pagar": 0, "requiere_pago": False, "subscription_id": "a4489478"}
    for etapa in ("pagar", "esperar_pago"):
        tarjeta = _tarjeta(etapa, **datos)
        acciones = next(linea for linea in tarjeta.splitlines() if linea.startswith("Acciones de esta etapa"))
        assert "No le ofrezcas enviarle por correo" in acciones
        assert "lo envía el sistema" in acciones
