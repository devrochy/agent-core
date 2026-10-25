"""Horario legal de los avisos de cobro (HU-SUB-18): reglas puras, sin BD."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pack_subscriptions.config import SubscriptionConfig
from pack_subscriptions.horario_legal import (
    VENTANA_LEGAL_CO,
    dentro_de_lo_legal,
    en_horario_de_cobro,
    es_festivo,
    festivos_co,
)
from pydantic import ValidationError

BOGOTA = "America/Bogota"


def _utc(y, m, d, h, mi=0):  # type: ignore[no-untyped-def]
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_festivos_de_colombia_2026_coinciden_con_el_calendario_oficial() -> None:
    esperados = {
        date(2026, 1, 1), date(2026, 1, 12), date(2026, 3, 23), date(2026, 4, 2), date(2026, 4, 3),
        date(2026, 5, 1), date(2026, 5, 18), date(2026, 6, 8), date(2026, 6, 15), date(2026, 6, 29),
        date(2026, 7, 20), date(2026, 8, 7), date(2026, 8, 17), date(2026, 10, 12), date(2026, 11, 2),
        date(2026, 11, 16), date(2026, 12, 8), date(2026, 12, 25),
    }
    assert festivos_co(2026) == esperados


def test_festivos_trasladables_caen_en_lunes_y_los_fijos_no_se_mueven() -> None:
    # 2027: San Pedro (29 de junio, martes) pasa al lunes 5 de julio; el 20 de julio es martes y se queda.
    assert date(2027, 7, 5) in festivos_co(2027)
    assert date(2027, 6, 29) not in festivos_co(2027)
    assert date(2027, 7, 20) in festivos_co(2027)
    assert len(festivos_co(2027)) == 18


def test_otro_pais_no_tiene_festivos_conocidos() -> None:
    assert not es_festivo(date(2026, 12, 25), "MX")


@pytest.mark.parametrize(
    ("ahora", "esperado"),
    [
        (_utc(2026, 10, 26, 12), True),  # lunes 26 de oct (no festivo), 07:00 Bogotá: abre
        (_utc(2026, 10, 27, 12), True),  # martes 07:00 Bogotá: abre
        (_utc(2026, 10, 27, 11, 59), False),  # martes 06:59 Bogotá
        (_utc(2026, 10, 28, 0), False),  # martes 19:00 Bogotá: cierra (el `to` se excluye)
        (_utc(2026, 10, 27, 23, 59), True),  # martes 18:59 Bogotá
        (_utc(2026, 10, 31, 13), True),  # sábado 08:00 Bogotá
        (_utc(2026, 10, 31, 20), False),  # sábado 15:00 Bogotá
        (_utc(2026, 11, 1, 15), False),  # domingo
        (_utc(2026, 11, 2, 15), False),  # lunes festivo (Todos los santos trasladado)
    ],
)
def test_ventana_legal_en_bogota(ahora: datetime, esperado: bool) -> None:
    assert en_horario_de_cobro(ahora, BOGOTA) is esperado


def test_la_ventana_se_evalua_en_la_zona_del_negocio() -> None:
    # Martes 27 de oct, 12:30 UTC: 07:30 en Bogotá (abierto), 06:30 en Ciudad de México
    # (cerrado, UTC-6) y 13:30 en Madrid (abierto).
    ahora = _utc(2026, 10, 27, 12, 30)
    assert en_horario_de_cobro(ahora, BOGOTA)
    assert not en_horario_de_cobro(ahora, "America/Mexico_City")
    assert en_horario_de_cobro(ahora, "Europe/Madrid")


def test_zona_invalida_cae_a_bogota_y_ventana_vacia_cierra() -> None:
    ahora = _utc(2026, 10, 27, 15)
    assert en_horario_de_cobro(ahora, "No/Existe")
    assert not en_horario_de_cobro(ahora, BOGOTA, ventana=[])


def test_el_negocio_puede_achicar_la_ventana_pero_no_ampliarla() -> None:
    assert dentro_de_lo_legal([{"day": 0, "from": "09:00", "to": "17:00"}])
    assert not dentro_de_lo_legal([{"day": 0, "from": "06:00", "to": "17:00"}])
    assert not dentro_de_lo_legal([{"day": 5, "from": "08:00", "to": "16:00"}])
    assert not dentro_de_lo_legal([{"day": 6, "from": "09:00", "to": "12:00"}])


def test_config_por_defecto_es_la_ventana_legal_y_rechaza_ampliarla() -> None:
    assert SubscriptionConfig().ventana_cobro == [dict(r) for r in VENTANA_LEGAL_CO]
    with pytest.raises(ValidationError):
        SubscriptionConfig(ventana_cobro=[{"day": 6, "from": "09:00", "to": "12:00"}])
    with pytest.raises(ValidationError):
        SubscriptionConfig(ventana_cobro=[{"day": 0, "from": "9:00", "to": "17:00"}])
    angosta = SubscriptionConfig(ventana_cobro=[{"day": 1, "from": "09:00", "to": "12:00"}])
    assert angosta.ventana_cobro == [{"day": 1, "from": "09:00", "to": "12:00"}]


def test_proximo_inicio_dentro_de_la_ventana_es_ahora() -> None:
    from pack_subscriptions.horario_legal import proximo_inicio_de_cobro

    ahora = _utc(2026, 10, 27, 15)  # martes 10:00 Bogotá
    assert proximo_inicio_de_cobro(ahora, BOGOTA) == ahora


def test_proximo_inicio_de_noche_es_las_siete_del_dia_siguiente() -> None:
    from pack_subscriptions.horario_legal import proximo_inicio_de_cobro

    # martes 27 de oct, 22:15 Bogotá (cron de cargos) -> miércoles 28, 07:00 Bogotá.
    assert proximo_inicio_de_cobro(_utc(2026, 10, 28, 3, 15), BOGOTA) == _utc(2026, 10, 28, 12)


def test_proximo_inicio_salta_domingo_y_lunes_festivo() -> None:
    from pack_subscriptions.horario_legal import proximo_inicio_de_cobro

    # sábado 31 de oct, 16:00 Bogotá -> domingo no, lunes 2 de nov festivo no -> martes 3, 07:00.
    assert proximo_inicio_de_cobro(_utc(2026, 10, 31, 21), BOGOTA) == _utc(2026, 11, 3, 12)


def test_proximo_inicio_sin_ventana_es_none() -> None:
    from pack_subscriptions.horario_legal import proximo_inicio_de_cobro

    assert proximo_inicio_de_cobro(_utc(2026, 10, 27, 15), BOGOTA, ventana=[]) is None
