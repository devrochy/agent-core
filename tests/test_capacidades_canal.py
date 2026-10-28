"""Pista C1 · tabla de capacidades por canal (SPEC-40, `connection_types.CAPACIDADES_POR_TIPO`).

La tabla sustituye los `if channel == ...` de `kernel/opciones/envio.py`, `gateway/sender.py` y
`orchestrator/opciones_toque.py` SIN cambiar comportamiento: estos tests fijan lo que cada canal admite hoy.
"""

from __future__ import annotations

import pytest

from core.gateway import connection_types as ct
from core.gateway.connection_types import (
    CAPACIDADES_POR_TIPO,
    CONNECTION_TYPES,
    SOLO_TEXTO,
    CapacidadesCanal,
    canales_con,
    capacidades_de,
    capacidades_del_canal,
)

META = CapacidadesCanal(botones=True, lista=True, plantilla_aprobada=True, ventana_24h=True)
PLAYGROUND = CapacidadesCanal(botones=True, lista=True)


def test_todo_tipo_de_conexion_con_canal_declara_sus_capacidades() -> None:
    """Un canal nuevo en el catálogo sin entrada en la tabla haría fallar este test: declara qué admite a propósito
    en vez de quedar como solo texto por omisión (o, peor, heredar botones por un `if` olvidado)."""
    con_canal = {t.id for t in CONNECTION_TYPES if t.channel}
    assert con_canal <= set(CAPACIDADES_POR_TIPO), f"tipos sin capacidades: {sorted(con_canal - set(CAPACIDADES_POR_TIPO))}"
    # Y al revés: ninguna entrada de la tabla apunta a un tipo que ya no existe.
    assert set(CAPACIDADES_POR_TIPO) <= {t.id for t in CONNECTION_TYPES}


@pytest.mark.parametrize(
    ("canal", "proveedor", "esperado"),
    [
        ("whatsapp", "meta", META),
        ("whatsapp", "waweb", SOLO_TEXTO),
        ("whatsapp", "twilio", SOLO_TEXTO),
        ("whatsapp", "plivo", SOLO_TEXTO),
        ("web", "web", SOLO_TEXTO),
        ("instagram", "meta", SOLO_TEXTO),
        ("messenger", "meta", SOLO_TEXTO),
        ("email", "smtp", SOLO_TEXTO),
        ("test", "internal", PLAYGROUND),
    ],
)
def test_capacidades_de_cada_canal_y_proveedor(canal: str, proveedor: str, esperado: CapacidadesCanal) -> None:
    assert capacidades_de(canal, proveedor) == esperado


@pytest.mark.parametrize(
    ("canal", "esperado"),
    [
        # Sin proveedor (lo que pasa `opciones_envio` fuera de WhatsApp): vale lo que comparten los tipos del canal.
        ("web", SOLO_TEXTO),
        ("instagram", SOLO_TEXTO),
        ("messenger", SOLO_TEXTO),
        ("email", SOLO_TEXTO),
        ("test", PLAYGROUND),
        # WhatsApp sin proveedor o con uno desconocido: los proveedores difieren, así que solo texto (antes:
        # `proveedor != "meta"`).
        ("whatsapp", SOLO_TEXTO),
        # Canal inexistente o vacío: solo texto.
        ("sms", SOLO_TEXTO),
        ("", SOLO_TEXTO),
    ],
)
def test_sin_proveedor_o_desconocido_gana_lo_que_comparte_el_canal_o_solo_texto(canal: str, esperado: CapacidadesCanal) -> None:
    assert capacidades_de(canal, "") == esperado
    assert capacidades_de(canal, None) == esperado


def test_whatsapp_con_proveedor_desconocido_es_solo_texto() -> None:
    assert capacidades_de("whatsapp", "otro") == SOLO_TEXTO


def test_capacidades_del_canal_es_lo_maximo_de_sus_proveedores() -> None:
    assert capacidades_del_canal("whatsapp") == META
    assert capacidades_del_canal("test") == PLAYGROUND
    for canal in ("web", "instagram", "messenger", "email", "sms", ""):
        assert capacidades_del_canal(canal) == SOLO_TEXTO


def test_ningun_canal_interpreta_hoy_la_respuesta_numerica() -> None:
    """C2 lo enciende en los canales de texto; mientras, «1» llega al LLM en todos."""
    assert not any(c.respuesta_numerica for c in CAPACIDADES_POR_TIPO.values())


def test_admite_distingue_botones_y_lista() -> None:
    assert META.admite("button") and META.admite("list")
    assert not SOLO_TEXTO.admite("button") and not SOLO_TEXTO.admite("list")
    assert not CapacidadesCanal(botones=True).admite("list")
    assert not META.admite("carousel") and not META.admite(None)


def test_canales_con_solo_lista_canales_de_clientes() -> None:
    assert canales_con("botones") == ["whatsapp"]  # el playground (`test`) tiene botones pero no es un canal de clientes
    assert canales_con("plantilla_aprobada") == ["whatsapp"]
    assert canales_con("ventana_24h") == ["whatsapp"]
    assert canales_con("respuesta_numerica") == []
    with pytest.raises(ValueError):
        canales_con("inventada")


def test_la_tabla_cubre_los_canales_que_guardan_las_conversaciones() -> None:
    """Los valores de `Conversation.channel` en uso salen de este catálogo; si uno nuevo no está, el decisor lo
    trataría como solo texto sin que nadie lo haya decidido."""
    en_uso = {"whatsapp", "messenger", "instagram", "web", "email", "test"}
    assert en_uso <= {t.channel for t in CONNECTION_TYPES if t.channel}


def test_el_modulo_no_deja_capacidades_sin_campo() -> None:
    assert set(CapacidadesCanal.__dataclass_fields__) == {
        "botones", "lista", "plantilla_aprobada", "ventana_24h", "respuesta_numerica"
    }
    assert ct.SOLO_TEXTO is SOLO_TEXTO


# ---------------------------------------------------------------- la cascada de envío sigue la tabla
@pytest.mark.parametrize(
    ("canal", "proveedor"),
    [("whatsapp", "waweb"), ("whatsapp", ""), ("whatsapp", "twilio"), ("web", ""), ("instagram", ""), ("messenger", ""), ("email", "")],
)
@pytest.mark.parametrize("ventana", [True, False])
def test_decidir_via_canales_de_texto_sale_texto_numerado(canal: str, proveedor: str, ventana: bool) -> None:
    from core.kernel.opciones import envio

    d = envio.decidir_via(
        canal=canal, proveedor=proveedor, ventana_abierta=ventana, interactivo_valido=True, plantilla_utilizable=True
    )
    assert d == envio.Decision(envio.TEXTO, envio.CANAL_SIN_BOTONES)


def test_decidir_via_playground_ignora_ventana_y_plantilla() -> None:
    from core.kernel.opciones import envio

    for ventana in (True, False):
        for plantilla in (True, False):
            kw = dict(canal="test", proveedor="", ventana_abierta=ventana, plantilla_utilizable=plantilla)
            assert envio.decidir_via(interactivo_valido=True, **kw) == envio.Decision(envio.INTERACTIVO)
            assert envio.decidir_via(interactivo_valido=False, **kw) == envio.Decision(envio.TEXTO, envio.FUERA_DE_TOPES)


def test_decidir_via_whatsapp_meta_sigue_la_ventana_y_la_plantilla() -> None:
    from core.kernel.opciones import envio

    kw = dict(canal="whatsapp", proveedor="meta", interactivo_valido=True)
    assert envio.decidir_via(ventana_abierta=True, plantilla_utilizable=False, **kw) == envio.Decision(envio.INTERACTIVO)
    assert envio.decidir_via(ventana_abierta=False, plantilla_utilizable=True, **kw) == envio.Decision(envio.PLANTILLA)
    assert envio.decidir_via(ventana_abierta=False, plantilla_utilizable=False, **kw).via is None
