"""SPEC-40 · payload firmado de las opciones directas (unitarios, sin BD)."""

from __future__ import annotations

import uuid

import pytest

from core.kernel.opciones import payload as p

SECRETO = "secreto-de-prueba"
T1, T2 = uuid.uuid4(), uuid.uuid4()
C1, C2 = uuid.uuid4(), uuid.uuid4()
S1, S2 = uuid.uuid4(), uuid.uuid4()
H = p.hash_de_argumentos("consultar", {"id": "1"})


def _ok(payload: str, tenant=T1, conv=C1, args_hash: str = "") -> bool:
    leido = p.leer(payload)
    return leido is not None and p.firma_valida(leido, SECRETO, tenant, conv, args_hash)


def test_ida_y_vuelta_de_cada_tipo() -> None:
    opt = p.construir(SECRETO, "opt", T1, C1, S1, "estado")
    leido = p.leer(opt)
    assert leido is not None and (leido.tipo, leido.send_id, leido.option_id) == ("opt", S1, "estado")
    assert _ok(opt)
    for tipo in ("optc", "optn"):
        pl = p.construir(SECRETO, tipo, T1, C1, S1, "pagar", H)
        assert pl.startswith(f"{tipo}:") and _ok(pl, args_hash=H)


def test_el_largo_maximo_cabe_en_70() -> None:
    mayor = max(
        len(p.construir(SECRETO, t, T1, C1, S1, "a" * 12, H if t != "opt" else "")) for t in p.TIPOS
    )
    assert mayor <= p.MAX_PAYLOAD == 70
    assert mayor == 67  # optc: + 32 + : + 12 + : + 16


def test_la_firma_ata_negocio_conversacion_envio_y_opcion() -> None:
    pl = p.construir(SECRETO, "opt", T1, C1, S1, "estado")
    assert not _ok(pl, tenant=T2)   # otro negocio
    assert not _ok(pl, conv=C2)     # la misma pulsación reenviada a otra conversación
    leido = p.leer(pl)
    assert leido is not None
    # otro envío u otra opción con la firma ya emitida
    otro_envio = p.PayloadLeido(leido.tipo, S2, leido.option_id, leido.firma)
    otra_opcion = p.PayloadLeido(leido.tipo, leido.send_id, "otra", leido.firma)
    assert not p.firma_valida(otro_envio, SECRETO, T1, C1)
    assert not p.firma_valida(otra_opcion, SECRETO, T1, C1)
    assert not p.firma_valida(leido, "otro-secreto", T1, C1)


def test_manipular_cualquier_caracter_invalida() -> None:
    pl = p.construir(SECRETO, "opt", T1, C1, S1, "estado")
    for i in range(len(pl)):
        cambiado = pl[:i] + ("0" if pl[i] != "0" else "1") + pl[i + 1 :]
        assert not _ok(cambiado), cambiado


def test_el_tipo_va_dentro_de_la_firma() -> None:
    """Un `optn:` (cancelar) no se convierte en `optc:` (confirmar) cambiando el prefijo."""
    cancelar = p.construir(SECRETO, "optn", T1, C1, S1, "pagar", H)
    como_confirmar = "optc:" + cancelar.split(":", 1)[1]
    assert _ok(cancelar, args_hash=H)
    assert not _ok(como_confirmar, args_hash=H)
    como_opt = "opt:" + cancelar.split(":", 1)[1]
    assert not _ok(como_opt)


def test_optc_y_optn_atan_los_argumentos() -> None:
    pl = p.construir(SECRETO, "optc", T1, C1, S1, "pagar", H)
    assert not _ok(pl, args_hash=p.hash_de_argumentos("consultar", {"id": "2"}))
    assert not _ok(pl, args_hash="")  # sin hash no se verifica
    # `opt:` no admite hash y `optc:` lo exige
    leido = p.leer(p.construir(SECRETO, "opt", T1, C1, S1, "estado"))
    assert leido is not None and not p.firma_valida(leido, SECRETO, T1, C1, H)
    with pytest.raises(ValueError):
        p.construir(SECRETO, "optc", T1, C1, S1, "pagar")
    with pytest.raises(ValueError):
        p.construir(SECRETO, "opt", T1, C1, S1, "estado", H)


@pytest.mark.parametrize("opcion", ["", "A", "con-guion", "a" * 13, "con espacio", "ñ"])
def test_rechaza_ids_de_opcion_no_firmables(opcion: str) -> None:
    with pytest.raises(ValueError):
        p.construir(SECRETO, "opt", T1, C1, S1, opcion)


@pytest.mark.parametrize(
    "basura",
    ["", "opt:", "opt:xx:estado:" + "0" * 16, "confirmar:" + str(S1) + ":1:" + "0" * 16,
     "opt:" + S1.hex + ":estado:" + "G" * 16, "OPT:" + S1.hex + ":estado:" + "0" * 16, None],
)
def test_leer_devuelve_none_si_la_forma_no_es_exacta(basura) -> None:
    assert p.leer(basura) is None


def test_es_de_opciones_incluye_lo_manipulado_y_excluye_otros_botones() -> None:
    assert p.es_de_opciones("opt:basura") and p.es_de_opciones("optc:x") and p.es_de_opciones("optn:x")
    assert not p.es_de_opciones("confirmar:abc") and not p.es_de_opciones("plan:premium")
    assert not p.es_de_opciones("options:x") and not p.es_de_opciones(None) and not p.es_de_opciones("")


def test_el_secreto_del_servidor_se_deriva_y_es_estable() -> None:
    assert p.secreto_del_servidor() == p.secreto_del_servidor()
    from core.config import get_settings

    assert p.secreto_del_servidor() != get_settings().app_secret_key


def test_hash_de_argumentos_es_canonico_y_distingue_la_tool() -> None:
    assert p.hash_de_argumentos("t", {"a": 1, "b": 2}) == p.hash_de_argumentos("t", {"b": 2, "a": 1})
    assert p.hash_de_argumentos("t", {"a": 1}) != p.hash_de_argumentos("u", {"a": 1})
    assert len(p.hash_de_argumentos("t", None)) == 32
