"""Detección de puertos publicables.

Regresión: `_port_available` bindeaba a 127.0.0.1 con SO_REUSEADDR, así que un
servicio escuchando en una IP concreta se reportaba como "libre" y el
`docker compose up` fallaba después con "port is already allocated".
"""

from __future__ import annotations

import socket

import pytest

from cli.preflight import PortUnavailable, find_free_port, port_available, port_owner


def _bind(host: str) -> tuple[socket.socket, int]:
    """Ocupa un puerto efímero en `host` y devuelve el socket y el puerto."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def test_puerto_libre_se_reporta_libre() -> None:
    sock, port = _bind("0.0.0.0")
    sock.close()
    assert port_available(port) is True


def test_puerto_ocupado_en_todas_las_interfaces() -> None:
    sock, port = _bind("0.0.0.0")
    try:
        assert port_available(port) is False
    finally:
        sock.close()


def test_puerto_ocupado_solo_en_loopback_no_es_publicable() -> None:
    """Docker publica en 0.0.0.0: si algo escucha en loopback, no puede."""
    sock, port = _bind("127.0.0.1")
    try:
        assert port_available(port) is False
    finally:
        sock.close()


def test_find_free_port_excluye_los_ya_asignados() -> None:
    """Sin `reserved`, dos búsquedas seguidas devolvían el mismo puerto."""
    asignados: set[int] = set()
    primero = find_free_port(49200, "A", asignados, docker_ports={})
    asignados.add(primero)
    segundo = find_free_port(49200, "B", asignados, docker_ports={})
    assert primero != segundo


def test_find_free_port_evita_puertos_de_contenedores_parados() -> None:
    """Un contenedor parado no ocupa el socket, pero reclamará el puerto."""
    reclamados = {49300: "otro-proyecto-postgres-1", 49301: "otro-proyecto-redis-1"}
    elegido = find_free_port(49300, "Postgres", set(), docker_ports=reclamados)
    assert elegido == 49302


def test_find_free_port_sin_hueco_falla_explicito() -> None:
    ocupados = {port: "contenedor" for port in range(49400, 49400 + 50)}
    with pytest.raises(PortUnavailable):
        find_free_port(49400, "API", set(), docker_ports=ocupados)


def test_port_owner_identifica_el_contenedor() -> None:
    assert "mi-postgres" in port_owner(49500, docker_ports={49500: "mi-postgres"})


def test_port_owner_vacio_si_libre() -> None:
    sock, port = _bind("0.0.0.0")
    sock.close()
    assert port_owner(port, docker_ports={}) == ""
