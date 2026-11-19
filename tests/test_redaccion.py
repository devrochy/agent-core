"""HU-AOS-28 · redacción al leer (ADR-0169 §3): forma visible, datos personales enmascarados."""

from __future__ import annotations

from core.shared.redaccion import OCULTO, clave_sensible, enmascarar, enmascarar_texto


def test_claves_sensibles_por_segmento_y_sin_tildes() -> None:
    for clave in ("email", "customer_email", "datos.teléfono", "cardNumber", "numero_documento", "Dirección", "api-token"):
        assert clave_sensible(clave), clave
    # «pan» (producto) o «clave_producto» no son datos personales.
    for clave in ("total", "estado", "pedido_id", "pan_integral", "clave_producto", "cuenta_de_cobro", "", None):
        assert not clave_sensible(clave), clave


def test_por_clave_conserva_una_pista_minima() -> None:
    salida = enmascarar(
        {"email": "ana.perez@correo.co", "telefono": "+57 300 123 4821", "cedula": 1053123456, "direccion": "Cra 23 # 45-10"}
    )
    assert salida == {"email": "a***@correo.co", "telefono": f"{OCULTO}4821", "cedula": f"{OCULTO}3456", "direccion": OCULTO}


def test_codigos_montos_y_estados_quedan_tal_cual() -> None:
    entrada = {"pedido": "PED-2041", "total": 185000, "estado": "pagado", "items": [{"sku": "A1", "cantidad": 2}], "ok": True}
    assert enmascarar(entrada) == entrada


def test_patrones_dentro_de_textos_libres() -> None:
    assert enmascarar({"nota": "escribir a luis@x.com o al 3001234567"}) == {"nota": f"escribir a l***@x.com o al {OCULTO}4567"}
    assert enmascarar_texto("año 2026, total 185.000") == "año 2026, total 185.000"


def test_clase_del_dato_manda_y_pago_siempre() -> None:
    assert enmascarar("Ana Pérez", data_class="pii") == OCULTO
    assert enmascarar("4111111111111111", data_class="payment") == f"{OCULTO}1111"
    assert enmascarar({"numero": "4111", "vence": "12/29"}, clave="tarjeta") == {"numero": OCULTO, "vence": OCULTO}


def test_no_muta_la_entrada_y_corta_la_profundidad() -> None:
    entrada = {"cliente": {"email": "a@b.co"}}
    enmascarar(entrada)
    assert entrada == {"cliente": {"email": "a@b.co"}}
    profundo: dict = {}
    nodo = profundo
    for _ in range(12):
        nodo["x"] = {}
        nodo = nodo["x"]
    salida = enmascarar(profundo)
    for _ in range(8):
        salida = salida["x"]
    assert salida == OCULTO
