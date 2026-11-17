"""SPEC-48, HU-COM-21 · formas de pago del agente de Ventas (reglas puras, resolver y referencia).

- Sin configuración se reproduce lo de antes: contra entrega, transferencia si hay cuentas y pago en
  línea si el negocio cobra en línea.
- Encendida sin lo que necesita = «Falta configurar» y no se ofrece.
- Al menos una forma encendida; sin repetidos ni formas inventadas.
- La referencia del turno lleva solo ids y si/no: nunca cuentas ni instrucciones.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pack_commerce import formas_de_pago as fp
from pack_commerce.config import CRMConfig
from pydantic import ValidationError

CUENTA = {"entity": "Bancolombia", "accountType": "Ahorros", "accountNumber": "123-456789-01", "holderName": "Tienda", "holderDocument": "900"}


def _formas(**encendidas: bool) -> list[dict]:
    return [{"method": m, "enabled": encendidas.get(m, False), "instructions": ""} for m in fp.METODOS]


def test_sin_configurar_se_deduce_como_antes() -> None:
    assert fp.metodos_activos(None, [], False) == ["efectivo_contraentrega"]
    assert fp.metodos_activos(None, [CUENTA], True) == ["efectivo_contraentrega", "transferencia", "pago_en_linea"]


def test_transferencia_sin_cuenta_falta_configurar_y_no_se_ofrece() -> None:
    formas = _formas(efectivo_contraentrega=True, transferencia=True)
    estado = {e.method: e for e in fp.estados(formas, [], False)}
    assert estado["transferencia"].estado == "falta_configurar"
    assert "cuenta" in estado["transferencia"].falta
    assert fp.metodos_activos(formas, [], False) == ["efectivo_contraentrega"]
    assert fp.metodos_activos(formas, [CUENTA], False) == ["efectivo_contraentrega", "transferencia"]


def test_pago_en_linea_sin_proveedor_falta_configurar() -> None:
    estado = {e.method: e for e in fp.estados(_formas(pago_en_linea=True), [], False)}
    assert estado["pago_en_linea"].estado == "falta_configurar"
    assert estado["efectivo_contraentrega"].estado == "apagada"


def test_contra_entrega_no_tiene_superpoder_y_las_demas_si() -> None:
    estado = {e.method: e for e in fp.estados(None, [CUENTA], True)}
    assert estado["efectivo_contraentrega"].superpoder is None
    assert estado["transferencia"].superpoder == "pack_commerce.comprobar_transferencia"
    assert estado["pago_en_linea"].superpoder == "pack_commerce.pago_en_linea"


def test_normalizar_formas_orden_fijo_y_completa() -> None:
    salida = fp.normalizar_formas([{"method": "transferencia", "enabled": True}])
    assert [f["method"] for f in salida] == list(fp.METODOS)
    assert [f["enabled"] for f in salida] == [False, True, False]


@pytest.mark.parametrize(
    "crudo",
    [
        [{"method": "cripto", "enabled": True}],
        [{"method": "transferencia", "enabled": True}, {"method": "transferencia", "enabled": False}],
        [{"method": "efectivo_contraentrega", "enabled": False}],
        [{"method": "transferencia", "enabled": True, "instructions": "x" * 501}],
        "no es lista",
    ],
)
def test_normalizar_formas_rechaza(crudo) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        fp.normalizar_formas(crudo)


def test_cuentas_estrictas_en_el_guardado_y_tolerantes_al_cargar() -> None:
    with pytest.raises(ValueError):
        fp.normalizar_cuentas([{"entity": "Nequi"}])
    # Una config ya guardada con una cuenta a medias sigue cargando.
    config = CRMConfig(transfer_accounts=[{"entity": "Nequi"}, "basura"])
    assert config.transfer_accounts == [{"entity": "Nequi", "accountType": "", "accountNumber": "", "holderName": "", "holderDocument": ""}]
    assert fp.ultimos_4("123-456789-01") == "8901"


def test_config_rechaza_formas_invalidas_y_lectura_sin_comprobante() -> None:
    assert CRMConfig().payment_forms is None
    with pytest.raises(ValidationError):
        CRMConfig(payment_forms=[{"method": "efectivo_contraentrega", "enabled": False}])
    with pytest.raises(ValidationError):
        CRMConfig(transfer_proof_required=False, transfer_ocr_enabled=True)


def test_las_formas_y_cuentas_son_del_agente() -> None:
    from pack_commerce.module import CRMModule

    campos = set(CRMModule.config_agente.model_fields)
    assert {"payment_forms", "transfer_accounts", "transfer_proof_required", "transfer_ocr_enabled"} <= campos


# ------------------------------------------------------------------ resolver y referencia
@pytest.mark.asyncio
async def test_resolver_solo_acepta_formas_activas(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import procedimientos

    async def _activos(_agent):  # type: ignore[no-untyped-def]
        return ["efectivo_contraentrega"]

    async def _online() -> bool:
        return True

    monkeypatch.setattr(fp, "metodos_configurados", _activos)
    monkeypatch.setattr(procedimientos, "_pago_en_linea_disponible", _online)
    ctx = SimpleNamespace(agent_id=uuid.uuid4())
    assert (await procedimientos.metodo_de_pago("efectivo_contraentrega", ctx, {})).ok
    rechazo = await procedimientos.metodo_de_pago("transferencia", ctx, {})
    assert not rechazo.ok and "contra entrega" in (rechazo.error or "")


@pytest.mark.asyncio
async def test_resolver_sin_proveedor_explica_el_pago_en_linea(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import procedimientos

    async def _activos(_agent):  # type: ignore[no-untyped-def]
        return ["efectivo_contraentrega"]

    async def _online() -> bool:
        return False

    monkeypatch.setattr(fp, "metodos_configurados", _activos)
    monkeypatch.setattr(procedimientos, "_pago_en_linea_disponible", _online)
    rechazo = await procedimientos.metodo_de_pago("pago_en_linea", SimpleNamespace(agent_id=None), {})
    assert not rechazo.ok and "no está disponible" in (rechazo.error or "")


@pytest.mark.asyncio
async def test_sin_formas_guardadas_el_resolver_sigue_como_antes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import procedimientos

    async def _sin_configurar(_agent):  # type: ignore[no-untyped-def]
        return None

    async def _online() -> bool:
        return False

    monkeypatch.setattr(fp, "metodos_configurados", _sin_configurar)
    monkeypatch.setattr(procedimientos, "_pago_en_linea_disponible", _online)
    ctx = SimpleNamespace(agent_id=uuid.uuid4())
    assert (await procedimientos.metodo_de_pago("transferencia", ctx, {})).ok
    assert not (await procedimientos.metodo_de_pago("pago_en_linea", ctx, {})).ok


@pytest.mark.asyncio
async def test_referencia_lleva_solo_ids_y_si_no(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import skills

    from core.orchestrator.referencias import renderizar
    from core.shared import release_vigente

    async def _activos(_agent):  # type: ignore[no-untyped-def]
        return ["efectivo_contraentrega", "transferencia"]

    monkeypatch.setattr(fp, "metodos_del_agente", _activos)
    monkeypatch.setattr(release_vigente, "agente_del_turno", lambda: SimpleNamespace(agent_id=uuid.uuid4()))
    [ref] = await skills._referencia_de_formas()
    linea = renderizar(ref)
    assert linea == "- formas_de_pago: efectivo_contraentrega=si, transferencia=si, pago_en_linea=no"
    monkeypatch.setattr(release_vigente, "agente_del_turno", lambda: None)
    assert await skills._referencia_de_formas() == []
