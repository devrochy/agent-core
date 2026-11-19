"""Content API de Twilio: plantillas de WhatsApp (ADR-0186).

Una plantilla es un Content (`HX…`) con su cuerpo y variables `{{1}}`, `{{2}}`…; para usarla
fuera de la ventana de 24 h se manda a aprobación de WhatsApp. Twilio no avisa por webhook cuando
cambia el estado de la aprobación: el core lo consulta (polling).

`validate_variables` es puro: el core lo corre antes de llamar a Twilio.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agentcore_channel_twilio.client import CONTENT_BASE, TwilioClient, require_sid
from core.gateway.channels.base import ProviderError

CONTENT_URL = f"{CONTENT_BASE}/Content"
CONTENT_AND_APPROVALS_URL = f"{CONTENT_BASE}/ContentAndApprovals"
CATEGORIES = frozenset({"UTILITY", "MARKETING", "AUTHENTICATION"})
# Estados de la aprobación de WhatsApp, en minúsculas. `unsubmitted` es nuestro: un Content sin
# solicitud de aprobación todavía.
APPROVAL_STATUSES = frozenset(
    {"unsubmitted", "received", "pending", "approved", "rejected", "paused", "disabled"}
)
_PLACEHOLDER = re.compile(r"\{\{\s*(\d+)\s*\}\}")


@dataclass(frozen=True)
class Approval:
    status: str
    name: str = ""
    category: str = ""
    rejection_reason: str = ""

    @classmethod
    def from_api(cls, body: Mapping[str, Any] | None) -> Approval:
        body = body or {}
        status = str(body.get("status") or "unsubmitted").lower()
        return cls(
            status=status if status in APPROVAL_STATUSES else status or "unsubmitted",
            name=str(body.get("name") or ""),
            category=str(body.get("category") or "").upper(),
            rejection_reason=str(body.get("rejection_reason") or ""),
        )


@dataclass(frozen=True)
class ContentTemplate:
    sid: str
    friendly_name: str
    language: str
    variables: dict[str, str] = field(default_factory=dict)
    types: dict[str, Any] = field(default_factory=dict)
    approval: Approval = field(default_factory=lambda: Approval("unsubmitted"))

    @classmethod
    def from_api(cls, body: Mapping[str, Any]) -> ContentTemplate:
        approvals = body.get("approval_requests") or {}
        return cls(
            sid=str(body.get("sid") or ""),
            friendly_name=str(body.get("friendly_name") or ""),
            language=str(body.get("language") or ""),
            variables={str(k): str(v) for k, v in (body.get("variables") or {}).items()},
            types=dict(body.get("types") or {}),
            approval=Approval.from_api(approvals if approvals.get("status") else None),
        )


def placeholders(texts: Iterable[str]) -> list[int]:
    """Índices de las variables `{{n}}` que aparecen en los textos, ordenados y sin repetir."""
    found: set[int] = set()
    for text in texts:
        found.update(int(n) for n in _PLACEHOLDER.findall(text or ""))
    return sorted(found)


def validate_variables(texts: Iterable[str], examples: Mapping[str, str]) -> None:
    """Las variables van de `{{1}}` en adelante sin saltos y cada una trae su valor de ejemplo
    (WhatsApp rechaza la plantilla si no). Lanza `ProviderError` CONTENT con el motivo."""
    used = placeholders(texts)
    if used and used != list(range(1, len(used) + 1)):
        raise ProviderError(
            f"Las variables deben ir de {{{{1}}}} a {{{{{len(used)}}}}} sin saltos (hay {used})",
            provider="twilio",
            category="CONTENT",
        )
    missing = [n for n in used if not str(examples.get(str(n)) or "").strip()]
    if missing:
        raise ProviderError(
            f"Falta el valor de ejemplo de las variables {missing}",
            provider="twilio",
            category="CONTENT",
        )
    extra = sorted(set(examples) - {str(n) for n in used})
    if extra:
        raise ProviderError(
            f"Hay ejemplos de variables que el texto no usa: {extra}",
            provider="twilio",
            category="CONTENT",
        )


async def create_content(
    client: TwilioClient,
    *,
    friendly_name: str,
    language: str,
    types: Mapping[str, Any],
    variables: Mapping[str, str],
) -> ContentTemplate:
    payload = {
        "friendly_name": friendly_name,
        "language": language,
        "variables": dict(variables),
        "types": dict(types),
    }
    body = await client.post_json(CONTENT_URL, payload, op="content.create")
    return ContentTemplate.from_api(body)


async def submit_whatsapp_approval(
    client: TwilioClient, content_sid: str, *, name: str, category: str
) -> Approval:
    category = category.upper()
    if category not in CATEGORIES:
        raise ProviderError(
            f"Categoría desconocida: {category}", provider="twilio", category="CONTENT"
        )
    body = await client.post_json(
        f"{CONTENT_URL}/{require_sid(content_sid, 'HX')}/ApprovalRequests/whatsapp",
        {"name": name, "category": category},
        op="content.submit_approval",
    )
    return Approval.from_api(body)


async def fetch_approval(client: TwilioClient, content_sid: str) -> Approval:
    body = await client.get(
        f"{CONTENT_URL}/{require_sid(content_sid, 'HX')}/ApprovalRequests",
        op="content.fetch_approval",
    )
    return Approval.from_api(body.get("whatsapp"))


async def list_contents_with_approvals(client: TwilioClient) -> list[ContentTemplate]:
    contenidos, _completo = await list_contents_completo(client)
    return contenidos


async def list_contents_completo(client: TwilioClient) -> tuple[list[ContentTemplate], bool]:
    """Todos los Content de la cuenta con su aprobación, y si la lista está completa (500 por página, el máximo)."""
    rows, completo = await client.paginate_completo(
        CONTENT_AND_APPROVALS_URL, key="contents", op="content.list", params={"PageSize": 500}
    )
    return [ContentTemplate.from_api(row) for row in rows], completo


async def delete_content(client: TwilioClient, content_sid: str) -> None:
    await client.delete(f"{CONTENT_URL}/{require_sid(content_sid, 'HX')}", op="content.delete")
