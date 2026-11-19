"""HU-COM-03 (D4/D17): numeración por prefijo del negocio sin truncar y
campos de checkout configurables por tipo de producto y de entrega."""

from __future__ import annotations

import pytest
from pack_commerce.config import CRMConfig
from pack_commerce.skills import (
    _campos_requeridos,
    _monto_alto_threshold,
    _normalizar_prefijo,
    _validar_checkout,
)


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


def test_los_planes_ya_no_son_de_commerce() -> None:
    """HU-COM-17: el checkout no tiene lista `plan` ni umbral propio de planes."""
    cfg = CRMConfig()
    assert "plan" not in cfg.checkout_fields
    assert "monto_alto_planes_threshold" not in CRMConfig.model_fields


def test_config_guardada_antes_con_la_clave_plan_sigue_cargando() -> None:
    """Una configuración vieja (con `checkout_fields['plan']` y el umbral de planes) no rompe la carga:
    el modelo ignora la clave extra y el campo nuevo no existe (HU-COM-17)."""
    cfg = CRMConfig.model_validate(
        {
            "checkout_fields": {"domicilio": ["full_name"], "plan": ["email"]},
            "monto_alto_planes_threshold": 5_000_000,
        }
    )
    assert _campos_requeridos(cfg, delivery_method="domicilio") == ["full_name"]
    assert not hasattr(cfg, "monto_alto_planes_threshold")


def test_umbral_de_monto_alto_es_el_del_negocio() -> None:
    assert _monto_alto_threshold(CRMConfig()) == 1_000_000
    assert _monto_alto_threshold(CRMConfig(monto_alto_threshold=2_500_000)) == 2_500_000


def test_retiro_sin_direccion() -> None:
    cfg = CRMConfig()
    campos = _campos_requeridos(cfg, delivery_method="recoger_en_tienda")
    assert "address" not in campos
    assert "full_name" in campos


def test_domicilio_con_direccion() -> None:
    cfg = CRMConfig()
    campos = _campos_requeridos(cfg, delivery_method="domicilio")
    assert "address" in campos and "municipality" in campos and "city" in campos


def test_campos_configurables_por_negocio() -> None:
    cfg = CRMConfig(
        checkout_fields={
            "domicilio": ["full_name"],
            "recoger_en_tienda": [],
        }
    )
    assert _campos_requeridos(cfg, delivery_method="domicilio") == ["full_name"]
    assert _campos_requeridos(cfg, delivery_method="recoger_en_tienda") == []


def test_validar_checkout_devuelve_faltantes() -> None:
    completo, faltan = _validar_checkout(["full_name", "email"], {"full_name": "Ana"})
    assert completo is False
    assert faltan == ["email"]
    completo, _ = _validar_checkout(["full_name"], {"full_name": "Ana"})
    assert completo is True


def test_checkout_fields_desconocidos_se_rechazan() -> None:
    with pytest.raises(ValueError):
        CRMConfig(checkout_fields={"domicilio": ["no_existe"]})
