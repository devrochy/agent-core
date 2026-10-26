"""Políticas deterministas del turno antes de gastar LLM (HU-AOS-10, design §3.5).

Todas las políticas de entrada viven aquí, con el mismo orden, las mismas lecturas y los mismos
textos que antes de mudarlas. Cada bloqueo deja su decisión en `core_decisions`
(`core.kernel.decisions`).

- **Al entrar el mensaje** (`politicas_de_entrada`, la llama el consumer después de guardar el
  inbound): conexión pausada o eliminada (ADR-0122 y su adenda), kill-switch en tres niveles o
  conversación escalada, palabras de escalamiento y horario de atención (ADR-0051).
- **Al disparar un turno diferido** (`conexion_pausada`, la llama el debounce): la conexión se
  pausó después de agendar el turno.
- **En el preámbulo del loop** (`conversacion_bloqueada`, `politicas_del_turno`): conversación
  pausada o escalada, cuota del período y presupuesto agotado.

Lo que no es política se queda en el orquestador: el negocio no operativo y la cuota de altas
descartan el inbound antes de que haya conversación (no hay turno ni fila), y la velocidad de
respuesta decide el despacho (inmediato o debounce). El kernel no importa el loop, el consumer
ni el debounce (contrato `kernel-sin-orquestador`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from core.agents.responses import load_response
from core.kernel.decisions import bloquear, marcar_escalado
from core.kernel.fuera_de_horario import primer_aviso, soltar_aviso, texto_por_defecto
from core.orchestrator.escalation import (
    ESCALATION_REPLY,
    matches_escalation_keywords,
    within_business_hours,
)
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Se agotó una cuota del período (mensajes o costo LLM). El texto real vive en
# core/playbooks/respuestas.md (`## quota_exhausted`); esto es el fallback
# determinístico si el playbook no está disponible (mismo patrón que FALLBACK_REPLY).
QUOTA_EXHAUSTED_REPLY = (
    "Por ahora no puedo seguir atendiendo esta conversación. "
    "Un asesor humano puede continuar la atención."
)


@dataclass(frozen=True)
class Bloqueo:
    motivo: str
    # Texto de cierre que se responde (None = el turno no responde nada).
    texto: str | None = None


async def conversacion_bloqueada(conversation: Any, customer: Any) -> bool:
    """Sin cliente, con el bot pausado o con la conversación escalada no corre el turno
    (la atiende una persona o el negocio la pausó)."""
    if customer is None:
        return True
    if conversation.status == "escalated":
        await bloquear(conversation, "escalated_conversation", status="silent")
        return True
    if conversation.bot_paused:
        await bloquear(conversation, "kill_switch", status="silent")
        return True
    return False


async def politicas_del_turno(conversation: Any, spec: Any) -> Bloqueo | None:
    """Cuota del período y presupuesto de la conversación, antes de gastar LLM."""
    # Cuota de turno (ADR-0045): si el período agotó `mensajes` o
    # `costo_llm_usd`, se corta el turno antes de gastar LLM. El bloqueo por
    # clientes/conversaciones/audio/OCR ocurre en el consumer/webhook (acción).
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        from core.billing.enforcement import turn_quota_metric

        blocked = await turn_quota_metric(tenant_id)
        if blocked is not None:
            log.warning(
                "turn_quota_exceeded",
                tenant_id=str(tenant_id),
                metric=blocked,
                conversation_id=str(conversation.id),
            )
            await bloquear(conversation, "quota")
            closure = await load_response(spec, "quota_exhausted") or QUOTA_EXHAUSTED_REPLY
            return Bloqueo("quota", closure)

    # Presupuesto agotado (ADR-0015): no se gasta más LLM en esta conversación
    # hasta que un admin suba el presupuesto o lo resetee.
    if conversation.state.get("budget_exhausted"):
        await bloquear(conversation, "budget", status="silent")
        return Bloqueo("budget")
    return None


# ------------------------------------------------------------------ políticas de entrada
async def conexion_pausada(
    conversation: Any, *, avisar_duenos: bool = True, evento: str = "inbound_conexion_pausada"
) -> bool:
    """Conexión pausada o eliminada (ADR-0122 y su adenda 2026-10-19): la cuenta de Meta o la
    propia conexión sin cuenta (manual, WhatsApp Web, correo, chat web). El mensaje ya quedó
    guardado y no corre la IA. `avisar_duenos` manda el push a los dueños (una vez al día por
    conexión); al disparar un turno diferido no se avisa otra vez. `evento` es el nombre del log."""
    # Imports en la llamada: los tests parchean `core.gateway.meta_connection_state`.
    from core.gateway.meta_connection_state import (
        avisar_mensaje_en_conexion_pausada,
        pausa_de_conversacion,
    )

    pausa = await pausa_de_conversacion(conversation)
    if pausa is None:
        return False
    log.info(evento, conversation_id=str(conversation.id), motivo=pausa.motivo, alcance=pausa.alcance)
    await bloquear(conversation, "connection_paused", status="silent")
    if avisar_duenos:
        if pausa.alcance == "meta_account":
            await avisar_mensaje_en_conexion_pausada(conversation.tenant_id, pausa.id, pausa.motivo)
        else:
            await avisar_mensaje_en_conexion_pausada(
                conversation.tenant_id, pausa.id, pausa.motivo, alcance=pausa.alcance
            )
    return True


async def politicas_de_entrada(
    conversation: Any, customer: Any, app_settings: Any, mensaje: str
) -> bool:
    """Políticas del mensaje que entra, en orden fijo y sin LLM. `True` = el turno terminó aquí
    (la respuesta fija y el escalamiento, si los hay, ya se hicieron); `False` = sigue al
    despacho. El inbound ya está guardado: el historial queda completo aunque se bloquee."""
    # Imports en la llamada: los tests parchean `core.gateway.sender.send_to_conversation`,
    # `core.orchestrator.escalation.escalate` y `core.agents.spec.load_active_spec`.
    from core.agents.spec import load_active_spec
    from core.gateway.sender import send_to_conversation
    from core.orchestrator.escalation import escalate

    # ---------------- conexión pausada (ADR-0122): ni horario ni keywords, que también responden
    if await conexion_pausada(conversation):
        return True

    # ---------------- kill-switch en 3 niveles (negocio > cliente > conversación) o escalada
    paused = (
        (app_settings.bot_paused if app_settings else False)
        or customer.bot_paused
        or conversation.bot_paused
    )
    if paused or conversation.status == "escalated":
        motivo_bloqueo = "escalated_conversation" if conversation.status == "escalated" else "kill_switch"
        await bloquear(conversation, motivo_bloqueo, status="silent")
        return True

    # ---------------- escalamiento por keywords
    escalation_cfg = dict(app_settings.escalation_config or {}) if app_settings else {}
    keywords = list(escalation_cfg.get("keywords", []))
    if matches_escalation_keywords(mensaje, keywords or None):
        # La decisión va antes que cualquier efecto (el `turn_id` de la auditoría sale de ella).
        # Después, primero el escalamiento: el reintento del consumer no lo rehace (el inbound ya
        # está guardado y lo vería como duplicado), así que nada opcional puede ir antes.
        await bloquear(conversation, "escalation_keyword", status="escalated")
        await _escalar_por_palabra(conversation.id, mensaje[:200])
        # El texto es opcional: si el spec no carga (p. ej. BD caída), va el fijo.
        # `load_active_spec()` sin tenant explícito resuelve el de la
        # conversación en curso vía el `tenant_scope` que el consumer ya abrió.
        try:
            spec = await load_active_spec()
            escalation_text = await load_response(spec, "escalation") or ESCALATION_REPLY
        except Exception as exc:
            log.warning(
                "escalation_text_fallback",
                conversation_id=str(conversation.id),
                error=type(exc).__name__,
            )
            escalation_text = ESCALATION_REPLY
        try:
            await send_to_conversation(conversation.id, escalation_text)
        except Exception as exc:
            # Ya está escalada: una persona la retoma aunque no salga el aviso al cliente.
            log.warning(
                "escalation_reply_send_failed",
                conversation_id=str(conversation.id),
                error=type(exc).__name__,
            )
        return True

    # ---------------- horario de atención (ADR-0051)
    # Fuera de horario: se responde el mensaje configurado y NO se procesa el turno
    # con el LLM (ahorra costo y evita prometer atención que no hay). Sin config,
    # `within_business_hours` devuelve True (fail-open).
    business_hours = escalation_cfg.get("business_hours")
    if business_hours and not within_business_hours(business_hours):
        out_msg = str(escalation_cfg.get("out_of_hours_message") or "").strip()
        if out_msg:
            await bloquear(conversation, "out_of_hours", status="replied")
            texto = out_msg
            for key, value in (
                ("{assistant_name}", app_settings.assistant_name if app_settings else ""),
                ("{business_description}", app_settings.business_description if app_settings else ""),
            ):
                texto = texto.replace(key, value or "")
        else:
            # Sin mensaje propio (adenda 2026-10-18): aviso por defecto del playbook del kernel,
            # una vez por conversación y ventana; los mensajes que siguen quedan en silencio.
            if not await primer_aviso(conversation.tenant_id, conversation.id):
                await bloquear(conversation, "out_of_hours", status="silent")
                return True
            try:
                await bloquear(conversation, "out_of_hours", status="replied")
                texto = texto_por_defecto(
                    app_settings.language if app_settings else None,
                    app_settings.assistant_name if app_settings else None,
                )
            except Exception:
                # El reintento del consumer no debe encontrar la llave tomada y callar.
                await soltar_aviso(conversation.tenant_id, conversation.id)
                raise
        try:
            delivered = await send_to_conversation(conversation.id, texto)
        except Exception as exc:
            log.warning(
                "out_of_hours_send_failed",
                conversation_id=str(conversation.id),
                error=type(exc).__name__,
            )
            delivered = False
        if not delivered:
            # Mismo criterio que el camino inmediato: si el envío falla (devuelve False
            # o lanza), se escala para que una persona retome (el inbound ya está
            # guardado: el reintento del consumer no rehace el turno).
            await escalate(conversation.id, reason="send_failed", summary=texto[:200])
            marcar_escalado()
        return True
    return False


# Pausa antes de reintentar el escalamiento: deja pasar un corte breve de la BD (failover, pool).
_ESPERA_REINTENTO_ESCALAR_S = 0.5


async def _escalar_por_palabra(conversation_id: Any, resumen: str) -> None:
    """Escala con un reintento: un fallo pasajero de la BD no deja al cliente sin persona.
    Si el reintento también falla, se avisa a los dueños por push (no pasa por la BD), el error
    sube (turno `failed`, `escalation_keyword_failed` en el log) y no se le responde nada al
    cliente: no se promete una persona que no viene."""
    # Import en la llamada: los tests parchean `core.orchestrator.escalation.escalate`.
    from core.orchestrator.escalation import escalate

    try:
        await escalate(conversation_id, reason="keyword", summary=resumen)
        return
    except Exception as exc:
        # Solo el tipo: el texto de un error de SQLAlchemy trae sus parámetros (el mensaje
        # del cliente va en `summary`).
        log.warning(
            "escalation_keyword_retry", conversation_id=str(conversation_id), error=type(exc).__name__
        )
    await asyncio.sleep(_ESPERA_REINTENTO_ESCALAR_S)
    try:
        await escalate(conversation_id, reason="keyword", summary=resumen)
    except Exception as exc:
        log.error(
            "escalation_keyword_failed", conversation_id=str(conversation_id), error=type(exc).__name__
        )
        await _avisar_escalamiento_fallido(conversation_id)
        raise


async def _avisar_escalamiento_fallido(conversation_id: Any) -> None:
    """Aviso best-effort al equipo: el reintento del consumer no rehace el turno, así que sin
    este aviso nadie se entera de que un cliente pidió una persona (centro de notificaciones,
    ADR-0167; nunca propaga)."""
    tenant = get_current_tenant_id()
    if tenant is None:
        return
    from core.notifications.catalog import escalacion_fallida
    from core.notifications.service import notify_standalone

    await notify_standalone(tenant, escalacion_fallida(conversation_id))
