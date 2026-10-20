"""PDF mínimo válido para los tests del pack de conocimiento (sin dependencias para generarlo)."""

from __future__ import annotations

import io


def pdf_minimo(paginas: list[str]) -> bytes:
    """Un PDF mínimo válido con una línea de texto por página (Helvetica)."""
    objetos: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids: list[str] = []
    for texto in paginas:
        n_pagina = len(objetos) + 1
        n_contenido = n_pagina + 1
        kids.append(f"{n_pagina} 0 R")
        linea = texto.replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({linea}) Tj ET".encode("latin-1")
        objetos.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n_contenido} 0 R "
            "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>".encode()
        )
        objetos.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objetos[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(paginas)} >>".encode()
    salida = io.BytesIO()
    salida.write(b"%PDF-1.4\n")
    offsets = []
    for i, cuerpo in enumerate(objetos, start=1):
        offsets.append(salida.tell())
        salida.write(f"{i} 0 obj\n".encode() + cuerpo + b"\nendobj\n")
    xref = salida.tell()
    salida.write(f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        salida.write(f"{off:010d} 00000 n \n".encode())
    salida.write(f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return salida.getvalue()
