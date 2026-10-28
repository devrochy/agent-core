"""Envío de una plantilla con opciones a una conversación (SPEC-40, ADR-0172 §2-§3, HU-OPC-02).

`sender.send_options_to_conversation` delega aquí. Esta pieza crea el `option_sends` que el resolver del toque
consume (`core/orchestrator/opciones_toque.py`) y encola el mensaje por la cascada de `kernel/opciones/envio.py`:
interactivo con la ventana de 24 h abierta, plantilla de Meta aprobada con la ventana cerrada, texto numerado en
un canal sin botones. Qué se decide y qué sale por cada vía está allí; aquí solo el acceso a la base y a la cola.

Reglas de este módulo:

- **Bandera primero.** Con `opciones_directas` apagada para el negocio no sale nada, no se crea ningún
  `option_sends` ni ningún mensaje: solo queda el log y la auditoría con el motivo (`flag_off`).
- **Solo versión publicada y vigente.** Borrador, retirada, borrada o sin versión: se rechaza.
- **El contexto lo arma el servidor** (`kernel/opciones/contexto.py`): del cliente y del negocio de la propia
  conversación y de los `datos` de quien dispara el envío. Nunca de lo que el LLM pasa a la tool. Ojo:
  `cliente.nombre` es el nombre de perfil del canal (dato del cliente, acotado a 60), no una instrucción.
- **El envío se crea ANTES de encolar** (con el `send_id` ya dentro de cada payload firmado): un toque nunca llega
  a un envío que no existe, y si el alta falla no sale nada. Por eso un fallo POSTERIOR al alta (la cola rechaza
  el mensaje, p. ej. por la pausa por uso de la API) deja una fila `option_sends` sin mensaje. `option_sends` es de
  solo INSERT para `app_runtime` (migración 0112): no se puede marcar vencida ni borrar. La fila es inerte:
  ningún cliente recibió el payload firmado, `expires_at` la acota y el resolver exige la firma del negocio y de la
  conversación. Se deja constancia en el log y en la auditoría (`enqueue_failed`). Se reduce al mínimo con
  las comprobaciones previas (pausa de la conexión, pausa por uso de la API, topes de Meta, plantilla utilizable)
  ANTES del alta. El residuo es una carrera entre el alta y el sender (ventana o pausa que cambian en medio).
- **Idempotencia.** Con `clave_idempotencia` el `send_id` es determinista y el `client_message_id` de cada
  burbuja, `opt_send:<send_id>`: repetir la misma llamada no duplica el mensaje. El interactivo lo respeta desde
  este PR (`send_interactive_to_conversation(client_message_id=…)`). El `send_id` incluye la plantilla y la
  versión: la misma clave con otro contenido es otro envío. Un mensaje `failed` (pausa, sin plantilla) no cuenta
  como enviado: el reintento puede volver a intentarlo.
- **Errores como resultado**, nunca excepciones hacia el agente: `ResultadoEnvio` con un `outcome` cerrado y un
  `motivo` (código). Ni los logs ni la auditoría llevan valores del contexto ni texto del cliente.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import (
    Conversation,
    Customer,
    Message,
    MessageTemplate,
    OptionSend,
    OptionTemplate,
    OptionTemplateVersion,
    WabaAccount,
    utcnow,
)
from core.db.session import get_session
from core.gateway.connection_types import capacidades_de
from core.kernel.opciones import bandera, contexto, mapeo, metricas, payload
from core.kernel.opciones import envio as ev
from core.kernel.opciones.definicion import Definicion
from core.kernel.opciones.definicion import leer as leer_definicion
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

CLAVE = re.compile(r"^[a-z0-9_]{1,40}$")  # misma forma que `ck_option_templates_key`
PREFIJO_ID = "opt_send"
ACCION_AUDITORIA = "option_send"

_DETALLE = {
    ev.SIN_VERSION_EN_META: "esta versión no tiene plantilla de Meta",
    ev.PLANTILLA_NO_APROBADA: "la plantilla de Meta de esta versión no está aprobada",
    ev.PLANTILLA_OTRA_CUENTA: "la plantilla de Meta es de otra cuenta de WhatsApp",
    ev.PLANTILLA_DISTINTA: "la plantilla de Meta no coincide con la versión",
}


@dataclass
class _Traza:
    """Lo que se sabe del envío mientras avanza; se emite UNA vez (log, métrica, auditoría)."""

    tenant_id: uuid.UUID | None
    conversation_id: uuid.UUID
    clave: str
    actor: str
    version_n: int | None = None
    canal: str = ""
    send_id: uuid.UUID | None = None


async def enviar_opciones(
    conversation_id: uuid.UUID,
    clave: str,
    *,
    datos: dict[str, Any] | None = None,
    clave_idempotencia: str | None = None,
    actor: str = "agent",
) -> ev.ResultadoEnvio:
    """Envía la plantilla con opciones `clave` (versión vigente publicada) a la conversación. Corre dentro del
    `tenant_scope` del negocio. Nunca lanza: devuelve un `ResultadoEnvio` con el `outcome` cerrado.

    - `datos`: rutas extra (`cita.fecha`) que arma el código del SERVIDOR que dispara el envío; ver `contexto`.
    - `clave_idempotencia`: repetir la misma llamada (mismo negocio, conversación y clave) no duplica el envío.
    - `actor`: quién lo pide, para la auditoría (`agent`, un usuario, un evento)."""
    traza = _Traza(get_current_tenant_id(), conversation_id, clave, actor)
    try:
        resultado = await _enviar(traza, datos, clave_idempotencia)
    except Exception as exc:
        log.error("option_send_internal_error", tenant_id=str(traza.tenant_id), error=type(exc).__name__)
        resultado = ev.ResultadoEnvio(False, ev.ERROR_INTERNO, send_id=traza.send_id, motivo=type(exc).__name__)
    await _rastro(traza, resultado)
    return resultado


async def _enviar(traza: _Traza, datos: dict[str, Any] | None, clave_idempotencia: str | None) -> ev.ResultadoEnvio:
    from core.gateway.sender import (
        _registrar_si_pausada,
        interactivo_valido,
        resolve_credentials,
        send_interactive_to_conversation,
        send_template_to_conversation,
        send_to_conversation,
    )

    tenant_id = traza.tenant_id
    if tenant_id is None:
        return ev.ResultadoEnvio(False, ev.ERROR_INTERNO, motivo="sin_negocio")
    if not CLAVE.fullmatch(traza.clave or ""):
        return ev.ResultadoEnvio(False, ev.CLAVE_INVALIDA)

    async with get_session() as session:
        if not await bandera.habilitada(session, tenant_id):
            return ev.ResultadoEnvio(False, ev.BANDERA_APAGADA)
        plantilla = (
            await session.execute(
                select(OptionTemplate).where(
                    OptionTemplate.tenant_id == tenant_id, OptionTemplate.key == traza.clave, OptionTemplate.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if plantilla is None:
            return ev.ResultadoEnvio(False, ev.NO_ENCONTRADA)
        if plantilla.status != "published" or plantilla.current_version_n is None:
            return ev.ResultadoEnvio(False, ev.NO_PUBLICADA, motivo=plantilla.status)
        version = (
            await session.execute(
                select(OptionTemplateVersion).where(
                    OptionTemplateVersion.tenant_id == tenant_id,
                    OptionTemplateVersion.option_template_id == plantilla.id,
                    OptionTemplateVersion.version_n == plantilla.current_version_n,
                )
            )
        ).scalar_one_or_none()
        if version is None or version.retired_at is not None:
            return ev.ResultadoEnvio(False, ev.NO_PUBLICADA, motivo="version_retirada")
        traza.version_n = version.version_n
        conversation = await session.get(Conversation, traza.conversation_id)
        if conversation is None or conversation.tenant_id != tenant_id or conversation.deleted_at is not None:
            return ev.ResultadoEnvio(False, ev.CONVERSACION_NO_ENCONTRADA)
        traza.canal = conversation.channel
        customer = await session.get(Customer, conversation.customer_id)
        if customer is None or customer.tenant_id != tenant_id:
            return ev.ResultadoEnvio(False, ev.CONVERSACION_NO_ENCONTRADA)
        negocio = (
            await session.execute(text("SELECT name FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id})
        ).scalar()

    try:
        definicion = leer_definicion(version.definition)
    except ValidationError:
        return ev.ResultadoEnvio(False, ev.NO_PUBLICADA, motivo="definicion_invalida")

    send_id = (
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"opciones-directas/{tenant_id}/{conversation.id}/{plantilla.id}/{version.id}/{clave_idempotencia}",
        )
        if clave_idempotencia
        else uuid.uuid4()
    )
    traza.send_id = send_id
    id_mensaje = f"{PREFIJO_ID}:{send_id}"
    if clave_idempotencia and await _ya_enviado(tenant_id, id_mensaje):
        return ev.ResultadoEnvio(False, ev.DUPLICADO, send_id=send_id)

    try:
        contexto_ = contexto.armar(definicion, {"cliente.nombre": customer.name, "negocio.nombre": negocio}, datos)
    except contexto.ContextoIncompleto as exc:
        return ev.ResultadoEnvio(False, ev.CONTEXTO_INCOMPLETO, send_id=send_id, motivo=",".join(exc.rutas[:5]))
    except contexto.DatosInvalidos:
        # Error de programación de quien dispara el envío, no del cliente: no se envía y queda visible en el log.
        return ev.ResultadoEnvio(False, ev.DATOS_INVALIDOS, send_id=send_id)
    cuerpo = mapeo.renderizar(definicion.body, contexto_)

    # Lo que el cliente va a poder tocar: los payloads de cada opción ya llevan el `send_id` y la firma.
    secreto = payload.secreto_del_servidor()
    try:
        payloads = ev.payloads_de(
            definicion, lambda opcion_id: payload.construir(secreto, "opt", tenant_id, conversation.id, send_id, opcion_id)
        )
    except ValueError:
        return ev.ResultadoEnvio(False, ev.ERROR_INTERNO, send_id=send_id, motivo="payload_invalido")
    interactivo = mapeo.a_interactivo(definicion, cuerpo, payloads)

    if await _registrar_si_pausada(conversation, cuerpo, "agent"):
        return ev.ResultadoEnvio(False, ev.PAUSADA, send_id=send_id)

    ahora = utcnow()
    proveedor, credenciales = "", {}
    if conversation.channel == "whatsapp":
        # Solo WhatsApp tiene varios proveedores con capacidades distintas: ahí hace falta resolverlo (una consulta);
        # en los demás canales `capacidades_de(canal, "")` ya es unívoca.
        proveedor, credenciales = await resolve_credentials(
            conversation.connection_id, conversation.meta_connection_id, expected_tenant_id=conversation.tenant_id
        )
    abierta = ev.ventana_abierta(conversation.service_window_expires_at, ahora)
    contraparte, motivo_plantilla = None, ""
    if capacidades_de(conversation.channel, proveedor).plantilla_aprobada and not abierta:
        contraparte, motivo_plantilla = await _contraparte(conversation, version, definicion)
    decision = ev.decidir_via(
        canal=conversation.channel,
        proveedor=proveedor,
        ventana_abierta=abierta,
        interactivo_valido=interactivo_valido(interactivo),
        plantilla_utilizable=contraparte is not None,
        motivo_plantilla=motivo_plantilla,
    )
    if decision.via is None:
        # WhatsApp con la ventana cerrada y sin plantilla utilizable: un texto libre lo rechaza Meta (131047).
        # Queda un mensaje `failed` visible en el panel (como los avisos por evento) y ninguna opción sale.
        from core.gateway.event_notices import _registrar_sin_plantilla

        # Id propio por intento: el estable `opt_send:<send_id>` es del mensaje que SÍ sale, y el reintento (con la
        # plantilla ya aprobada) tiene que poder usarlo.
        await _registrar_sin_plantilla(
            conversation.id, f"{id_mensaje}:f{uuid.uuid4().hex[:8]}", f"opciones:{traza.clave}",
            _DETALLE.get(decision.motivo, decision.motivo),
        )
        return ev.ResultadoEnvio(False, ev.SIN_PLANTILLA, send_id=send_id, motivo=decision.motivo)

    if proveedor == "meta" and decision.via in (ev.INTERACTIVO, ev.PLANTILLA) and await _pausada_por_uso(tenant_id, credenciales):
        # Pausa por uso de la API de Meta (>= 90 %): el sender no encolaría. Se evita el alta de un envío huérfano.
        return ev.ResultadoEnvio(False, ev.PAUSADA, send_id=send_id, motivo="uso_api_meta")

    try:
        await _crear_envio(
            tenant_id, plantilla.id, version.id, conversation.id, send_id, decision.via, contexto_,
            ev.expira_en(ahora, definicion.ttl_horas),
        )
    except _EnvioPrevioDistinto:
        return ev.ResultadoEnvio(False, ev.ERROR_INTERNO, send_id=send_id, motivo="envio_previo_distinto")

    if decision.via == ev.INTERACTIVO:
        # `sin_degradar`: el alta ya dice «interactive»; si el sender no puede, no cambia de vía por su cuenta.
        encolado = await send_interactive_to_conversation(
            conversation.id, interactivo, fallback_text=mapeo.texto_de_respaldo(definicion, cuerpo),
            sin_degradar=True, client_message_id=id_mensaje,
        )
    elif decision.via == ev.PLANTILLA and contraparte is not None:
        from core.gateway.event_notices import build_components

        componentes = ev.componentes_de_plantilla(
            build_components(contraparte, mapeo.valores_para_meta(definicion, contexto_)),
            [payloads[o.id] for o in definicion.opciones],
        )
        encolado = await send_template_to_conversation(
            conversation.id, template_name=contraparte.name, language=contraparte.language,
            components=componentes, preview_text=cuerpo, client_message_id=id_mensaje,
        )
    else:
        encolado = await send_to_conversation(
            conversation.id, mapeo.texto_de_respaldo(definicion, cuerpo), client_message_id_prefix=id_mensaje
        )
    if not encolado:
        # La fila queda sin mensaje: ver el docstring del módulo (inerte, de solo INSERT).
        return ev.ResultadoEnvio(False, ev.NO_ENCOLADO, via=decision.via, send_id=send_id, motivo="cola_rechazo")
    return ev.ResultadoEnvio(
        True, ev.ENVIADO, via=decision.via, send_id=send_id, motivo=decision.motivo, degradado=decision.via == ev.TEXTO
    )


async def _ya_enviado(tenant_id: uuid.UUID, id_mensaje: str) -> bool:
    """Hay un mensaje de este envío que salió o está en cola. Uno `failed` (conexión pausada, sin plantilla) no
    salió: el reintento puede volver a intentarlo."""
    async with get_session() as session:
        fila = (
            await session.execute(
                select(Message.id).where(
                    Message.tenant_id == tenant_id,
                    Message.client_message_id.in_((id_mensaje, f"{id_mensaje}:0")),
                    Message.status != "failed",
                )
            )
        ).first()
    return fila is not None


async def _pausada_por_uso(tenant_id: uuid.UUID, credenciales: dict[str, Any]) -> bool:
    """La API de Meta de esta cuenta está pausada por uso (>= 90 %). Es la misma comprobación del sender."""
    from core.gateway.meta_usage import connection_id_for_phone_number, is_paused

    meta_connection_id = await connection_id_for_phone_number(credenciales.get("phone_number_id", ""), tenant_id)
    return bool(await is_paused(tenant_id, meta_connection_id))


async def _contraparte(
    conversation: Conversation, version: OptionTemplateVersion, definicion: Definicion
) -> tuple[MessageTemplate | None, str]:
    """La plantilla de Meta de ESTA versión, solo si se puede enviar a esta conversación: existe, está `APPROVED`
    (no pendiente, rechazada ni pausada), pertenece a la WABA de la conexión de la conversación (ADR-0115 §2, el
    mismo criterio que `event_notices.approved_template_for`) y es compatible con la definición. `(None, motivo)`
    si no."""
    from core.gateway.meta_connection_state import cuenta_de_plantillas_de_conversacion

    if version.message_template_id is None:
        return None, ev.SIN_VERSION_EN_META
    cuenta = await cuenta_de_plantillas_de_conversacion(conversation)
    async with get_session() as session:
        mt = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.tenant_id == conversation.tenant_id, MessageTemplate.id == version.message_template_id
                )
            )
        ).scalar_one_or_none()
        if mt is None:
            return None, ev.SIN_VERSION_EN_META
        if mt.status != "APPROVED":
            return None, ev.PLANTILLA_NO_APROBADA
        en_su_cuenta = (
            cuenta is not None
            and (
                await session.execute(
                    select(WabaAccount.id).where(
                        WabaAccount.id == mt.waba_account_id,
                        WabaAccount.tenant_id == conversation.tenant_id,
                        WabaAccount.connection_id == cuenta,
                        WabaAccount.deleted_at.is_(None),
                    )
                )
            ).first()
            is not None
        )
    if not en_su_cuenta:
        return None, ev.PLANTILLA_OTRA_CUENTA
    if ev.compatibilidad_plantilla(mt.components, definicion) is not None:
        return None, ev.PLANTILLA_DISTINTA
    return mt, ""


class _EnvioPrevioDistinto(Exception):
    """Ya existe un `option_sends` con ese id pero con otro contenido: no se reutiliza."""


async def _crear_envio(
    tenant_id: uuid.UUID,
    option_template_id: uuid.UUID,
    version_id: uuid.UUID,
    conversation_id: uuid.UUID,
    send_id: uuid.UUID,
    via: str,
    contexto_: dict[str, str],
    expires_at: Any,
) -> None:
    """Alta del `option_sends` (sesión corta, un commit). `ON CONFLICT DO NOTHING` por si una llamada
    idempotente se repite tras un fallo de la cola: el `send_id` determinista ya existe. Se reutiliza solo si es
    el mismo envío (plantilla, versión, conversación, vía y contexto); si no, `_EnvioPrevioDistinto`."""
    async with get_session() as session:
        resultado = await session.execute(
            pg_insert(OptionSend)
            .values(
                id=send_id, tenant_id=tenant_id, option_template_id=option_template_id, version_id=version_id,
                conversation_id=conversation_id, via=via, context=contexto_, expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=[OptionSend.id])
        )
        if getattr(resultado, "rowcount", 1) != 0:
            return
        previo = (
            await session.execute(select(OptionSend).where(OptionSend.tenant_id == tenant_id, OptionSend.id == send_id))
        ).scalar_one_or_none()
    if (
        previo is None
        or previo.option_template_id != option_template_id
        or previo.version_id != version_id
        or previo.conversation_id != conversation_id
        or previo.via != via
        or previo.context != contexto_
    ):
        raise _EnvioPrevioDistinto


async def _rastro(traza: _Traza, r: ev.ResultadoEnvio) -> None:
    """Un log por envío, la métrica si salió algo y la auditoría con ids y claves, sin valores."""
    via = ev.ETIQUETA_VIA.get(r.via or "", "")
    clave = traza.clave if CLAVE.fullmatch(traza.clave or "") else "invalid"  # texto libre no va al log ni a la auditoría
    campos: dict[str, Any] = {
        "tenant_id": str(traza.tenant_id), "conversation_id": str(traza.conversation_id),
        "send_id": str(r.send_id) if r.send_id else None, "template_key": clave,
        "version": traza.version_n, "canal": traza.canal, "via": via, "outcome": r.outcome, "motivo": r.motivo,
    }
    if r.enviado:
        log.info("option_send", **campos)
        metricas.envio(traza.canal, via)
    else:
        log.warning("option_send", **campos)
        metricas.envio_fallido(traza.canal, r.outcome)
    await record_audit(
        actor=f"{traza.actor}"[:120],
        action=ACCION_AUDITORIA,
        conversation_id=traza.conversation_id,
        input_data={"template_key": clave, "version": traza.version_n, "send_id": str(r.send_id) if r.send_id else None},
        output_data={"outcome": r.outcome, "via": via or None, "motivo": r.motivo or None, "canal": traza.canal},
    )
