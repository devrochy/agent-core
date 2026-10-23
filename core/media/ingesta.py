"""Ingesta de media entrante en el consumer (SPEC-04, ADR-0151).

Descarga el media de Meta, lo guarda con el controlador único, lo transcribe o
describe y devuelve un `MediaOutcome` que el consumer usa para persistir el
inbound y decidir si sigue el turno normal, responde un texto fijo o reintenta.

Idempotencia: Redis por `(tenant, external_message_id)` para no repetir la
llamada pagada en un reintento del consumer; el archivo usa un nombre
determinístico para no duplicarse.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from core.agents.responses import load_response
from core.agents.spec import load_active_spec
from core.almacenamiento import StorageInvalido, guardar
from core.billing.tarifario import TARIFARIO_VIGENTE, trm_vigente
from core.db.models import UsageLedger
from core.db.session import get_session
from core.media.config import (
    MediaConfigError,
    resolve_audio_config,
    resolve_vision_config,
)
from core.media.errors import MediaProviderError
from core.media.meta_media import descargar_media
from core.media.prompts import prompt_de_media
from core.media.stt import transcribir_audio
from core.media.vision import describir_imagen
from core.memory.working import get_redis
from core.shared.logger import get_logger
from core.shared.tenant import current_tenant_slug, get_current_tenant_id

log = get_logger(__name__)

_CACHE_TTL_S = 3600

# Fallbacks determinísticos si el playbook `respuestas` no está disponible. El
# texto real vive en core/playbooks/respuestas.md (invariante 3).
RESPUESTAS_FALLBACK: dict[str, str] = {
    "media_audio_unreadable": (
        "No pudimos entender el audio 🎙️ ¿Podés escribirlo o mandarlo de nuevo, más despacio "
        "y sin ruido de fondo?"
    ),
    "media_audio_disabled": "En este momento no puedo escuchar audios. ¿Me lo escribís?",
    "media_audio_unavailable": (
        "En este momento no puedo escuchar audios. ¿Me lo escribís o te comunico con un asesor?"
    ),
    "media_image_unidentified": (
        "No pudimos reconocer bien qué buscás en esa foto 📷 ¿Nos contás qué producto es o "
        "mandás otra foto más de cerca?"
    ),
    "media_download_failed": "No pudimos recibir el archivo. ¿Podés mandarlo de nuevo?",
}


@dataclass(slots=True)
class MediaOutcome:
    handled: bool
    content: str = ""
    effective_message: str = ""
    media_type: str | None = None
    media_key: str | None = None
    media_transcription: str | None = None
    reply: str | None = None
    reply_key: str | None = None
    decision: str = ""

    @classmethod
    def skipped(cls) -> MediaOutcome:
        return cls(handled=False)


def _combinar(caption: str, bloque: str) -> str:
    caption = (caption or "").strip()
    return f"{caption}\n{bloque}".strip() if caption else bloque


def _nombre_archivo(tenant_id: uuid.UUID, external_message_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{external_message_id}".encode()).hexdigest()[:32]


def _clave_cache(tenant_id: uuid.UUID, external_message_id: str) -> str:
    return f"media:result:{tenant_id}:{external_message_id}"


async def _cache_get(clave: str) -> MediaOutcome | None:
    try:
        raw = await get_redis().get(clave)
    except Exception:
        log.warning("media_cache_get_failed", exc_info=True)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("handled"):
        return None
    return MediaOutcome(
        handled=True,
        content=str(data.get("content") or ""),
        effective_message=str(data.get("effective_message") or ""),
        media_type=data.get("media_type"),
        media_key=data.get("media_key"),
        media_transcription=data.get("media_transcription"),
        reply=data.get("reply"),
        reply_key=data.get("reply_key"),
        decision=str(data.get("decision") or ""),
    )


async def _cache_set(clave: str, outcome: MediaOutcome) -> None:
    try:
        await get_redis().set(
            clave,
            json.dumps(
                {
                    "handled": outcome.handled,
                    "content": outcome.content,
                    "effective_message": outcome.effective_message,
                    "media_type": outcome.media_type,
                    "media_key": outcome.media_key,
                    "media_transcription": outcome.media_transcription,
                    "reply": outcome.reply,
                    "reply_key": outcome.reply_key,
                    "decision": outcome.decision,
                }
            ),
            ex=_CACHE_TTL_S,
        )
    except Exception:
        log.warning("media_cache_set_failed", exc_info=True)


async def _guardar(contenido: bytes, mime_type: str, external_message_id: str) -> str | None:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return None
    try:
        slug = await current_tenant_slug()
    except Exception:
        log.warning("media_tenant_slug_failed", exc_info=True)
        return None
    if not slug:
        log.warning("media_sin_slug", tenant_id=str(tenant_id))
        return None
    try:
        ref = guardar(
            "media",
            contenido,
            mimetype=mime_type,
            tenant_slug=slug,
            subcarpeta="entrantes",
            nombre_base=_nombre_archivo(tenant_id, external_message_id),
        )
    except (StorageInvalido, OSError) as exc:
        # El almacenamiento no puede tumbar el turno: se procesa igual y se
        # registra la ausencia de archivo para auditoría.
        log.warning("media_storage_failed", error=type(exc).__name__, tenant_id=str(tenant_id))
        return None
    return ref.key


async def _respuesta(key: str) -> str:
    try:
        spec = await load_active_spec()
        texto = await load_response(spec, key)
    except Exception:
        texto = None
    return (texto or RESPUESTAS_FALLBACK.get(key) or "").strip()


async def _registrar_consumo(
    *,
    kind: str,
    conversation_id: uuid.UUID,
    model: str,
    cost_usd: float,
    key_mode: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    resultado: dict[str, Any],
) -> None:
    """Fila de costo en modo sombra (`credits=0`): mide sin cobrar dos veces."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    try:
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd,
                    kind=kind,
                    credits=Decimal("0.0"),
                    key_mode=key_mode,
                    tarifario_version=TARIFARIO_VIGENTE.version,
                    trm_cop=trm_vigente(),
                    result=resultado,
                )
            )
    except Exception:
        log.warning("media_ledger_failed", kind=kind, exc_info=True)


async def _outcome_fijo(
    key: str,
    *,
    content: str,
    media_type: str,
    media_key: str | None,
    media_transcription: str | None,
    decision: str,
) -> MediaOutcome:
    return MediaOutcome(
        handled=True,
        content=content,
        effective_message=content,
        media_type=media_type,
        media_key=media_key,
        media_transcription=media_transcription,
        reply=await _respuesta(key),
        reply_key=key,
        decision=decision,
    )


def _parsear_descripcion(texto: str | None) -> str | None:
    """Espera `{"descripcion": "..."}` o `{"descripcion": null}`.

    Si el modelo se sale del formato se trata como "no identificado": es más
    seguro pedir otra foto que inventar una búsqueda con una frase suelta.
    """
    if not texto:
        return None
    limpio = texto.strip()
    if limpio.startswith("```"):
        limpio = limpio.strip("`")
        if limpio.startswith("json"):
            limpio = limpio[4:].strip()
    inicio, fin = limpio.find("{"), limpio.rfind("}")
    if inicio < 0 or fin <= inicio:
        return None
    try:
        data = json.loads(limpio[inicio : fin + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    descripcion = data.get("descripcion")
    if descripcion is None:
        return None
    if isinstance(descripcion, str) and descripcion.strip():
        return descripcion.strip()[:500]
    return None


async def _procesar_audio(
    *,
    contenido: bytes,
    mime_type: str,
    caption: str,
    media_key: str | None,
    conversation_id: uuid.UUID,
    app_settings: Any,
) -> MediaOutcome:
    if not getattr(app_settings, "audio_enabled", True):
        return await _outcome_fijo(
            "media_audio_disabled",
            content=caption or "[Nota de voz]",
            media_type=mime_type,
            media_key=media_key,
            media_transcription=None,
            decision="audio_deshabilitado",
        )
    try:
        config = resolve_audio_config(app_settings)
    except MediaConfigError as exc:
        log.warning("media_audio_config_error", code=exc.code, conversation_id=str(conversation_id))
        return await _outcome_fijo(
            "media_audio_unavailable",
            content=caption or "[Nota de voz]",
            media_type=mime_type,
            media_key=media_key,
            media_transcription=None,
            decision="audio_no_configurado",
        )
    if not config.enabled:
        return await _outcome_fijo(
            "media_audio_disabled",
            content=caption or "[Nota de voz]",
            media_type=mime_type,
            media_key=media_key,
            media_transcription=None,
            decision="audio_deshabilitado",
        )

    inicio = time.monotonic()
    resultado = await transcribir_audio(
        contenido,
        mime_type,
        provider=config.provider,
        model=config.model,
        api_key=config.api_key,
        language=(getattr(app_settings, "language", None) or "es"),
    )
    latencia = int((time.monotonic() - inicio) * 1000)
    if not resultado.text:
        return await _outcome_fijo(
            "media_audio_unreadable",
            content=caption or "[Nota de voz]",
            media_type=mime_type,
            media_key=media_key,
            media_transcription=None,
            decision="audio_ilegible",
        )

    await _registrar_consumo(
        kind="stt",
        conversation_id=conversation_id,
        model=resultado.model,
        cost_usd=resultado.cost_usd,
        key_mode=config.key_mode,
        input_tokens=0,
        output_tokens=0,
        latency_ms=latencia,
        resultado={
            "provider": resultado.provider,
            "duration_s": resultado.duration_s,
            "media_key": media_key,
        },
    )
    log.info(
        "media_audio_transcrito",
        conversation_id=str(conversation_id),
        provider=resultado.provider,
        model=resultado.model,
        duration_s=resultado.duration_s,
    )
    return MediaOutcome(
        handled=True,
        content=caption or "[Nota de voz]",
        effective_message=_combinar(caption, f"[Nota de voz] {resultado.text}"),
        media_type=mime_type,
        media_key=media_key,
        media_transcription=resultado.text,
        decision="audio_transcrito",
    )


async def _procesar_imagen(
    *,
    contenido: bytes,
    mime_type: str,
    caption: str,
    media_key: str | None,
    conversation_id: uuid.UUID,
    app_settings: Any,
) -> MediaOutcome:
    config = None
    if (
        getattr(app_settings, "vision_enabled", True)
        and getattr(app_settings, "vision_mode", "describir") == "describir"
    ):
        try:
            config = resolve_vision_config(app_settings)
        except MediaConfigError as exc:
            log.warning("media_vision_config_error", code=exc.code, conversation_id=str(conversation_id))

    if config is not None and config.mode == "describir":
        prompt = prompt_de_media(
            "imagen_descripcion",
            business_description=(getattr(app_settings, "business_description", "") or ""),
            language=(getattr(app_settings, "language", None) or "es"),
        )
        if prompt:
            inicio = time.monotonic()
            try:
                resultado = await describir_imagen(
                    contenido,
                    mime_type,
                    provider=config.provider,
                    model=config.model,
                    api_key=config.api_key,
                    prompt=prompt,
                )
            except MediaProviderError as exc:
                if exc.transient:
                    raise
                log.warning(
                    "media_vision_error",
                    code=exc.code,
                    conversation_id=str(conversation_id),
                )
            else:
                latencia = int((time.monotonic() - inicio) * 1000)
                descripcion = _parsear_descripcion(resultado.text)
                if descripcion:
                    await _registrar_consumo(
                        kind="vision",
                        conversation_id=conversation_id,
                        model=resultado.model,
                        cost_usd=resultado.cost_usd,
                        key_mode=config.key_mode,
                        input_tokens=resultado.input_tokens,
                        output_tokens=resultado.output_tokens,
                        latency_ms=latencia,
                        resultado={
                            "provider": resultado.provider,
                            "media_key": media_key,
                            "tokens_in": resultado.input_tokens,
                            "tokens_out": resultado.output_tokens,
                        },
                    )
                    log.info(
                        "media_imagen_descrita",
                        conversation_id=str(conversation_id),
                        provider=resultado.provider,
                        model=resultado.model,
                    )
                    return MediaOutcome(
                        handled=True,
                        content=caption or "[Imagen]",
                        effective_message=_combinar(
                            caption,
                            f"[Foto] El cliente mandó una foto. Descripción automática: {descripcion}",
                        ),
                        media_type=mime_type,
                        media_key=media_key,
                        media_transcription=descripcion,
                        decision="imagen_descrita",
                    )
                return await _outcome_fijo(
                    "media_image_unidentified",
                    content=caption or "[Imagen]",
                    media_type=mime_type,
                    media_key=media_key,
                    media_transcription=None,
                    decision="imagen_no_identificada",
                )

    # Sin visión (deshabilitada, sin llave o modo sin descripción): el agente
    # recibe el aviso y pregunta; no se inventa una descripción.
    return MediaOutcome(
        handled=True,
        content=caption or "[Imagen]",
        effective_message=_combinar(caption, "[Imagen adjunta] El cliente mandó una imagen."),
        media_type=mime_type,
        media_key=media_key,
        media_transcription=None,
        decision="imagen_sin_descripcion",
    )


async def procesar_media_entrante(
    fields: dict[str, str],
    conversation: Any,
    app_settings: Any,
) -> MediaOutcome:
    """Procesa el primer media del mensaje. `handled=False` = sigue el camino actual."""
    if fields.get("media_context"):
        return MediaOutcome.skipped()
    try:
        items = json.loads(fields.get("media", "[]") or "[]")
    except Exception:
        return MediaOutcome.skipped()
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return MediaOutcome.skipped()
    item = items[0]
    mime_type = str(item.get("type") or "").split(";")[0].strip().lower()
    media_id = str(item.get("id") or "")
    if not (mime_type.startswith("audio/") or mime_type.startswith("image/")):
        return MediaOutcome.skipped()

    tenant_id = get_current_tenant_id()
    external_id = fields.get("external_message_id", "")
    if tenant_id is None:
        return MediaOutcome.skipped()
    # Solo Meta Cloud en esta tanda (ADR-0151): waweb y web conservan su camino.
    if fields.get("provider") != "meta" or fields.get("channel") != "whatsapp":
        log.info(
            "media_canal_no_soportado",
            provider=fields.get("provider"),
            channel=fields.get("channel"),
        )
        return MediaOutcome.skipped()

    clave = _clave_cache(tenant_id, external_id) if external_id else ""
    if clave:
        cacheado = await _cache_get(clave)
        if cacheado is not None:
            log.info("media_cache_hit", conversation_id=str(conversation.id), kind=mime_type)
            return cacheado

    # Solo Meta Cloud en esta tanda (ADR-0151). WA Web y chat web conservan su camino.
    from core.gateway.sender import resolve_credentials

    try:
        provider, creds = await resolve_credentials(
            getattr(conversation, "connection_id", None),
            getattr(conversation, "meta_connection_id", None),
            tenant_id,
        )
    except Exception:
        log.warning("media_credenciales_failed", conversation_id=str(conversation.id), exc_info=True)
        return MediaOutcome.skipped()
    if provider != "meta" or not creds.get("access_token"):
        log.warning(
            "media_sin_credenciales",
            conversation_id=str(conversation.id),
            provider=provider,
        )
        outcome = await _outcome_fijo(
            "media_download_failed",
            content=fields.get("content", "") or "[Archivo adjunto]",
            media_type=mime_type,
            media_key=None,
            media_transcription=None,
            decision="media_sin_credenciales",
        )
        if clave:
            await _cache_set(clave, outcome)
        return outcome
    if not media_id:
        outcome = await _outcome_fijo(
            "media_download_failed",
            content=fields.get("content", "") or "[Archivo adjunto]",
            media_type=mime_type,
            media_key=None,
            media_transcription=None,
            decision="media_sin_id",
        )
        if clave:
            await _cache_set(clave, outcome)
        return outcome

    try:
        media = await descargar_media(media_id, str(creds.get("access_token") or ""))
    except MediaProviderError as exc:
        if exc.transient:
            raise
        log.warning(
            "media_descarga_error",
            conversation_id=str(conversation.id),
            code=exc.code,
        )
        outcome = await _outcome_fijo(
            "media_download_failed",
            content=fields.get("content", "") or "[Archivo adjunto]",
            media_type=mime_type,
            media_key=None,
            media_transcription=None,
            decision="media_descarga_fallida",
        )
        if clave:
            await _cache_set(clave, outcome)
        return outcome

    media_key = await _guardar(media.contenido, media.mime_type, external_id)
    caption = fields.get("content", "")
    if mime_type.startswith("audio/"):
        outcome = await _procesar_audio(
            contenido=media.contenido,
            mime_type=media.mime_type,
            caption=caption,
            media_key=media_key,
            conversation_id=conversation.id,
            app_settings=app_settings,
        )
    else:
        outcome = await _procesar_imagen(
            contenido=media.contenido,
            mime_type=media.mime_type,
            caption=caption,
            media_key=media_key,
            conversation_id=conversation.id,
            app_settings=app_settings,
        )
    if clave:
        await _cache_set(clave, outcome)
    return outcome


__all__ = ["MediaOutcome", "procesar_media_entrante"]
