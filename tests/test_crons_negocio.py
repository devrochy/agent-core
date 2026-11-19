"""Política por negocio de los crons (ADR-0201): funciones puras.

La lectura/escritura de `tenant_cron_configs` y el aislamiento RLS se prueban en
`tests/integration/test_crons_negocio.py` (requiere Postgres y Redis).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.admin import crons_negocio_service as svc
from core.shared.tiempo_negocio import ZONA_POR_DEFECTO, zona


def _local(anio: int, mes: int, dia: int, hora: int, minuto: int = 0) -> datetime:
    return datetime(anio, mes, dia, hora, minuto, tzinfo=zona("America/Bogota"))


def test_sin_ventana_siempre_corre():
    assert svc.en_ventana(_local(2026, 11, 16, 3), None) is True
    assert svc.en_ventana(_local(2026, 11, 16, 3), {}) is True


def test_dentro_y_fuera_de_la_ventana():
    ventana = {"desde": "08:00", "hasta": "18:00", "dias": list(range(7))}
    assert svc.en_ventana(_local(2026, 11, 16, 8), ventana) is True
    assert svc.en_ventana(_local(2026, 11, 16, 12), ventana) is True
    assert svc.en_ventana(_local(2026, 11, 16, 18), ventana) is True
    assert svc.en_ventana(_local(2026, 11, 16, 7, 59), ventana) is False
    assert svc.en_ventana(_local(2026, 11, 16, 18, 1), ventana) is False


def test_los_dias_se_respetan():
    # 2026-11-16 es lunes (weekday 0).
    ventana = {"desde": "08:00", "hasta": "18:00", "dias": [0, 2, 4]}
    assert svc.en_ventana(_local(2026, 11, 16, 9), ventana) is True
    assert svc.en_ventana(_local(2026, 11, 17, 9), ventana) is False


def test_ventana_que_cruza_la_medianoche():
    # Empieza el lunes a las 22:00 y termina el martes a las 02:00.
    ventana = {"desde": "22:00", "hasta": "02:00", "dias": [0]}
    assert svc.en_ventana(_local(2026, 11, 16, 23), ventana) is True
    assert svc.en_ventana(_local(2026, 11, 17, 1), ventana) is True
    assert svc.en_ventana(_local(2026, 11, 17, 3), ventana) is False
    # El martes a la 01:00 cuenta como lunes (día de inicio), no como martes.
    ventana_sin_lunes = {"desde": "22:00", "hasta": "02:00", "dias": [1]}
    assert svc.en_ventana(_local(2026, 11, 17, 1), ventana_sin_lunes) is False


def test_validar_ventana_normaliza():
    assert svc._validar_ventana({"desde": "08:00", "hasta": "18:00"}) == {
        "desde": "08:00",
        "hasta": "18:00",
        "dias": list(range(7)),
    }
    assert svc._validar_ventana({"desde": "08:00", "hasta": "18:00", "dias": [5, 1, 5]}) == {
        "desde": "08:00",
        "hasta": "18:00",
        "dias": [1, 5],
    }
    assert svc._validar_ventana(None) is None


@pytest.mark.parametrize(
    "ventana",
    [
        {"desde": "8", "hasta": "18:00"},
        {"desde": "08:00", "hasta": "25:00"},
        {"desde": "08:00", "hasta": "18:00", "dias": [7]},
        {"desde": "08:00", "hasta": "18:00", "dias": "lunes"},
    ],
)
def test_validar_ventana_rechaza_lo_invalido(ventana):
    with pytest.raises(svc.CronNegocioInvalido):
        svc._validar_ventana(ventana)


@pytest.mark.asyncio
async def test_un_cron_de_plataforma_siempre_corre_sin_leer_la_base():
    politica = await svc.politica(
        "00000000-0000-0000-0000-000000000001", "core.billing_renovacion"
    )
    assert politica.debe_correr is True
    assert politica.motivo is None


@pytest.mark.asyncio
async def test_un_cron_desconocido_corre_por_defecto():
    politica = await svc.politica(
        "00000000-0000-0000-0000-000000000001", "core.no_existe"
    )
    assert politica.debe_correr is True


def test_zona_invalida_cae_a_la_del_negocio():
    assert str(zona("No/Existe")) == ZONA_POR_DEFECTO
    assert str(zona(None)) == ZONA_POR_DEFECTO


@pytest.mark.asyncio
@pytest.mark.parametrize(("hora", "motivo"), [(22, None), (10, svc.OMITIDA_FUERA_DE_HORA)])
async def test_un_cron_con_hora_local_solo_corre_a_su_hora(monkeypatch, hora, motivo):
    """Sin configuración del negocio rige `hora_local_por_defecto` del catálogo (22 h en Bogotá)."""

    async def _sin_config(*_a, **_k):
        return None

    async def _bogota(*_a, **_k):
        return zona("America/Bogota")

    monkeypatch.setattr(svc, "cargar", _sin_config)
    monkeypatch.setattr(svc, "zona_del_negocio", _bogota)
    politica = await svc.politica(
        "00000000-0000-0000-0000-000000000001",
        "pack_subscriptions.generar_cargos",
        ahora=_local(2026, 11, 16, hora),
    )
    assert politica.motivo == motivo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("modulo", "job"),
    [
        ("pack_subscriptions.jobs", "_expirar_inscripciones"),
        ("pack_subscriptions.jobs", "_generar_cargos"),
        ("pack_subscriptions.jobs", "_marcar_vencidas"),
        ("pack_subscriptions.jobs", "_avisos_de_cobro"),
        ("pack_calendar.jobs", "_recordatorios_de_citas"),
    ],
)
async def test_el_job_le_pasa_su_reloj_a_la_politica(monkeypatch, modulo, job):
    """El job y la política miran el mismo reloj: el `now` del job llega como `ahora`."""
    import importlib
    import uuid

    jobs = importlib.import_module(modulo)
    recibido: list[datetime | None] = []

    async def _ids():
        return [uuid.UUID("00000000-0000-0000-0000-000000000001")]

    async def _debe_correr(_tid, _cron, *, ahora=None):
        recibido.append(ahora)
        return False

    monkeypatch.setattr(jobs, "_tenant_ids", _ids)
    monkeypatch.setattr(jobs, "debe_correr", _debe_correr)
    momento = _local(2026, 11, 16, 3)
    await getattr(jobs, job)(momento)
    assert recibido == [momento]
