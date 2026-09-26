"""Capacidad de pagos del core (ADR-0067)."""

from __future__ import annotations

from core.payments import PaymentLink, PaymentMethod, payment_registry, qr_png_data_uri


class _Cap:
    plugin_id = "demo_pagos"

    def methods(self) -> list[PaymentMethod]:
        return [PaymentMethod(id="x", label="X")]

    async def create_link(self, *, reference: str, amount: float, description: str = "", method: str | None = None) -> PaymentLink:
        return PaymentLink(reference=reference, url="https://x", qr_payload="https://x")


def test_registry_filtra_por_pack_habilitado() -> None:
    cap = _Cap()
    payment_registry.register("demo_pagos", cap)
    try:
        assert payment_registry.capability_for({"demo_pagos"}) is cap
        assert payment_registry.capability_for({"otro"}) is None
    finally:
        payment_registry.unregister("demo_pagos")


def test_qr_png_data_uri() -> None:
    assert qr_png_data_uri("") is None
    data = qr_png_data_uri("https://checkout.example/l/x")
    assert data is None or data.startswith("data:image/png;base64,")
