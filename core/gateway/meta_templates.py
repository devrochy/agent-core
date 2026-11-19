"""CRUD de plantillas de WhatsApp (`message_templates`) contra
`graph.facebook.com` -- mismo estilo que `meta_phone_numbers.py`.

`parameter_format` se manda en MAYÚSCULAS ("NAMED"/"POSITIONAL"), tal como
lo define la referencia de Graph API -- hay un conflicto documental real con
los ejemplos de Template fundamentals (minúsculas); si Meta llegara a
rechazar el valor en mayúsculas, este comentario se actualiza con el
resultado real observado. No mezclar ambos casings en el código.

Nombres EXACTOS de `example` (causa nº1 de 400s, ver `validate_template_
components`): `header_text`, `header_text_named_params`, `header_handle`,
`body_text` (array DE ARRAYS), `body_text_named_params`. BODY es obligatorio
en toda plantilla; FOOTER no lleva `example`.

Editar una plantilla es `POST /{TEMPLATE_ID}` (nodo WhatsAppBusinessHSM) --
NUNCA `POST /{WABA_ID}/message_templates?hsm_id=`, esa vía quedó obsoleta.
`update_template` no acepta `name`/`language` ni como parámetro: no se
pueden cambiar y así es imposible pasarlos por error.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from core.db.models import MessageTemplate, utcnow
from core.db.session import get_session
from core.gateway.meta_graph import graph_fetch
from core.memory.working import get_redis
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

ALLOWED_BUTTON_TYPES = {"QUICK_REPLY", "URL", "PHONE_NUMBER", "COPY_CODE"}
# FLOW y OTP explícitamente NO soportados -- su sintaxis no está verificada
# contra la doc de 2026 (ver ticket). Antes de agregarlos hace falta una
# llamada de prueba real que confirme la forma exacta.
_UNVERIFIED_BUTTON_TYPES = {"FLOW", "OTP"}

EDITABLE_STATUSES = {"APPROVED", "REJECTED", "PAUSED"}
MAX_EDITS_PER_MONTH = 10

_CREATE_RATE_LIMIT = 100
_CREATE_RATE_WINDOW_S = 3600

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


class TemplateValidationError(AgentCoreError):
    """Falla de validación LOCAL -- nunca se llega a llamar a Meta."""


class TemplateRateLimitError(AgentCoreError):
    """Límite propio de 100 creaciones/hora por WABA alcanzado."""


def _named_params(example: dict[str, Any] | None, key: str) -> dict[str, str]:
    if not example:
        return {}
    entries = example.get(key) or []
    return {str(e.get("param_name")): str(e.get("example")) for e in entries if isinstance(e, dict)}


def _validate_named_params(component_type: str, text: str, param_names: set[str]) -> None:
    placeholders = set(_PLACEHOLDER_RE.findall(text or ""))
    missing_examples = placeholders - param_names
    unused_params = param_names - placeholders
    if missing_examples:
        names = "/".join(f"{{{{{n}}}}}" for n in sorted(missing_examples))
        raise TemplateValidationError(
            f"{component_type}: el texto usa {names} pero no hay example.param_name para ese/esos parámetro/s"
        )
    if unused_params:
        raise TemplateValidationError(
            f"{component_type}: declaraste param_name {sorted(unused_params)} que no aparece "
            f"como {{{{...}}}} en el texto"
        )


def validate_template_components(
    category: str, components: list[dict[str, Any]], parameter_format: str | None
) -> None:
    """Levanta `TemplateValidationError` nombrando el componente y el
    parámetro concreto -- nunca un mensaje genérico. Se llama SIEMPRE antes
    de gastar el rate limit de creación contra Meta."""
    if category not in ("UTILITY", "MARKETING", "AUTHENTICATION"):
        raise TemplateValidationError(f"category inválida: {category!r} (UTILITY|MARKETING|AUTHENTICATION)")

    body_components = [c for c in components if c.get("type") == "BODY"]
    if not body_components:
        raise TemplateValidationError("BODY es obligatorio en toda plantilla")

    for component in components:
        ctype = component.get("type")
        example = component.get("example")

        if ctype == "HEADER":
            fmt = component.get("format", "TEXT")
            if fmt == "TEXT":
                text = component.get("text", "")
                if parameter_format == "NAMED":
                    _validate_named_params("HEADER", text, set(_named_params(example, "header_text_named_params")))
                elif "{{" in text and not (example or {}).get("header_text"):
                    raise TemplateValidationError("HEADER: falta example.header_text para el parámetro posicional")
            elif fmt in ("IMAGE", "VIDEO", "DOCUMENT"):
                if not (example or {}).get("header_handle"):
                    raise TemplateValidationError(f"HEADER ({fmt}): falta example.header_handle")

        elif ctype == "BODY":
            text = component.get("text", "")
            if parameter_format == "NAMED":
                _validate_named_params("BODY", text, set(_named_params(example, "body_text_named_params")))
            elif "{{" in text:
                body_text = (example or {}).get("body_text")
                if not body_text or not isinstance(body_text, list) or not isinstance(body_text[0], list):
                    raise TemplateValidationError(
                        "BODY: example.body_text debe ser un ARRAY DE ARRAYS (ej. [[\"v1\",\"v2\"]])"
                    )

        elif ctype == "FOOTER":
            if component.get("example"):
                raise TemplateValidationError("FOOTER no lleva example")

        elif ctype == "BUTTONS":
            for button in component.get("buttons", []):
                btype = button.get("type")
                if btype in _UNVERIFIED_BUTTON_TYPES:
                    raise TemplateValidationError(
                        f"botón {btype}: no soportado todavía -- su sintaxis no está verificada contra la "
                        f"doc primaria de 2026, hace falta una llamada de prueba real primero"
                    )
                if btype not in ALLOWED_BUTTON_TYPES:
                    raise TemplateValidationError(f"tipo de botón no reconocido: {btype!r}")


async def _check_and_bump_create_rate_limit(waba_id: str) -> None:
    """100 creaciones/hora por WABA -- no existe un rate limiter reusable en
    el repo, se construye acá con `INCR`+`EXPIRE` (el único precedente,
    `core/admin/views.py` línea ~1639, es un flag binario `SET ... EX`, no
    sirve para contar)."""
    redis = get_redis()
    hour_key = datetime.now(UTC).strftime("%Y-%m-%d-%H")
    key = f"tpl_create_rl:{waba_id}:{hour_key}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _CREATE_RATE_WINDOW_S)
    if count > _CREATE_RATE_LIMIT:
        raise TemplateRateLimitError(
            f"Límite de {_CREATE_RATE_LIMIT} plantillas/hora alcanzado para esta WABA -- "
            f"esperá a la próxima hora"
        )


async def post_template(
    waba_id: str,
    token: str,
    app_secret: str,
    version: str,
    connection_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    name: str,
    language: str,
    category: str,
    components: list[dict[str, Any]],
    parameter_format: str | None = None,
    allow_category_change: bool = True,
) -> tuple[str | None, str]:
    """Valida, gasta el límite de creación y hace el POST a Meta. Devuelve `(id de Meta, status)` SIN tocar la base:
    quien llama decide cómo persistir (`create_template` inserta la fila; las plantillas con opciones, HU-OPC-04,
    completan una fila que reservaron ANTES del POST para poder reintentar sin duplicar)."""
    if len(name) > 512:
        raise TemplateValidationError("name: máximo 512 caracteres")
    validate_template_components(category, components, parameter_format)
    await _check_and_bump_create_rate_limit(waba_id)

    body: dict[str, Any] = {
        "name": name,
        "language": language,
        "category": category,
        "components": components,
        "allow_category_change": allow_category_change,
    }
    if parameter_format:
        body["parameter_format"] = parameter_format

    data = await graph_fetch(
        f"/{waba_id}/message_templates", method="POST", token=token, app_secret=app_secret,
        body=body, version=version, connection_id=connection_id, tenant_id=tenant_id,
    )
    meta_template_id = str((data or {}).get("id") or "") or None
    # Meta normalmente devuelve "PENDING" acá, pero no se asume: se lee del
    # campo `status` de la respuesta si viene, default explícito si no.
    return meta_template_id, str((data or {}).get("status") or "PENDING")


async def find_template_by_name(
    waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID, *, name: str, language: str
) -> dict[str, Any] | None:
    """`GET /{waba_id}/message_templates?name=` -- la plantilla de ESE nombre e idioma, o None. Es el filtro por
    `name` de Graph (documentado; no verificado contra Meta real desde este entorno): se usa solo para conciliar un
    reintento tras un POST de resultado desconocido, y se vuelve a comparar el nombre y el idioma de lo que
    responde, así que un filtro ignorado no adopta otra plantilla."""
    data = await graph_fetch(
        f"/{waba_id}/message_templates", token=token, app_secret=app_secret,
        query={"name": name, "fields": "id,name,status,category,language,parameter_format", "limit": "100"},
        version=version, connection_id=connection_id,
    )
    for item in (data or {}).get("data", []):
        if item.get("name") == name and item.get("language") == language:
            return item
    return None


async def create_template(
    waba_id: str,
    token: str,
    app_secret: str,
    version: str,
    connection_id: uuid.UUID,
    tenant_id: uuid.UUID,
    waba_account_id: uuid.UUID,
    *,
    name: str,
    language: str,
    category: str,
    components: list[dict[str, Any]],
    parameter_format: str | None = None,
    allow_category_change: bool = True,
) -> MessageTemplate:
    meta_template_id, status = await post_template(
        waba_id, token, app_secret, version, connection_id, tenant_id, name=name, language=language,
        category=category, components=components, parameter_format=parameter_format,
        allow_category_change=allow_category_change,
    )
    async with get_session() as session:
        row = MessageTemplate(
            tenant_id=tenant_id, waba_account_id=waba_account_id, meta_template_id=meta_template_id,
            name=name, language=language, category=category, parameter_format=parameter_format,
            status=status, components=components, synced_at=utcnow(),
        )
        session.add(row)
        await session.flush()
        return row


async def update_template(
    meta_template_id: str,
    token: str,
    app_secret: str,
    version: str,
    connection_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    category: str | None = None,
    components: list[dict[str, Any]] | None = None,
    message_send_ttl_seconds: int | None = None,
    parameter_format: str | None = None,
    creative_sourcing_spec: dict[str, Any] | None = None,
) -> None:
    async with get_session() as session:
        row = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.meta_template_id == meta_template_id, MessageTemplate.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise TemplateValidationError("plantilla no encontrada")
        if row.status not in EDITABLE_STATUSES:
            raise TemplateValidationError(
                f"no se puede editar una plantilla en estado {row.status} "
                f"(solo APPROVED, REJECTED o PAUSED)"
            )

        month_key = datetime.now(UTC).strftime("%Y-%m")
        edits_this_month = row.edits_this_month if row.edits_month_key == month_key else 0
        if edits_this_month >= MAX_EDITS_PER_MONTH:
            raise TemplateValidationError(f"límite de {MAX_EDITS_PER_MONTH} ediciones/mes alcanzado")
        if row.last_edited_at and row.last_edited_at.date() == datetime.now(UTC).date():
            raise TemplateValidationError("ya se editó esta plantilla hoy -- máximo 1 edición/día")

        if category is not None:
            validate_template_components(category, components or (row.components or []), parameter_format)

        body: dict[str, Any] = {}
        if category is not None:
            body["category"] = category
        if components is not None:
            body["components"] = components
        if message_send_ttl_seconds is not None:
            body["message_send_ttl_seconds"] = message_send_ttl_seconds
        if parameter_format is not None:
            body["parameter_format"] = parameter_format
        if creative_sourcing_spec is not None:
            body["creative_sourcing_spec"] = creative_sourcing_spec

        await graph_fetch(
            f"/{meta_template_id}", method="POST", token=token, app_secret=app_secret,
            body=body, version=version, connection_id=connection_id, tenant_id=tenant_id,
        )

        if category is not None:
            row.category = category
        if components is not None:
            row.components = components
        if parameter_format is not None:
            row.parameter_format = parameter_format
        row.last_edited_at = utcnow()
        row.edits_this_month = edits_this_month + 1
        row.edits_month_key = month_key


async def delete_template_all_languages(
    waba_id: str,
    name: str,
    token: str,
    app_secret: str,
    version: str,
    connection_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    waba_account_id: uuid.UUID | None = None,
) -> None:
    """`DELETE /{WABA_ID}/message_templates?name={NAME}` -- borra TODAS las
    versiones de idioma de esa plantilla. Con `waba_account_id` solo marca las filas de esa
    WABA (el mismo nombre puede existir en otra WABA del negocio y allá sigue viva)."""
    await graph_fetch(
        f"/{waba_id}/message_templates", method="DELETE", token=token, app_secret=app_secret,
        query={"name": name}, version=version, connection_id=connection_id, tenant_id=tenant_id,
    )
    async with get_session() as session:
        query = select(MessageTemplate).where(MessageTemplate.name == name, MessageTemplate.tenant_id == tenant_id)
        if waba_account_id is not None:
            query = query.where(MessageTemplate.waba_account_id == waba_account_id)
        rows = (await session.execute(query)).scalars().all()
        for row in rows:
            row.status = "DELETED"


async def delete_template_one_language(
    waba_id: str, name: str, meta_template_id: str, token: str, app_secret: str, version: str,
    connection_id: uuid.UUID, tenant_id: uuid.UUID,
) -> None:
    """`DELETE /{WABA_ID}/message_templates?name={NAME}&hsm_id={TEMPLATE_ID}`
    -- borra SOLO esa versión de idioma."""
    await graph_fetch(
        f"/{waba_id}/message_templates", method="DELETE", token=token, app_secret=app_secret,
        query={"name": name, "hsm_id": meta_template_id}, version=version,
        connection_id=connection_id, tenant_id=tenant_id,
    )
    async with get_session() as session:
        row = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.meta_template_id == meta_template_id, MessageTemplate.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = "DELETED"


async def delete_templates_bulk(
    waba_id: str, meta_template_ids: list[str], token: str, app_secret: str, version: str,
    connection_id: uuid.UUID, tenant_id: uuid.UUID,
) -> None:
    """`DELETE /{WABA_ID}/message_templates?hsm_ids=[ID1,ID2]` -- el formato
    exacto de serialización de `hsm_ids` NO está verificado contra doc
    primaria de 2026 (ticket). Se prueba primero como array JSON en el query
    string (convención general de Graph API para params de tipo lista);
    NO hay forma de confirmar esto contra Meta real desde este entorno --
    si en staging Meta rechaza el formato, actualizar este comentario y el
    query de acá con la forma que sí funcionó."""
    import json

    await graph_fetch(
        f"/{waba_id}/message_templates", method="DELETE", token=token, app_secret=app_secret,
        query={"hsm_ids": json.dumps(meta_template_ids)}, version=version,
        connection_id=connection_id, tenant_id=tenant_id,
    )
    async with get_session() as session:
        rows = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.meta_template_id.in_(meta_template_ids), MessageTemplate.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        for row in rows:
            row.status = "DELETED"


async def get_template_limits(
    waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> dict[str, Any]:
    """`GET /{waba_id}/message_templates?limit=1&summary=...` -- SIEMPRE se
    usa `message_template_limit` de acá, nunca un valor hardcodeado."""
    data = await graph_fetch(
        f"/{waba_id}/message_templates",
        token=token, app_secret=app_secret,
        query={
            "limit": "1",
            "summary": "total_count,message_template_count,message_template_limit,are_translations_complete",
        },
        version=version, connection_id=connection_id,
    )
    return (data or {}).get("summary") or {}
