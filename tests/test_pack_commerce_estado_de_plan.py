"""Un pedido de PLAN no está «en preparación» (smoke de test 2026-10-20, negocio solvershub):
`consultar_estado_pedido` y `verificar_pago_pedido` devuelven el estado del plan en códigos."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pack_commerce.skills import _estado_del_plan


def _pedido(**campos: object) -> SimpleNamespace:
    base = {"plan_activation_status": None, "datos_alta": None, "payment_status": "pendiente"}
    return SimpleNamespace(**{**base, **campos})


def test_un_pedido_comun_no_trae_estado_de_plan() -> None:
    assert _estado_del_plan(_pedido(payment_status="pagado")) == {}


def test_plan_activado_dice_activo_y_que_el_acceso_llega_al_correo() -> None:
    pedido = _pedido(plan_activation_status="activada", payment_status="pagado", datos_alta={"email": "a@b.co"})
    assert _estado_del_plan(pedido) == {"estado_plan": "activo", "acceso": "enviado_al_correo_del_alta"}


@pytest.mark.parametrize(
    ("campos", "esperado"),
    [
        ({"plan_activation_status": "pendiente", "payment_status": "pagado"}, "activando"),
        ({"datos_alta": {"email": "a@b.co"}, "payment_status": "pagado"}, "activando"),
        ({"datos_alta": {"email": "a@b.co"}}, "pendiente_de_pago"),
    ],
)
def test_plan_sin_activar(campos: dict[str, object], esperado: str) -> None:
    assert _estado_del_plan(_pedido(**campos)) == {"estado_plan": esperado}
