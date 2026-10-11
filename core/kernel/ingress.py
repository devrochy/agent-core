"""Políticas deterministas del turno antes de gastar LLM (HU-AOS-10, design §3.5).

Todas las políticas de entrada viven aquí, con el mismo orden, las mismas lecturas y los mismos
textos que antes de mudarlas. Cada bloqueo deja su decisión en `core_decisions`
(`core.kernel.decisions`).

- **Al entrar el mensaje** (`politicas_de_entrada`, la llama el consumer después de guardar el
  inbound): conexión de Meta pausada o eliminada (ADR-0122), kill-switch en tres niveles o
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

from dataclasses import dataclass
from typing import Any

from core.agents.responses import load_response
from core.kernel.decisions import bloquear, marcar_escalado
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
    """Conexión de Meta pausada o eliminada (ADR-0122): el mensaje ya quedó guardado y no corre
    la IA. `avisar_duenos` manda el push a los dueños (una vez al día por conexión); al
    disparar un turno diferido no se avisa otra vez. `evento` es el nombre del log."""
    # Imports en la llamada: los tests parchean `core.gateway.meta_connection_state`.
    from core.gateway.meta_connection_state import (
        avisar_mensaje_en_conexion_pausada,
        cuenta_meta_de_conversacion,
        motivo_pausa_de,
    )

    cuenta = await cuenta_meta_de_conversacion(conversation)
    if cuenta is None or (motivo := await motivo_pausa_de(cuenta)) is None:
        return False
    log.info(evento, conversation_id=str(conversation.id), motivo=motivo)
    await bloquear(conversation, "connection_paused", status="silent")
    if avisar_duenos:
        await avisar_mensaje_en_conexion_pausada(conversation.tenant_id, cuenta, motivo)
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
        await bloquear(conversation, "escalation_keyword", status="escalated")
        # Resolver el texto ANTES de escalar (auditor-multitenant): si
        # load_active_spec falla (ej. BD caída), no queremos dejar la
        # conversación ya marcada `escalated`/`bot_paused` sin haber podido
        # avisarle nada al cliente -- y el claim de idempotencia de este
        # evento ya se insertó en el consumer, así que un reintento
        # posterior lo vería como duplicado y no volvería a intentar nada.
        # `load_active_spec()` sin tenant explícito resuelve el de la
        # conversación en curso vía el `tenant_scope` que el consumer ya abrió.
        spec = await load_active_spec()
        escalation_text = await load_response(spec, "escalation") or ESCALATION_REPLY
        await escalate(conversation.id, reason="keyword", summary=mensaje[:200])
        await send_to_conversation(conversation.id, escalation_text)
        return True

    # ---------------- horario de atención (ADR-0051)
    # Fuera de horario: se responde el mensaje configurado y NO se procesa el turno
    # con el LLM (ahorra costo y evita prometer atención que no hay). Sin config,
    # `within_business_hours` devuelve True (fail-open).
    business_hours = escalation_cfg.get("business_hours")
    if business_hours and not within_business_hours(business_hours):
        out_msg = str(escalation_cfg.get("out_of_hours_message") or "").strip()
        await bloquear(conversation, "out_of_hours", status="replied" if out_msg else "silent")
        if out_msg:
            texto = out_msg
            for key, value in (
                ("{assistant_name}", app_settings.assistant_name if app_settings else ""),
                ("{business_description}", app_settings.business_description if app_settings else ""),
            ):
                texto = texto.replace(key, value or "")
            delivered = await send_to_conversation(conversation.id, texto)
            if not delivered:
                # Mismo criterio que el camino inmediato: si el envío falla, se
                # escala para que una persona retome (el claim de idempotencia ya
                # se quemó, no habrá reintento).
                await escalate(conversation.id, reason="send_failed", summary=texto[:200])
                marcar_escalado()
        return True
    return False
