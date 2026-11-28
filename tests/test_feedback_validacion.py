"""HU-FB-03 · validación del envío de reportes, sin base (ADR-0213 §5 y §9).

Todo se valida antes de guardar: cantidad, tamaño por archivo y total, firma y archivos repetidos. La
`Idempotency-Key` es obligatoria. El detalle con la base, la RLS y el almacenamiento está en
`tests/integration/test_hu_fb_03_reportes.py`.
"""

from __future__ import annotations

import pytest

from core.feedback import catalogo
from core.feedback.reportes import AdjuntoEntrada, ErrorFeedback, validar_adjuntos, validar_clave

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _codigo(adjuntos: list[AdjuntoEntrada]) -> tuple[int, str]:
    with pytest.raises(ErrorFeedback) as exc:
        validar_adjuntos(adjuntos)
    return exc.value.status, exc.value.code


def test_imagenes_y_pdf_validos_conservan_el_orden() -> None:
    validos = validar_adjuntos(
        [AdjuntoEntrada("a.png", "image/png", PNG), AdjuntoEntrada("b.pdf", "application/pdf", b"%PDF-1.7\n")]
    )
    assert [(v.orden, v.mimetype) for v in validos] == [(0, "image/png"), (1, "application/pdf")]
    assert all(len(v.sha256) == 64 for v in validos)


def test_rechaza_firma_falsa_tipos_no_admitidos_y_vacios() -> None:
    assert _codigo([AdjuntoEntrada("x.png", "image/png", b"MZ\x90\x00")]) == (422, "adjunto_invalido")
    assert _codigo([AdjuntoEntrada("x.txt", "text/plain", b"hola")]) == (422, "adjunto_invalido")
    assert _codigo([AdjuntoEntrada("x.png", "image/png", b"")]) == (422, "adjunto_invalido")


def test_limites_de_cantidad_tamano_y_repetidos() -> None:
    seis = [AdjuntoEntrada(f"{i}.png", "image/png", PNG + bytes([i])) for i in range(6)]
    assert _codigo(seis) == (422, "demasiados_adjuntos")
    grande = PNG + b"\x00" * catalogo.MAX_BYTES_POR_ARCHIVO
    assert _codigo([AdjuntoEntrada("g.png", "image/png", grande)]) == (413, "adjunto_muy_grande")
    siete = 7 * 1024 * 1024
    cuatro = [AdjuntoEntrada(f"{i}.png", "image/png", PNG + bytes([i]) * siete) for i in range(4)]
    assert _codigo(cuatro) == (413, "adjuntos_muy_grandes")
    dos_iguales = [AdjuntoEntrada("a.png", "image/png", PNG), AdjuntoEntrada("b.png", "image/png", PNG)]
    assert _codigo(dos_iguales) == (422, "adjunto_repetido")


@pytest.mark.parametrize(
    ("clave", "codigo"),
    [(None, "idempotency_key_requerida"), ("  ", "idempotency_key_requerida"), ("corta", "idempotency_key_invalida"),
     ("con espacios 123", "idempotency_key_invalida"), ("x" * 129, "idempotency_key_invalida")],
)
def test_idempotency_key_obligatoria_y_acotada(clave: str | None, codigo: str) -> None:
    with pytest.raises(ErrorFeedback) as exc:
        validar_clave(clave)
    assert (exc.value.status, exc.value.code) == (400, codigo)


def test_idempotency_key_valida() -> None:
    assert validar_clave(" 3f2b6c1e-0000-4000-8000-000000000000 ") == "3f2b6c1e-0000-4000-8000-000000000000"
