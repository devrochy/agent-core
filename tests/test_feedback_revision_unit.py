"""HU-FB-04 · lógica pura de la revisión de reportes: topes, repeticiones idempotentes y plantillas del aviso
al autor (ADR-0213 §4 y §7). Sin base: los recorridos con la base están en
`tests/integration/test_feedback_revision.py`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from core.feedback.revision import (
    ErrorDeRevision,
    es_misma_aprobacion,
    es_mismo_rechazo,
    segundos_hasta_decidir,
    validar_monto,
)
from core.notifications import catalog

TOPES = {"max_reporte": 5000, "max_mes": 10000}


def _codigo(**kwargs) -> str:
    with pytest.raises(ErrorDeRevision) as exc:
        validar_monto(**{**TOPES, "regalado_en_el_mes": 0, **kwargs})
    assert exc.value.status == 422
    return exc.value.code


@pytest.mark.parametrize("creditos", [0, -1, True, 2.5])
def test_monto_invalido(creditos) -> None:
    assert _codigo(creditos=creditos) == "monto_invalido"


def test_tope_por_reporte() -> None:
    # Criterio de la HU: 8.000 > 5.000.
    assert _codigo(creditos=8000) == "tope_reporte_excedido"
    validar_monto(5000, regalado_en_el_mes=0, **TOPES)


def test_tope_mensual_con_lo_ya_regalado() -> None:
    # Criterio de la HU: 9.000 regalados en el ciclo + 2.000 > 10.000.
    with pytest.raises(ErrorDeRevision) as exc:
        validar_monto(2000, regalado_en_el_mes=9000, **TOPES)
    assert exc.value.code == "tope_mensual_excedido"
    assert exc.value.details == {"maximo": 10000, "regalado": 9000, "disponible": 1000}
    validar_monto(1000, regalado_en_el_mes=9000, **TOPES)  # justo en el tope


def test_misma_aprobacion_solo_con_mismo_revisor_y_monto() -> None:
    revisor = uuid.uuid4()
    reporte = {"estado": "aprobado", "revisado_por": revisor, "creditos_otorgados": 3000}
    assert es_misma_aprobacion(reporte, revisor, 3000)
    assert not es_misma_aprobacion(reporte, revisor, 2000)
    assert not es_misma_aprobacion(reporte, uuid.uuid4(), 3000)
    assert not es_misma_aprobacion({**reporte, "estado": "rechazado"}, revisor, 3000)


def test_mismo_rechazo() -> None:
    revisor = uuid.uuid4()
    reporte = {"estado": "rechazado", "revisado_por": revisor, "motivo_codigo": "otro"}
    assert es_mismo_rechazo(reporte, revisor, "otro")
    assert not es_mismo_rechazo(reporte, revisor, "duplicado")
    assert not es_mismo_rechazo({**reporte, "estado": "aprobado"}, revisor, "otro")


def test_segundos_hasta_decidir() -> None:
    creado = datetime(2026, 11, 27, 10, tzinfo=UTC)
    assert segundos_hasta_decidir({"created_at": creado, "decidido_en": creado + timedelta(hours=2)}) == 7200
    assert segundos_hasta_decidir({"created_at": creado, "decidido_en": None}) == 0.0


def test_plantillas_validan_y_van_dirigidas_al_autor() -> None:
    rid, autor = uuid.uuid4(), uuid.uuid4()
    aprobado = catalog.feedback_aprobado(rid, autor, 3000)
    rechazado = catalog.feedback_rechazado(rid, autor, "duplicado")
    for spec in (aprobado, rechazado):
        spec.validate()  # la entidad `feedback_reporte` existe en ENTIDADES
        assert spec.category == "sistema" and spec.section is None
        assert spec.audience_admin_id == autor
        assert spec.target == {"entity": "feedback_reporte", "id": str(rid), "section": None}
    assert "3.000" in aprobado.body
    assert aprobado.dedupe_key == f"feedback_aprobado:{rid}"
    # El cliente nunca ve a qué reporte duplica: ni en el texto ni en `data`.
    assert "duplicado_de" not in rechazado.data and str(rid) == rechazado.data["reportId"]


def test_motivo_desconocido_cae_en_otro() -> None:
    spec = catalog.feedback_rechazado(uuid.uuid4(), uuid.uuid4(), "raro")
    assert "no aplica para el programa" in spec.body
