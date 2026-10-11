"""El modelo ya no elige referencia ni monto de un cobro: `generar_link_pago` se retiró.

Hallazgo de `auditor-multitenant` (2026-10-10, severidad media, no cruza tenants): la
tool recibía `reference` y `amount` del LLM y buscaba las transacciones solo por
`tenant_id`. Un cliente que conociera el número de pedido de otro del mismo negocio
(`OC-0002`, secuencial) sabía si estaba pagado (`ya_pagado`) y su monto exacto
(`monto_no_coincide` lo citaba), y podía crear un link con esa referencia y un monto
inventado, que después bloqueaba el link del dueño del pedido.

ADR-0087 ya había sacado el monto del LLM en comercio (`confirmar_pago_pedido(order_id)`,
que filtra por cliente); la tool seguía ofrecida por pack-payments y nombrada en el
flujo de commerce. Ningún otro consumidor la usaba (cobranza usa la capacidad,
suscripciones `sub_link_pago`), así que se retira (adenda a ADR-0087, 2026-10-10).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pack_payments.config import PaymentsConfig
from pack_payments.module import PaymentsModule

from core.payments import PaymentRegistry

RAIZ = Path(__file__).resolve().parents[1]


class _Host:
    def __init__(self) -> None:
        self.skills: list[Any] = []

    def register_skill(self, skill: Any) -> None:
        self.skills.append(skill)


def _config() -> PaymentsConfig:
    return PaymentsConfig.model_validate(
        {"public_key": "pub_test_x", "private_key": "prv_test_x", "events_secret": "e", "sandbox": True}
    )


def test_pagos_no_le_ofrece_tools_al_modelo_y_sigue_dando_la_capacidad(monkeypatch) -> None:
    import pack_payments.module as modulo

    registro = PaymentRegistry()
    monkeypatch.setattr(modulo, "payment_registry", registro)
    module = PaymentsModule()
    module.config = _config()
    host = _Host()

    module.on_activate(host)  # type: ignore[arg-type]

    assert host.skills == []
    assert getattr(PaymentsModule, "declared_skills", []) == []  # tampoco en el catálogo de acciones
    # El link de un pedido sigue saliendo por la capacidad (lo pide pack-commerce).
    assert registro.capability_for({"pack_payments"}) is not None


def test_el_flujo_de_commerce_cobra_con_la_tool_que_filtra_por_cliente() -> None:
    flow = json.loads((RAIZ / "packages/pack-commerce/flow.json").read_text(encoding="utf-8"))
    tools = [t for seccion in flow["sections"] for t in seccion["tools"]]

    assert "generar_link_pago" not in tools
    pedido = next(s for s in flow["sections"] if s["id"] == "pedido")
    assert "confirmar_pago_pedido" in pedido["tools"]


def test_el_playbook_de_pagos_no_le_pide_referencia_ni_monto_al_modelo() -> None:
    texto = (RAIZ / "packages/pack-payments/playbooks/pagos.md").read_text(encoding="utf-8")

    assert "generar_link_pago" not in texto
    assert "`amount`" not in texto and "`reference`" not in texto
    assert "nunca pongas vos\n  el monto ni la referencia" in texto
