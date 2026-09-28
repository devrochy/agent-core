"""Canal WhatsApp Web (no oficial, ADR-0074): estado del vínculo y QR.

Fuente única del panel SSR y la API. Todo delega en `core.waweb.connections`.
El QR se devuelve como data URI para que el panel lo pinte sin exponer el socket.
"""

from __future__ import annotations

import uuid
from typing import Any


def qr_data_uri(data: str) -> str:
    """QR (texto) → data URI PNG. Único lugar que genera la imagen."""
    import base64
    import io

    import qrcode

    image = qrcode.make(data)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


async def session_state(tenant_id: uuid.UUID | None) -> dict[str, Any]:
    """Estado del vínculo: status, risk_ok, jid y QR vigente (data URI o None)."""
    if tenant_id is None:
        return {"status": "", "risk_ok": False, "jid": None, "qr_image": None}
    from core.waweb import connections as waweb

    state = await waweb.session_view(tenant_id)
    return {
        "status": state.get("status", ""),
        "risk_ok": state.get("risk_ok", False),
        "jid": state.get("jid"),
        "qr_image": qr_data_uri(state["qr"]) if state.get("qr") else None,
    }


async def accept_risk(tenant_id: uuid.UUID | None, admin_id: uuid.UUID) -> str | None:
    if tenant_id is None:
        return "No hay negocio activo"
    from core.waweb import connections as waweb

    try:
        await waweb.accept_risk(tenant_id, admin_id)
    except Exception as exc:
        return str(exc)[:140]
    return None


async def reset_session(tenant_id: uuid.UUID | None) -> str | None:
    if tenant_id is None:
        return "No hay negocio activo"
    from core.waweb import connections as waweb

    try:
        await waweb.reset_session(tenant_id)
    except Exception as exc:
        return str(exc)[:140]
    return None
