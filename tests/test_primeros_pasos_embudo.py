"""Embudo de activación (HU-SH-06, ADR-0124): agregación de los hitos, sin base.

El registro (idempotencia, RLS, barrido) lo prueba `tests/integration/test_primeros_pasos.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from core.primeros_pasos.calculo import Evaluacion
from core.primeros_pasos.hitos import Hito, armar_embudo, candidatos, etiqueta
from core.primeros_pasos.reglas import paso_catalogo, paso_datos_negocio, paso_probar_asistente

AHORA = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


def _hito(paso: str, segundos: int) -> Hito:
    return Hito(paso=paso, completado_en=AHORA, segundos_desde_alta=segundos, fuente="dato")


def test_el_embudo_cuenta_porcentaje_mediana_y_las_primeras_24_horas() -> None:
    negocios = [
        {"invitacion_aceptada": _hito("invitacion_aceptada", 60), "whatsapp": _hito("whatsapp", 3600)},
        {"invitacion_aceptada": _hito("invitacion_aceptada", 120), "whatsapp": _hito("whatsapp", 2 * 86400)},
        {"invitacion_aceptada": _hito("invitacion_aceptada", 30)},
        {},  # se dio de alta y no hizo nada
    ]
    embudo = armar_embudo(AHORA - timedelta(days=30), AHORA, negocios)

    assert embudo.altas == 4
    filas = {f.paso: f for f in embudo.filas}
    assert (filas["invitacion_aceptada"].negocios, filas["invitacion_aceptada"].porcentaje) == (3, 75.0)
    assert filas["invitacion_aceptada"].mediana_segundos == 60
    assert (filas["whatsapp"].negocios, filas["whatsapp"].en_24_horas) == (2, 1)
    assert filas["whatsapp"].mediana_segundos == (3600 + 2 * 86400) // 2


def test_el_embudo_sigue_el_recorrido_de_la_invitacion_al_primer_mensaje() -> None:
    negocios = [
        {
            paso: _hito(paso, 10)
            for paso in ("primer_mensaje", "configurar_pack_payments", "catalogo", "invitacion_aceptada", "whatsapp")
        }
    ]
    orden = [f.paso for f in armar_embudo(AHORA, AHORA, negocios).filas]
    assert orden == ["invitacion_aceptada", "whatsapp", "catalogo", "primer_mensaje", "configurar_pack_payments"]


def test_sin_altas_el_embudo_esta_vacio() -> None:
    embudo = armar_embudo(AHORA, AHORA, [])
    assert (embudo.altas, embudo.filas) == (0, [])


def test_candidatos_son_los_pasos_completos_y_los_bordes_que_ocurrieron() -> None:
    evaluacion = Evaluacion(
        tenant_id=uuid.uuid4(),
        alta_en=AHORA,
        origen="manual",
        pasos=[paso_datos_negocio("Tienda", AHORA), paso_probar_asistente(None), paso_catalogo([])],
        hitos_del_embudo={"primer_mensaje": AHORA},
    )
    assert candidatos(evaluacion) == {"datos_negocio": AHORA, "primer_mensaje": AHORA}


def test_etiquetas_legibles_del_reporte() -> None:
    assert etiqueta("whatsapp") == "WhatsApp conectado y verificado"
    assert etiqueta("configurar_pack_payments") == "Configuró pack_payments"
    assert etiqueta("otro_paso") == "otro_paso"
