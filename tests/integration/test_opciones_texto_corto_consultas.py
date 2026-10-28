"""SPEC-40 · pista C2: el costo de un texto corto en un canal sin botones (seguimiento del arquitecto sobre #624).

Antes, todo texto corto de web/QR/Messenger/Instagram abría una sesión de BD y leía `tenants.settings` para ver la
bandera `opciones_directas.habilitada`, aunque el negocio nunca usara opciones. Ahora la bandera sale de la MISMA fila de
`tenants` que `handle_inbound` ya lee para el gate «negocio operativo» (sin caché: nada que invalidar ni que cruce
negocios), así que:

- bandera apagada → ni una consulta extra por mensaje;
- bandera encendida y el último saliente no es un menú → UNA consulta (el último saliente), sin `cargar_envio`;
- bandera encendida y menú vigente → dos (último saliente y envío).

Todo va por `handle_inbound` (la ruta real del consumer): las llamadas directas a `_handle_inbound_scoped` de los demás
tests no pasan por el gate y caen en la lectura de siempre. Requiere el stack local y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import os
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event
from test_opciones_envio import (  # noqa: F401  (fixtures y ayudas del envío)
    cola,
    negocios,
)
from test_opciones_menu_texto import (
    WEB,
    _abrir,
    _activaciones,
    _entrada,
    _menu,
    crear_menu_de_texto,
)
from test_opciones_toque_entorno import (  # noqa: F401
    _bandera,
    _binding,
    _inmediato,
    herramienta,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

LARGO = "x" * 140  # más largo que cualquier elección posible: ni se mira la bandera


# ---------------------------------------------------------------- ayudas
async def _por_el_gate(tid: uuid.UUID, cliente: str, contenido: str) -> None:
    """Un mensaje por la ruta real del consumer (gate de negocio operativo incluido)."""
    from core.orchestrator import consumer

    assert await consumer.handle_inbound(_entrada(tid, cliente, contenido, WEB)) == consumer.TURN_PROCESSED


@contextmanager
def _sentencias() -> Iterator[list[str]]:
    """Todas las sentencias SQL que ejecuta el proceso (el engine compartido), hasta salir."""
    from core.db.session import get_engine

    vistas: list[str] = []

    def _anota(_conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        vistas.append(statement)

    motor = get_engine().sync_engine
    event.listen(motor, "before_cursor_execute", _anota)
    try:
        yield vistas
    finally:
        event.remove(motor, "before_cursor_execute", _anota)


class _Espias:
    """Cuenta cuántas veces se consulta cada paso de la respuesta de texto (y la lectura de la bandera en BD)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.kernel.opciones import activaciones, bandera

        self.ultimo_saliente = 0
        self.cargar_envio = 0
        self.bandera_en_bd = 0
        real_ultimo, real_cargar, real_bandera = activaciones.envio_del_ultimo_saliente, activaciones.cargar_envio, bandera.habilitada

        async def ultimo(*a: Any, **k: Any) -> Any:
            self.ultimo_saliente += 1
            return await real_ultimo(*a, **k)

        async def cargar(*a: Any, **k: Any) -> Any:
            self.cargar_envio += 1
            return await real_cargar(*a, **k)

        async def en_bd(*a: Any, **k: Any) -> Any:
            self.bandera_en_bd += 1
            return await real_bandera(*a, **k)

        monkeypatch.setattr(activaciones, "envio_del_ultimo_saliente", ultimo)
        monkeypatch.setattr(activaciones, "cargar_envio", cargar)
        monkeypatch.setattr(bandera, "habilitada", en_bd)


def _de_mas(medido: list[str], base: list[str]) -> list[str]:
    """Las sentencias que `medido` ejecutó y `base` no (por multiconjunto: el texto largo hace alguna escritura propia
    —el estado de la conversación—, así que comparar totales sería frágil; lo que importa es qué añade el texto corto)."""
    return list((Counter(medido) - Counter(base)).elements())


# ---------------------------------------------------------------- bandera apagada: cero consultas extra
async def test_con_la_bandera_apagada_un_texto_corto_no_cuesta_ni_una_consulta_extra(  # type: ignore[no-untyped-def]
    negocios, cola, recorded, monkeypatch  # noqa: F811
) -> None:
    _, _, apagado = negocios
    _inmediato(monkeypatch)
    espias = _Espias(monkeypatch)
    cliente = f"c-{uuid.uuid4().hex[:8]}"
    await _por_el_gate(apagado.tid, cliente, "hola")  # abre la conversación: lo que viene es solo el mensaje medido
    recorded.clear()

    with _sentencias() as corto:
        await _por_el_gate(apagado.tid, cliente, "ok")  # elegible como respuesta de menú (corto, sin adjunto)
    with _sentencias() as largo:
        await _por_el_gate(apagado.tid, cliente, LARGO)  # no elegible: NUNCA toca la bandera (la línea base)

    assert recorded == ["ok", LARGO], "con la bandera apagada el texto sigue al agente como siempre"
    assert _de_mas(corto, largo) == [], "el texto corto no ejecuta ni una sentencia más que el que no puede ser una elección"
    assert (espias.bandera_en_bd, espias.ultimo_saliente, espias.cargar_envio) == (0, 0, 0)


# ---------------------------------------------------------------- bandera encendida: ruta corta
async def test_encendida_y_el_ultimo_saliente_no_es_un_menu_cuesta_una_consulta_y_sigue_al_agente(  # type: ignore[no-untyped-def]
    negocios, cola, recorded, monkeypatch  # noqa: F811
) -> None:
    a, _, _ = negocios
    _inmediato(monkeypatch)
    espias = _Espias(monkeypatch)
    cliente = f"c-{uuid.uuid4().hex[:8]}"
    await _por_el_gate(a.tid, cliente, "hola")
    recorded.clear()
    espias.ultimo_saliente = 0

    with _sentencias() as corto:
        await _por_el_gate(a.tid, cliente, "ok")
    with _sentencias() as largo:
        await _por_el_gate(a.tid, cliente, LARGO)

    assert recorded == ["ok", LARGO]
    assert espias.ultimo_saliente == 1, "una sola consulta: el último saliente de la conversación"
    assert espias.cargar_envio == 0, "no es un menú: ni se carga el envío"
    assert espias.bandera_en_bd == 0, "la bandera salió de la fila del gate, no de otra lectura"
    extra = _de_mas(corto, largo)
    assert len(extra) == 2, "una sesión corta: el `set_config` del tenant y UNA consulta"
    consulta = next(x for x in extra if not x.startswith("select set_config"))
    assert "FROM messages" in consulta and "direction = 'outbound'" in consulta, "el último saliente de la conversación"


async def test_la_bandera_sale_de_la_fila_del_gate_no_de_otra_lectura(  # type: ignore[no-untyped-def]
    negocios, cola, recorded, herramienta, monkeypatch  # noqa: F811
) -> None:
    """Journey completo por `handle_inbound` con la lectura de la bandera en BD rota: si el menú se resuelve, la
    decisión salió de la fila que el gate ya había leído."""
    from core.kernel.opciones import bandera

    a, _, _ = negocios
    _inmediato(monkeypatch)
    await _binding(a.tid)
    conv, cliente = await _abrir(a.tid, WEB)
    r = await _menu(a.tid, conv)

    async def no_deberia_leerse(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("el consumer no debe volver a leer la bandera de la base")

    espias = _Espias(monkeypatch)
    monkeypatch.setattr(bandera, "habilitada", no_deberia_leerse)  # pisa al espía: leerla de la base explota
    recorded.clear()

    await _por_el_gate(a.tid, cliente, "1")

    assert recorded == [] and [x for x, _ in herramienta.llamadas] == [{"pedido": "A-1"}]
    assert await _activaciones(a.tid, r.send_id) == [("o1", "done")]
    # Menú vigente: el último saliente (1) y el envío; el resolver vuelve a cargar el envío para revalidarlo todo.
    assert espias.ultimo_saliente == 1 and espias.cargar_envio >= 1


# ---------------------------------------------------------------- aislamiento entre negocios
async def test_la_bandera_de_un_negocio_no_se_hereda_al_siguiente_mensaje_de_otro(  # type: ignore[no-untyped-def]
    negocios, cola, recorded, herramienta, monkeypatch  # noqa: F811
) -> None:
    """B (apagado, con un menú de texto como último saliente) primero, luego A (encendido), luego B otra vez, en el
    mismo proceso y la misma tarea: B nunca ejecuta ni consulta nada de opciones; A sí."""
    a, _, b = negocios
    _inmediato(monkeypatch)
    await _binding(a.tid)
    await _binding(b.tid)
    conv_a, cliente_a = await _abrir(a.tid, WEB)
    conv_b, cliente_b = await _abrir(b.tid, WEB)
    envio_a = await _menu(a.tid, conv_a)
    envio_b = await crear_menu_de_texto(b.tid, conv_b)
    espias = _Espias(monkeypatch)
    recorded.clear()

    await _por_el_gate(b.tid, cliente_b, "1")
    assert (espias.bandera_en_bd, espias.ultimo_saliente, espias.cargar_envio) == (0, 0, 0)
    assert recorded == ["1"] and herramienta.llamadas == [], "B está apagado: el «1» llega al agente"

    await _por_el_gate(a.tid, cliente_a, "1")
    assert [x for x, _ in herramienta.llamadas] == [{"pedido": "A-1"}], "A está encendido: el «1» ejecuta"
    assert await _activaciones(a.tid, envio_a.send_id) == [("o1", "done")]

    antes = (espias.ultimo_saliente, espias.cargar_envio)
    recorded.clear()
    await _por_el_gate(b.tid, cliente_b, "1")
    assert (espias.ultimo_saliente, espias.cargar_envio) == antes, "B no heredó la bandera encendida de A"
    assert recorded == ["1"] and len(herramienta.llamadas) == 1
    assert await _activaciones(b.tid, envio_b) == []


# ---------------------------------------------------------------- invalidación
async def test_apagar_la_bandera_rige_desde_el_siguiente_mensaje(  # type: ignore[no-untyped-def]
    negocios, cola, recorded, herramienta, monkeypatch  # noqa: F811
) -> None:
    """No hay caché: encendida el «1» ejecuta; apagada con `fijar(False)`, el siguiente «1» ya llega al agente; y al
    encenderla de nuevo vuelve a ejecutar."""
    a, _, _ = negocios
    _inmediato(monkeypatch)
    await _binding(a.tid)
    conv, cliente = await _abrir(a.tid, WEB)
    try:
        await _menu(a.tid, conv)
        recorded.clear()
        await _por_el_gate(a.tid, cliente, "1")
        assert len(herramienta.llamadas) == 1 and recorded == []

        await _menu(a.tid, conv)
        await _bandera(a.tid, False)
        await _por_el_gate(a.tid, cliente, "1")
        assert len(herramienta.llamadas) == 1 and recorded == ["1"], "apagada: el «1» sigue al agente"

        await _bandera(a.tid, True)
        await _menu(a.tid, conv)  # el agente ya contestó el «1» de arriba: hace falta un menú nuevo como último saliente
        await _por_el_gate(a.tid, cliente, "1")
        assert len(herramienta.llamadas) == 2, "encendida otra vez: ejecuta"
    finally:
        await _bandera(a.tid, True)
