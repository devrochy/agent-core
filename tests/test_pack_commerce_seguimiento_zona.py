"""HU-CRM-21 · La zona horaria del negocio decide qué es «hoy» (sin base de datos).

La fecha del seguimiento se guarda en UTC; «hoy» y «mañana» se cuentan en la zona del negocio.
El defecto clásico es comparar contra el día UTC: un seguimiento a las 23:30 de Bogotá ya es
«mañana» en UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pack_commerce import seguimiento as seg

BOGOTA = ZoneInfo("America/Bogota")  # UTC-5 todo el año


def _utc(*partes: int) -> datetime:
    return datetime(*partes, tzinfo=UTC)


def test_la_fecha_sin_zona_es_hora_del_negocio() -> None:
    assert seg.parsear_fecha("2026-11-03T09:00", BOGOTA) == _utc(2026, 11, 3, 14, 0)


def test_la_fecha_con_zona_se_respeta() -> None:
    assert seg.parsear_fecha("2026-11-03T09:00:00-08:00", BOGOTA) == _utc(2026, 11, 3, 17, 0)
    assert seg.parsear_fecha("2026-11-03T09:00:00Z", BOGOTA) == _utc(2026, 11, 3, 9, 0)


@pytest.mark.parametrize("texto", ["mañana", "2026-13-40T99:00", ""])
def test_una_fecha_ilegible_se_rechaza(texto: str) -> None:
    with pytest.raises(ValueError, match="fecha"):
        seg.parsear_fecha(texto, BOGOTA)


def test_se_muestra_en_la_zona_del_negocio() -> None:
    assert seg.texto_local(_utc(2026, 11, 3, 14, 0), BOGOTA) == "2026-11-03T09:00"
    assert seg.texto_local(None, BOGOTA) == ""


def test_las_23_30_de_bogota_no_caen_en_hoy_de_utc() -> None:
    # Son las 22:00 del lunes 2 en Bogotá (03:00 del martes 3 en UTC).
    lim = seg.limites(BOGOTA, _utc(2026, 11, 3, 3, 0))
    tarde = seg.parsear_fecha("2026-11-02T23:30", BOGOTA)  # 04:30 UTC del martes 3
    assert tarde.date().isoformat() == "2026-11-03"  # en UTC ya es otro día
    assert seg.estado(tarde, lim) == "hoy"  # pero para el negocio sigue siendo hoy
    # La medianoche local de Bogotá es el corte: 00:00 del martes ya es «próximos».
    assert seg.estado(seg.parsear_fecha("2026-11-03T00:00", BOGOTA), lim) == "proximos_7_dias"


def test_las_opciones_cubren_todos_los_casos_una_sola_vez() -> None:
    lim = seg.limites(BOGOTA, _utc(2026, 11, 3, 15, 0))  # martes 3, 10:00 en Bogotá
    casos = {
        None: None,
        seg.parsear_fecha("2026-11-03T09:59", BOGOTA): "vencidos",
        seg.parsear_fecha("2026-11-03T10:01", BOGOTA): "hoy",
        seg.parsear_fecha("2026-11-04T00:00", BOGOTA): "proximos_7_dias",
        seg.parsear_fecha("2026-11-10T23:59", BOGOTA): "proximos_7_dias",  # hoy + 7 días
        seg.parsear_fecha("2026-11-11T00:00", BOGOTA): "mas_adelante",
    }
    for fecha, esperado in casos.items():
        assert seg.estado(fecha, lim) == esperado, fecha


def test_el_horario_de_verano_no_corre_el_dia() -> None:
    # En Nueva York el 2026-11-01 el día dura 25 horas: el corte sigue siendo la medianoche local.
    ny = ZoneInfo("America/New_York")
    lim = seg.limites(ny, _utc(2026, 11, 1, 12, 0))  # 07:00 EST (ya cambió la hora)
    assert lim.fin_de_hoy == _utc(2026, 11, 2, 5, 0)  # 00:00 EST del día 2 = 05:00 UTC


@pytest.mark.parametrize("texto", ["x" * 201])
def test_la_nota_tiene_tope(texto: str) -> None:
    with pytest.raises(ValueError, match="200"):
        seg._nota(texto)
    assert seg._nota("  ") is None
    assert seg._nota("x" * 200) == "x" * 200
