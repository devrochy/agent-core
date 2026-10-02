"""HU-COM-02: umbral de escalamiento por monto, distinto para planes.

Un pedido one-shot de 1.8M escala (protege); un plan recurrente de 1.8M no
(margen de negocio). El umbral de planes es configurable y `None` = nunca.
"""

from __future__ import annotations

from pack_commerce.skills import _monto_alto_threshold


class _Cfg:
    def __init__(self, alto: float = 1_000_000, planes: float | None = None) -> None:
        self.monto_alto_threshold = alto
        self.monto_alto_planes_threshold = planes


def test_oneshot_usa_el_umbral_normal() -> None:
    assert _monto_alto_threshold(_Cfg(alto=1_000_000), es_plan=False) == 1_000_000


def test_plan_no_escala_por_defecto() -> None:
    """`monto_alto_planes_threshold = None` -> un plan nunca escala por monto."""
    assert _monto_alto_threshold(_Cfg(alto=1_000_000, planes=None), es_plan=True) is None


def test_plan_usa_su_umbral_si_esta_configurado() -> None:
    assert _monto_alto_threshold(_Cfg(planes=5_000_000), es_plan=True) == 5_000_000


def test_plan_con_umbral_none_no_escala() -> None:
    assert _monto_alto_threshold(_Cfg(alto=100, planes=None), es_plan=True) is None
    assert _monto_alto_threshold(_Cfg(alto=100, planes=None), es_plan=False) == 100
