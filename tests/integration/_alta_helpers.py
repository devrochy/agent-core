"""Ayudas de los tests del alta automática (HU-SH-03/04/07).

Desde HU-SH-07 (ADR-0125) un alta solo sigue sola si el pedido tiene la aceptación de
términos, privacidad y DPA registrada (`pedidos_de_alta`). Los tests que arman el pedido a
mano (sin la tool `crear_pedido`) la registran con `aceptar_terminos`, igual que la tool.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.provisioning import legal


async def publicar_documentos(vendedor: uuid.UUID, *, version: str = "v1") -> dict[str, legal.Documento]:
    """Publica términos, privacidad y DPA del vendedor si todavía no tiene versión vigente."""
    vigentes = await legal.documentos_vigentes(vendedor)
    for tipo in legal.faltantes(vigentes):
        await legal.publicar(
            vendedor, tipo=tipo, version=version, url=f"https://solvershub.test/{tipo}/{version}", por="test"
        )
    return await legal.documentos_vigentes(vendedor)


async def aceptar_terminos(
    vendedor: uuid.UUID,
    referencia: str,
    *,
    customer_id: uuid.UUID | None = None,
    gratis: bool = False,
    telefono: str | None = None,
    documento: str | None = None,
    senales: list[str] | None = None,
    con_aceptacion: bool = True,
) -> None:
    """Registra el pedido de alta de `referencia` como lo hace `crear_pedido`."""
    from datetime import UTC, datetime

    from core.db.session import get_session
    from core.provisioning.antiabuso import (
        PedidoPreparado,
        huella,
        normalizar_documento,
        normalizar_telefono,
        registrar_pedido_de_alta,
    )
    from core.shared.tenant import tenant_scope

    aceptacion: dict[str, Any] | None = None
    if con_aceptacion:
        vigentes = await publicar_documentos(vendedor)
        aceptacion = {
            "documentos": [vigentes[t].para_aceptar() for t in legal.TIPOS],
            "version_legal": legal.version_legal(vigentes),
            "mensaje_id": None,
        }
    huellas = []
    if telefono and normalizar_telefono(telefono):
        huellas.append(huella("tel", normalizar_telefono(telefono) or ""))
    if documento and normalizar_documento(documento):
        huellas.append(huella("doc", normalizar_documento(documento) or ""))
    pedido = PedidoPreparado(
        vendedor=vendedor,
        customer_id=customer_id,
        conversation_id=None,
        canal="whatsapp",
        gratis=gratis,
        huellas=huellas,
        senales=senales or [],
        aceptacion=aceptacion,
        aceptado_en=datetime.now(UTC) if aceptacion else None,
    )
    async with tenant_scope(vendedor):
        async with get_session() as session:
            registrar_pedido_de_alta(session, pedido, referencia=referencia)
