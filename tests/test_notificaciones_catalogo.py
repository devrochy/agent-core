"""Catálogo de avisos de core (ADR-0167): cada constructor entrega una spec válida, apunta a
una sección real de permisos y no lleva PII en `data`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.admin.permissions import CORE_SECTIONS
from core.notifications import catalog

CID = uuid.uuid4()

CONSTRUCTORES = {
    "escalacion": lambda: catalog.escalacion(CID, "keyword", uuid.uuid4()),
    "escalacion_fallida": lambda: catalog.escalacion_fallida(CID),
    "conexion_pausada": lambda: catalog.conexion_pausada(CID, alcance="meta_account", motivo="Pausada", dia="2026-10-25"),
    "conexion_pausada_propia": lambda: catalog.conexion_pausada(CID, alcance="connection", motivo="Pausada", dia="2026-10-25"),
    "conexion_sin_acceso": lambda: catalog.conexion_sin_acceso(CID, vencido=True, dia="2026-10-25"),
    "conexion_token_por_vencer": lambda: catalog.conexion_token_por_vencer(CID, vence="2026-11-01", dia="2026-10-25"),
    "calidad_de_numero_roja": lambda: catalog.calidad_de_numero_roja(CID, dia="2026-10-25"),
    "mensaje_fallido": lambda: catalog.mensaje_fallido("1730000000-0"),
    "handoff_tomado": lambda: catalog.handoff_tomado(uuid.uuid4(), CID, uuid.uuid4(), "ana@negocio.com"),
    "handoff_resuelto": lambda: catalog.handoff_resuelto(
        uuid.uuid4(), CID, uuid.uuid4(), "ana@negocio.com", devuelta_al_agente=True
    ),
    "cliente_esperando": lambda: catalog.cliente_esperando(
        uuid.uuid4(), CID, desde=datetime(2026, 10, 26, tzinfo=UTC), umbral_minutos=15
    ),
    # HU-AOS-32: canary.
    "canary_revertido": lambda: catalog.canary_revertido(CID, uuid.uuid4(), 5, motivo="no_inferioridad", metricas=["escalamiento"]),
    "canary_revertido_dura": lambda: catalog.canary_revertido(CID, uuid.uuid4(), None, motivo="senal_dura"),
    "canary_listo": lambda: catalog.canary_listo(CID, uuid.uuid4(), 5),
    "canary_sin_muestra": lambda: catalog.canary_sin_muestra(CID, uuid.uuid4(), 5),
    "canary_costo": lambda: catalog.canary_costo(CID, uuid.uuid4(), 5),
    "canary_lentitud": lambda: catalog.canary_lentitud(CID, uuid.uuid4(), 5),
}


@pytest.mark.parametrize("nombre", sorted(CONSTRUCTORES))
def test_cada_aviso_del_catalogo_es_valido_y_apunta_a_una_seccion_real(nombre) -> None:
    spec = CONSTRUCTORES[nombre]()
    spec.validate()
    assert spec.section in CORE_SECTIONS
    assert spec.target is not None and spec.target["section"] == spec.section


def test_las_claves_de_deduplicacion_son_distintas_por_episodio() -> None:
    hoy = catalog.conexion_pausada(CID, alcance="connection", motivo="x", dia="2026-10-25")
    manana = catalog.conexion_pausada(CID, alcance="connection", motivo="x", dia="2026-10-26")
    assert hoy.dedupe_key != manana.dedupe_key
    assert catalog.escalacion(CID, "a", uuid.uuid4()).dedupe_key != catalog.escalacion(CID, "a", uuid.uuid4()).dedupe_key


def test_el_titulo_depende_del_alcance_de_la_pausa() -> None:
    cuenta = catalog.conexion_pausada(CID, alcance="meta_account", motivo="x", dia="d")
    propia = catalog.conexion_pausada(CID, alcance="connection", motivo="x", dia="d")
    assert cuenta.title == catalog.TITULO_CONEXION_WHATSAPP
    assert propia.title == catalog.TITULO_CONEXION_PROPIA


def test_un_episodio_de_espera_tiene_una_clave_y_otro_episodio_otra() -> None:
    esc = uuid.uuid4()
    uno = catalog.cliente_esperando(esc, CID, desde=datetime(2026, 10, 26, 10, 0, tzinfo=UTC), umbral_minutos=15)
    mismo = catalog.cliente_esperando(esc, CID, desde=datetime(2026, 10, 26, 10, 0, tzinfo=UTC), umbral_minutos=15)
    otro = catalog.cliente_esperando(esc, CID, desde=datetime(2026, 10, 26, 11, 0, tzinfo=UTC), umbral_minutos=15)
    assert uno.dedupe_key == mismo.dedupe_key != otro.dedupe_key


def test_el_aviso_de_handoff_lleva_al_actor_para_no_avisarle_a_el() -> None:
    admin = uuid.uuid4()
    spec = catalog.handoff_tomado(uuid.uuid4(), CID, admin, "ana@negocio.com")
    assert spec.data["actorAdminId"] == str(admin)
