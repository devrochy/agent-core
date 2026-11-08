"""Content API de Twilio: plantillas por id de contenido (`HX…`) y su aprobación en WhatsApp.

Fuentes (2026-11-07): https://www.twilio.com/docs/content/content-api-resources y
https://www.twilio.com/docs/content/content-types-overview. Una plantilla ya enviada a
aprobación no se edita: se crea otra. El estado de aprobación llega en minúsculas o
capitalizado según la página; se normaliza a minúsculas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agentcore_channel_twilio.client import CONTENT_BASE, TwilioClient
from core.shared.exceptions import ChannelError

CONTENT_URL = f"{CONTENT_BASE}/Content"
_CONTENT_SID = re.compile(r"^HX[0-9a-fA-F]{32}$")
_APPROVAL_NAME = re.compile(r"^[a-z0-9_]{1,512}$")
_PLACEHOLDER = re.compile(r"\{\{\s*([^}]*?)\s*\}\}")

APPROVAL_CATEGORIES = frozenset({"UTILITY", "MARKETING", "AUTHENTICATION"})
APPROVAL_STATUSES = frozenset(
    {"unsubmitted", "received", "pending", "approved", "rejected", "paused", "disabled"}
)


def check_content_sid(sid: str) -> str:
    if not _CONTENT_SID.match(sid or ""):
        raise ChannelError("Content SID de Twilio inválido")
    return sid


def validate_variables(body: str, variables: dict[str, str]) -> list[str]:
    """Problemas de las variables de una plantilla, vacío si está bien: los marcadores son
    `{{1}}`, `{{2}}`… contiguos desde 1 y cada uno tiene un ejemplo no vacío (WhatsApp
    rechaza la plantilla sin ejemplos)."""
    problems: list[str] = []
    found: list[int] = []
    for raw in _PLACEHOLDER.findall(body or ""):
        if not raw.isdigit():
            problems.append(f"El marcador {{{{{raw}}}}} no es un número")
            continue
        found.append(int(raw))
    numbers = sorted(set(found))
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        problems.append("Los marcadores deben ser contiguos desde {{1}}")
    for n in numbers:
        if not str(variables.get(str(n)) or "").strip():
            problems.append(f"Falta el ejemplo de {{{{{n}}}}}")
    extra = sorted(set(variables) - {str(n) for n in numbers})
    if extra:
        problems.append(f"Ejemplos sin marcador en el texto: {', '.join(extra)}")
    return problems


async def create_content(
    client: TwilioClient,
    *,
    friendly_name: str,
    language: str,
    types: dict[str, Any],
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Crea el contenido; devuelve el recurso (con `sid`). No lo envía a aprobación."""
    payload: dict[str, Any] = {"friendly_name": friendly_name, "language": language, "types": types}
    if variables:
        payload["variables"] = variables
    return await client.request("POST", CONTENT_URL, json=payload)


async def get_content(client: TwilioClient, sid: str) -> dict[str, Any]:
    return await client.request("GET", f"{CONTENT_URL}/{check_content_sid(sid)}")


async def list_contents(client: TwilioClient) -> list[dict[str, Any]]:
    return await client.paginate(CONTENT_URL, "contents", params={"PageSize": "500"})


async def delete_content(client: TwilioClient, sid: str) -> None:
    """Borra el contenido en Twilio (no en la WABA)."""
    await client.request("DELETE", f"{CONTENT_URL}/{check_content_sid(sid)}")


async def request_whatsapp_approval(
    client: TwilioClient, sid: str, *, name: str, category: str
) -> dict[str, Any]:
    """Envía la plantilla a aprobación de WhatsApp. `name` solo admite minúsculas, números
    y `_`; `category` es UTILITY, MARKETING o AUTHENTICATION."""
    if not _APPROVAL_NAME.match(name or ""):
        raise ChannelError("El nombre de la plantilla solo admite minúsculas, números y _")
    if category.upper() not in APPROVAL_CATEGORIES:
        raise ChannelError(f"Categoría de plantilla inválida: {category}")
    return await client.request(
        "POST",
        f"{CONTENT_URL}/{check_content_sid(sid)}/ApprovalRequests/whatsapp",
        json={"name": name, "category": category.upper()},
    )


@dataclass(frozen=True)
class Approval:
    status: str  # unsubmitted, received, pending, approved, rejected, paused, disabled
    name: str = ""
    category: str = ""
    rejection_reason: str = ""

    @property
    def approved(self) -> bool:
        return self.status == "approved"


async def fetch_approval(client: TwilioClient, sid: str) -> Approval:
    """Estado de aprobación de WhatsApp. Sin solicitud todavía, `unsubmitted`."""
    raw = await client.request("GET", f"{CONTENT_URL}/{check_content_sid(sid)}/ApprovalRequests")
    whatsapp = raw.get("whatsapp") or {}
    if not isinstance(whatsapp, dict):
        whatsapp = {}
    status = str(whatsapp.get("status") or "unsubmitted").lower()
    return Approval(
        status=status,
        name=str(whatsapp.get("name") or ""),
        category=str(whatsapp.get("category") or ""),
        rejection_reason=str(whatsapp.get("rejection_reason") or ""),
    )
