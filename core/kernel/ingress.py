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
  pausada o escalada, saldo de créditos (con el colchón de las conversaciones abiertas, ADR-0208) y
  presupuesto agotado.

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
from core.kernel.horario_de_servicio import MODO_INFORMATIVO
from core.orchestrator.escalation import (
    ESCALATION_REPLY,
    matches_escalation_keywords,
    within_business_hours,
)
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# El negocio no tiene saldo de créditos y la conversación es nueva (ADR-0208 §3). El texto real vive en
# core/playbooks/respuestas.md (`## quota_exhausted`); esto es el fallback
# determinístico si el playbook no está disponible (mismo patrón que FALLBACK_REPLY).
QUOTA_EXHAUSTED_REPLY = (
    "Por ahora no puedo seguir atendiendo esta conversación. "
    "Un asesor humano puede continuar la atención."
)
# Se agotó también el colchón de una conversación abierta y pasa a una persona (ADR-0208 §6). El texto
# real vive en core/playbooks/respuestas.md (`## creditos_agotados`); esto es solo el fallback.
CREDITOS_AGOTADOS_REPLY = (
    "En este momento no puedo seguir atendiendo esta conversación. "
    "Ya le avisé a una persona de nuestro equipo para que continúe contigo por este mismo chat."
)
# Resumen fijo de la escalación por créditos: sin contenido del cliente.
RESUMEN_CREDITOS_AGOTADOS = "El negocio agotó el crédito del ciclo y la reserva de las conversaciones abiertas."
# `quota_exhausted` se responde una vez por episodio (ADR-0208 §3). La llave lleva el inicio del episodio,
# así que un episodio nuevo vuelve a avisar; el TTL solo limpia Redis.
AVISO_SIN_CREDITO_TTL_S = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class Bloqueo:
    motivo: str
    # Texto de cierre que se responde (None = el turno no responde nada).
    texto: str | None = None
    # El turno ya escaló a una persona: egress no vuelve a escalar si el envío falla.
    escalado: bool = False


async def conversacion_bloqueada(conversation: Any, customer: Any) -> bool:
    """Sin cliente, con el bot pausado o con la conversación escalada no corre el turno
    (la atiende una persona o el negocio la pausó)."""
    if customer is None:
        return True
    # Interruptor global (HU-ARCH-38, ADR-0200): también frena los turnos diferidos y los que ya estaban en cola.
    from core.shared import kill_switch

    if await kill_switch.global_activo():
        await bloquear(conversation, "kill_switch", status="silent")
        return True
    if conversation.status == "escalated":
        await bloquear(conversation, "escalated_conversation", status="silent")
        return True
    if conversation.bot_paused:
        await bloquear(conversation, "kill_switch", status="silent")
        return True
    return False


async def politicas_del_turno(conversation: Any, spec: Any) -> Bloqueo | None:
    """Saldo de créditos y presupuesto de la conversación, antes de gastar LLM. Corre después de
    `fijar_release_del_turno`: la decisión relee el episodio que esa función abrió."""
    # Crédito del ciclo (D-79, ADR-0208 §2-7): el crédito es el único tope de consumo. El tope de
    # clientes (estructural) se aplica en el alta del consumer.
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        from core.billing.avisos_de_consumo import avisar_consumo
        from core.billing.enforcement import turn_quota_metric

        decision = await turn_quota_metric(tenant_id, conversation.id)
        await avisar_consumo(tenant_id, decision)
        if decision.salida == "colchon":
            log.info("turn_credit_buffer", tenant_id=str(tenant_id), conversation_id=str(conversation.id))
        elif decision.salida == "agotado":
            return await _sin_credito(conversation, spec, decision)

    # Presupuesto agotado (ADR-0015): no se gasta más LLM en esta conversación
    # hasta que un admin suba el presupuesto o lo resetee.
    if conversation.state.get("budget_exhausted"):
        await bloquear(conversation, "budget", status="silent")
        return Bloqueo("budget")
    return None


async def _sin_credito(conversation: Any, spec: Any, decision: Any) -> Bloqueo:
    """Sin saldo y sin colchón, sin LLM. Nada queda guardado en la conversación por el corte (solo la
    decisión del turno): cuando vuelve el saldo, el siguiente mensaje se atiende solo.

    - Conversación abierta (colchón agotado, ADR-0208 §6): pasa a una persona por el camino sin modelo
      del ingreso. Queda escalada hasta que una persona la devuelva, aunque llegue saldo.
    - Conversación nueva (§3): `quota_exhausted` una vez por episodio; los mensajes siguientes, en silencio.
      El playground (canal `test`) va por aquí: ahí `escalate` no escala."""
    from core.shared.test_mode import is_test_channel

    tenant_id = conversation.tenant_id
    log.warning(
        "turn_quota_exceeded",
        tenant_id=str(tenant_id),
        metric="creditos",
        conversation_id=str(conversation.id),
        abierta=decision.abierta,
    )
    if decision.abierta and not is_test_channel(getattr(conversation, "channel", None)):
        # Import en la llamada: los tests parchean `core.orchestrator.escalation.escalate`.
        from core.orchestrator.escalation import escalate

        # La decisión va antes que cualquier efecto (el `turn_id` de la auditoría sale de ella).
        await bloquear(conversation, "quota", status="escalated")
        await escalate(conversation.id, reason="creditos_agotados", summary=RESUMEN_CREDITOS_AGOTADOS)
        marcar_escalado()
        texto = await load_response(spec, "creditos_agotados") or CREDITOS_AGOTADOS_REPLY
        return Bloqueo("quota", texto, escalado=True)

    llave = _llave_sin_credito(tenant_id, conversation.id, decision.episodio)
    if not await _tomar_aviso(llave, conversation.id):
        await bloquear(conversation, "quota", status="silent")
        return Bloqueo("quota")
    try:
        await bloquear(conversation, "quota")
        texto = await load_response(spec, "quota_exhausted") or QUOTA_EXHAUSTED_REPLY
    except Exception:
        # El reintento del consumer no debe encontrar la llave tomada y callar.
        await _soltar_aviso(llave, conversation.id)
        raise
    return Bloqueo("quota", texto)


def _llave_sin_credito(tenant_id: Any, conversation_id: Any, episodio: Any) -> str:
    inicio = int(episodio.timestamp()) if episodio is not None else 0
    return f"creditos:sin_saldo:{tenant_id}:{conversation_id}:{inicio}"


async def _tomar_aviso(llave: str, conversation_id: Any) -> bool:
    """True si toca responder `quota_exhausted` en este episodio (`SET NX EX`, como `primer_aviso` de
    `fuera_de_horario`). Si Redis no responde se responde igual: el mal menor es repetir el aviso."""
    from core.memory.working import get_redis

    try:
        return bool(await get_redis().set(llave, "1", nx=True, ex=AVISO_SIN_CREDITO_TTL_S))
    except Exception as exc:
        log.warning("sin_credito_aviso_sin_redis", conversation_id=str(conversation_id), error=type(exc).__name__)
        return True


async def _soltar_aviso(llave: str, conversation_id: Any) -> None:
    from core.memory.working import get_redis

    try:
        await get_redis().delete(llave)
    except Exception as exc:
        log.warning("sin_credito_soltar_aviso_fallo", conversation_id=str(conversation_id), error=type(exc).__name__)


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


async def horario_del_agente(conversation: Any) -> Any:
    """Horario propio del agente que atiende la conversación (HU-AOS-82), o `None` para el del
    negocio. Antes del ruteo: el dueño vigente si es especialista de la release de la conversación;
    si no, el agente por defecto de la release activa de la conexión. Fail-open: cualquier problema
    (sin release, release vieja, error) cae al horario del negocio."""
    try:
        from sqlalchemy import text

        from core.db.session import get_session
        from core.releases.cargar import cargar_release

        tenant_id = conversation.tenant_id
        async with get_session() as session:
            fila = (
                await session.execute(
                    text(
                        "SELECT c.release_id, c.active_agent_instance_id, cc.active_release_id "
                        "FROM conversations c LEFT JOIN channel_connections cc "
                        "  ON cc.tenant_id = c.tenant_id AND cc.id = c.connection_id "
                        "WHERE c.tenant_id = :t AND c.id = :c"
                    ),
                    {"t": tenant_id, "c": conversation.id},
                )
            ).first()
            if fila is None:
                return None
            release_id = fila[0] or fila[2]
            if release_id is None:
                return None
            release = await cargar_release(session, tenant_id, release_id)
        if release is None:
            return None
        foco = release.foco(fila[1] if fila[0] is not None else None)
        return foco.schedule
    except Exception:
        log.warning("horario_del_agente_fallback", conversation_id=str(getattr(conversation, "id", "")))
        return None


async def politicas_de_entrada(
    conversation: Any, customer: Any, app_settings: Any, mensaje: str
) -> bool:
    """Políticas del mensaje que entra, en orden fijo y sin LLM. `True` = el turno terminó aquí
    (la respuesta fija y el escalamiento, si los hay, ya se hicieron); `False` = sigue al
    despacho. El inbound ya está guardado: el historial queda completo aunque se bloquee."""
    # Imports en la llamada: los tests parchean `core.gateway.sender.send_to_conversation`,
    # `core.orchestrator.escalation.escalate`. El texto de escalamiento sale de `respuestas` (ADR-0206 §4).
    from core.gateway.sender import send_to_conversation
    from core.orchestrator.escalation import escalate

    # ---------------- interruptor global (HU-ARCH-38, ADR-0200): primero de todo, sin LLM ni respuesta
    from core.shared import kill_switch

    if await kill_switch.global_activo():
        await bloquear(conversation, "kill_switch", status="silent")
        return True

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
    # HU-AOS-82: el horario que vale es el del agente que atiende (su dueño vigente o el de la
    # conexión); sin horario propio, el del negocio. El modo y el aviso siguen siendo del negocio.
    from core.agents.horario_agente import con_horario_del_agente

    escalation_cfg = con_horario_del_agente(escalation_cfg, await horario_del_agente(conversation))
    keywords = list(escalation_cfg.get("keywords", []))
    if matches_escalation_keywords(mensaje, keywords or None):
        # La decisión va antes que cualquier efecto (el `turn_id` de la auditoría sale de ella).
        # Después, primero el escalamiento: el reintento del consumer no lo rehace (el inbound ya
        # está guardado y lo vería como duplicado), así que nada opcional puede ir antes.
        await bloquear(conversation, "escalation_keyword", status="escalated")
        await _escalar_por_palabra(conversation.id, mensaje[:200])
        # El texto es opcional: si el playbook no carga, va el fijo. Corre antes de fijar la release: el texto sale de
        # `respuestas` (con el override del negocio del `tenant_scope`), no de `agent_specs` (ADR-0206 §4).
        try:
            from core.agents.turno import SoloRespuestas

            escalation_text = await load_response(SoloRespuestas(), "escalation") or ESCALATION_REPLY
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
    # Con horario informativo (HU-AOS-58) el horario es el del equipo humano: no se corta el turno,
    # el asistente responde 24/7 y lo conoce por el prompt (`core/kernel/horario_de_servicio.py`).
    business_hours = escalation_cfg.get("business_hours")
    if (
        business_hours
        and escalation_cfg.get("business_hours_mode") != MODO_INFORMATIVO
        and not within_business_hours(business_hours)
    ):
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
