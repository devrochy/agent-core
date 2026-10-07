"""HU-COM-03 (D4/D17): numeración por prefijo del negocio sin truncar y
campos de checkout configurables por tipo de producto y de entrega."""

from __future__ import annotations

import pytest
from pack_commerce.config import CRMConfig
from pack_commerce.skills import _campos_requeridos, _normalizar_prefijo, _validar_checkout


def test_prefijo_normalizado() -> None:
    assert _normalizar_prefijo("oc") == "OC"
    assert _normalizar_prefijo(" SH ") == "SH"
    assert _normalizar_prefijo("") == "OC"
    assert _normalizar_prefijo("a-b") == "OC"
    assert _normalizar_prefijo("1234567890") == "OC"


def test_numero_no_se_trunca_a_partir_de_diez_mil() -> None:
    assert f"OC-{10_000:04d}" == "OC-10000"
    assert f"OC-{1_000:04d}" == "OC-1000"
    assert "OC-10000" != "OC-1000"


def test_plan_sin_direccion() -> None:
    cfg = CRMConfig()
    campos = _campos_requeridos(cfg, es_plan=True, delivery_method="domicilio")
    assert "address" not in campos
    assert "municipality" not in campos and "city" not in campos
    assert "email" in campos


def test_retiro_sin_direccion() -> None:
    cfg = CRMConfig()
    campos = _campos_requeridos(cfg, es_plan=False, delivery_method="recoger_en_tienda")
    assert "address" not in campos
    assert "full_name" in campos


def test_domicilio_con_direccion() -> None:
    cfg = CRMConfig()
    campos = _campos_requeridos(cfg, es_plan=False, delivery_method="domicilio")
    assert "address" in campos and "municipality" in campos and "city" in campos


def test_campos_configurables_por_negocio() -> None:
    cfg = CRMConfig(
        checkout_fields={
            "domicilio": ["full_name"],
            "recoger_en_tienda": [],
            "plan": ["email"],
        }
    )
    assert _campos_requeridos(cfg, es_plan=True, delivery_method="domicilio") == ["email"]
    assert _campos_requeridos(cfg, es_plan=False, delivery_method="domicilio") == ["full_name"]
    assert _campos_requeridos(cfg, es_plan=False, delivery_method="recoger_en_tienda") == []


def test_validar_checkout_devuelve_faltantes() -> None:
    completo, faltan = _validar_checkout(["full_name", "email"], {"full_name": "Ana"})
    assert completo is False
    assert faltan == ["email"]
    completo, _ = _validar_checkout(["full_name"], {"full_name": "Ana"})
    assert completo is True


def test_checkout_fields_desconocidos_se_rechazan() -> None:
    with pytest.raises(ValueError):
        CRMConfig(checkout_fields={"domicilio": ["no_existe"]})
