"""HU-CRM-01: foto del producto por URL.

La regla de la foto que adjunta el core (`_image_url`, ver safe_renderer) es la de
agent-sale (`extractSingleMatchImageUrl`): solo con un único resultado pedido por SKU o
por el nombre exacto (D-SPEC-33-1). La API solo acepta links https.
"""

from __future__ import annotations

import pytest
from pack_commerce.admin_api import IMAGE_URL_MAX, ProductOut, ProductSave
from pack_commerce.skills import _foto_del_unico
from pydantic import ValidationError

FOTO = "https://cdn.ejemplo.com/casco-rojo.jpg"


def _m(variant_id: str, name: str) -> dict:
    return {"variant_id": variant_id, "name": name}


def test_lista_de_varios_productos_no_lleva_foto() -> None:
    matches = [_m("v1", "Casco Rojo"), _m("v2", "Casco Azul")]
    fotos = {"v1": FOTO, "v2": FOTO}
    assert _foto_del_unico("", "casco", matches, fotos) is None
    assert _foto_del_unico("", "Casco Rojo", matches, fotos) is None


def test_unico_resultado_por_sku_con_foto_la_lleva() -> None:
    assert _foto_del_unico("CR-01", "", [_m("v1", "Casco Rojo")], {"v1": FOTO}) == FOTO


def test_unico_resultado_por_nombre_exacto_la_lleva() -> None:
    assert _foto_del_unico("", "  casco ROJO ", [_m("v1", "Casco Rojo")], {"v1": FOTO}) == FOTO


def test_busqueda_por_texto_con_un_resultado_no_lleva_foto() -> None:
    """"qué cascos tienen" es una exploración, aunque devuelva uno solo (agent-sale)."""
    assert _foto_del_unico("", "cascos rojos", [_m("v1", "Casco Rojo")], {"v1": FOTO}) is None


def test_producto_sin_url_no_lleva_foto() -> None:
    assert _foto_del_unico("CR-01", "", [_m("v1", "Casco Rojo")], {"v1": ""}) is None
    assert _foto_del_unico("CR-01", "", [_m("v1", "Casco Rojo")], {}) is None


def test_url_vieja_que_no_es_https_no_se_manda() -> None:
    """El seed o datos viejos pueden traer http://: WhatsApp no la recibe y no se adjunta."""
    assert _foto_del_unico("CR-01", "", [_m("v1", "Casco Rojo")], {"v1": "http://x.com/a.jpg"}) is None


@pytest.mark.parametrize(
    "url",
    ["http://cdn.ejemplo.com/a.jpg", "ftp://x/a.jpg", "javascript:alert(1)", "cdn.ejemplo.com/a.jpg",
     "https://", "https://x.com/a b.jpg", "https://x.com/" + "a" * IMAGE_URL_MAX],
)
def test_api_rechaza_url_que_no_es_https(url: str) -> None:
    with pytest.raises(ValidationError) as exc:
        ProductSave(name="Casco", image_url=url)
    assert [e["loc"] for e in exc.value.errors()] == [("image_url",)]


def test_api_acepta_https_y_vacio_para_quitarla() -> None:
    assert ProductSave(name="Casco", image_url=f"  {FOTO} ").image_url == FOTO
    assert ProductSave(name="Casco", image_url="").image_url == ""
    assert ProductSave(name="Casco").image_url is None
    assert "image_url" in ProductOut.model_fields
