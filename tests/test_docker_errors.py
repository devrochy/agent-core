"""Traducción de errores de Docker/PostgreSQL a causas accionables."""

from __future__ import annotations

from cli.preflight import explain_docker_error


def test_puerto_ocupado_se_explica() -> None:
    salida = explain_docker_error(
        'Error: driver failed programming external connectivity: '
        'Bind for 0.0.0.0:5432 failed: port is already allocated'
    )
    assert "puerto" in salida.lower()
    assert "5432" in salida


def test_credenciales_rechazadas_senalan_el_volumen() -> None:
    """El síntoma es de autenticación; la causa suele ser el volumen heredado."""
    salida = explain_docker_error(
        'FATAL:  password authentication failed for user "nuevo"', project="miproyecto"
    )
    assert "miproyecto_pgdata" in salida
    assert "down -v" in salida


def test_rol_inexistente_tambien_apunta_al_volumen() -> None:
    salida = explain_docker_error('FATAL:  role "nuevo" does not exist', project="demo")
    assert "demo_pgdata" in salida


def test_daemon_caido_se_explica() -> None:
    salida = explain_docker_error("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    assert "daemon" in salida.lower()


def test_error_desconocido_no_inventa_diagnostico() -> None:
    assert explain_docker_error("algo raro que nadie ha visto nunca") == ""


def test_error_vacio_no_rompe() -> None:
    assert explain_docker_error("") == ""
