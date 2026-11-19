"""SPEC-43 · HU-SUB-33 · cálculo y reglas de las promociones de plan (sin base de datos)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pack_subscriptions.promociones import (
    DatosDePromocion,
    PromoError,
    calcular,
    huella_de,
    normalizar_codigo,
    validar,
)

from core.subscriptions import SubscriptionError


def _datos(**kw) -> DatosDePromocion:
    base = {"etiqueta": "Lanzamiento", "tipo_descuento": "porcentaje", "valor": Decimal("20"), "codigo": "LANZAMIENTO"}
    return DatosDePromocion(**{**base, **kw})


@pytest.mark.parametrize(
    ("precio", "tipo", "valor", "moneda", "descuento", "neto"),
    [
        ("100000", "porcentaje", "20", "COP", "20000", "80000"),
        ("99000", "porcentaje", "15", "COP", "14850", "84150"),
        # El descuento se redondea al peso, media hacia arriba: 33 % de 99.999 = 32.999,67 → 33.000.
        ("99999", "porcentaje", "33", "COP", "33000", "66999"),
        ("100000", "monto", "25000", "COP", "25000", "75000"),
        # Otra moneda redondea al centavo.
        ("19.99", "porcentaje", "10", "USD", "2.00", "17.99"),
    ],
)
def test_calcular_descuento_y_neto(precio, tipo, valor, moneda, descuento, neto) -> None:
    d, n = calcular(Decimal(precio), tipo, Decimal(valor), moneda)
    assert (d, n) == (Decimal(descuento), Decimal(neto))


@pytest.mark.parametrize(("tipo", "valor"), [("monto", "100000"), ("monto", "150000"), ("porcentaje", "99.99")])
def test_un_descuento_que_deja_el_neto_en_cero_se_rechaza(tipo, valor) -> None:
    # 99,99 % de 100 (COP) redondea a 100: el neto sería 0. Un monto igual o mayor al precio, también.
    precio = Decimal("100") if tipo == "porcentaje" else Decimal("100000")
    with pytest.raises(SubscriptionError) as error:
        calcular(precio, tipo, Decimal(valor), "COP")
    assert error.value.code == "descuento_excede_precio"


def test_una_promocion_que_no_descuenta_nada_no_es_valida() -> None:
    # 1 % de $10 (COP) redondea a 0.
    with pytest.raises(SubscriptionError) as error:
        calcular(Decimal("10"), "porcentaje", Decimal("1"), "COP")
    assert error.value.code == "codigo_no_valido"


@pytest.mark.parametrize("valor", ["0", "100", "-5", "100.01", "abc"])
def test_porcentaje_fuera_de_1_a_99_se_rechaza(valor) -> None:
    with pytest.raises(PromoError):
        validar(_datos(valor=valor))


def test_porcentaje_de_1_y_de_99_son_validos() -> None:
    assert validar(_datos(valor=Decimal("1"))).valor == Decimal("1.00")
    assert validar(_datos(valor=Decimal("99"))).valor == Decimal("99.00")


def test_un_monto_fijo_con_dos_o_mas_periodos_se_rechaza() -> None:
    with pytest.raises(PromoError) as error:
        validar(_datos(tipo_descuento="monto", valor=Decimal("5000"), periodos=2), max_periodos=12)
    assert error.value.code == "monto_un_periodo"


def test_la_api_acepta_de_1_a_12_periodos_y_rechaza_el_resto() -> None:
    assert validar(_datos(periodos=3)).periodos == 3
    assert validar(_datos(periodos=12)).periodos == 12
    with pytest.raises(PromoError) as error:
        validar(_datos(periodos=13))
    assert error.value.code == "periodos_invalidos"


@pytest.mark.parametrize("codigo", ["ab", "con espacio", "-empieza", "x" * 41, "ñandú"])
def test_codigo_con_forma_invalida_se_rechaza(codigo) -> None:
    with pytest.raises(PromoError) as error:
        validar(_datos(codigo=codigo))
    assert error.value.code == "codigo_invalido"


def test_el_codigo_se_guarda_en_minusculas_y_sin_espacios_en_los_bordes() -> None:
    assert validar(_datos(codigo="  LanzaMiento-26 ")).codigo == "lanzamiento-26"
    assert normalizar_codigo("   ") is None
    assert normalizar_codigo(None) is None


def test_vigencia_que_termina_antes_de_empezar_se_rechaza() -> None:
    from datetime import UTC, datetime, timedelta

    ahora = datetime.now(UTC)
    with pytest.raises(PromoError) as error:
        validar(_datos(vigente_desde=ahora, vigente_hasta=ahora - timedelta(days=1)))
    assert error.value.code == "vigencia_invalida"


def test_tope_de_canjes_debe_ser_positivo() -> None:
    with pytest.raises(PromoError):
        validar(_datos(max_canjes=0))


def test_huella_documento_manda_sobre_telefono_y_nunca_es_el_dato_en_claro(monkeypatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "prueba-huella")
    con_doc = huella_de(documento="CC 1.234.567", telefono_canal="+573001112233", telefono_datos=None)
    mismo_doc_otro_tel = huella_de(documento="1234567", telefono_canal="+573009998877", telefono_datos=None)
    assert con_doc is not None and con_doc.startswith("doc:") and con_doc == mismo_doc_otro_tel
    assert "1234567" not in con_doc
    # Sin documento, la huella es del teléfono (el del canal primero) y es la misma con otro formato.
    tel = huella_de(documento=None, telefono_canal="+57 300 111 2233", telefono_datos=None)
    assert tel is not None and tel.startswith("tel:")
    assert tel == huella_de(documento=None, telefono_canal=None, telefono_datos="3001112233")
    # Sin documento ni teléfono no hay huella: el candado por persona no aplica (límite documentado).
    assert huella_de(documento=None, telefono_canal=None, telefono_datos="") is None
