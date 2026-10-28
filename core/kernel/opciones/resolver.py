"""Resolver del toque de una opción directa (SPEC-40, ADR-0172 §3-§5, HU-OPC-03): el cliente toca un botón
`opt:`/`optc:`/`optn:` y el servidor ejecuta una tool y contesta con un texto fijo, SIN LLM.

Lo atiende el consumer (`core/orchestrator/consumer.py`) cuando la bandera `opciones_directas` del negocio
está encendida; este módulo es el flujo y sus reglas, y recibe del orquestador solo lo que el kernel no puede
importar (`Bordes`): el entorno de la tool (release, spec, binding, ejecución) y el envío de mensajes. Así las
pruebas unitarias simulan únicamente el borde.

Flujo de un `opt:` (el `optc:` y el `optn:` son los mismos pasos con la confirmación en medio):

1. **Firma y pertenencia.** El payload debe estar bien formado, la firma HMAC debe corresponder a ESTE
   negocio y ESTA conversación, y el envío debe existir para este negocio y ser de esta conversación. Si no,
   se DESCARTA: log `warning`, métrica y ninguna respuesta (quien arma un webhook no recibe señales). En
   `optc`/`optn` la firma se ata a los argumentos, que se vuelven a calcular desde la definición y el contexto
   del envío (inmutables): el cliente confirma exactamente lo que el servidor va a ejecutar.
2. **Vigencia.** Envío vencido o versión/plantilla retirada → texto fijo del sistema. Un botón viejo ejecuta SU
   versión mientras no venza.
3. **Una sola ejecución.** `dedupe_key` única (`<envío>` si el envío es exclusivo; `<envío>:<opción>` si no) más
   `INSERT … ON CONFLICT DO NOTHING`: el doble toque, el reintento de Meta y otra opción de un envío exclusivo
   chocan en la base, no en la memoria de un worker. Quien pierde recibe un texto fijo según el estado de la
   activación que ya existe. Una activación en `running` NUNCA se reejecuta sola (un cobro o una cita doble es
   peor que un «sin confirmar»): reciente (< 60 s) → «en curso»; más vieja → «sin confirmar el resultado».
4. **Autorización al ejecutar, fail-closed** (otra vez, aunque el lint ya la aplicó al guardar): tool apta,
   pack activo, binding EXPLÍCITO y habilitado (no vale el fail-open transitorio de `_tool_denial`), no
   gobernada por un procedimiento (`lint.motivos_de_rechazo`, la misma regla), más lo que ya exige el loop
   (salud del pack, conexiones, scopes, tope de llamadas). Argumentos solo de la definición y del contexto del
   envío, validados contra el schema vigente de la tool: si derivó, `respuesta_error` y log `option_args_drift`.
5. **Confirmación.** Una opción sensible (tool `sensible`, binding con `requiresConfirmation` u opción con
   `confirmar`) crea la activación en `pending_confirm` y envía un resumen de los argumentos exactos con
   «Confirmar» (`optc:`) y «Cancelar» (`optn:`). Solo `optc:` ejecuta; la confirmación vence a los
   `VIGENCIA_CONFIRMACION` minutos; cancelar o vencer libera la opción para volver a tocarla.
6. **Ejecución** sin transacción ni lock abiertos (cada paso de base es una sesión corta) y con
   `idempotency_key = activation_id`. Después, `done`/`failed` y la respuesta fija (`respuesta_ok` o
   `respuesta_error`) por la cola normal. El toque nunca termina en silencio ni en el LLM.
7. **Rastro.** Un log `option_tap` por toque, métricas y `record_audit(actor="option:<clave>",
   action="option_tap")` con ids y claves, sin valores.

**Respuesta de texto (pista C2).** En un canal sin botones el cliente contesta «1» o el título de una opción del menú
numerado (`option_sends.via = 'text'`). El consumer (`orchestrator/opciones_toque.py`) comprueba las guardas (menú último
saliente y vigente, coincidencia exacta, canal con `respuesta_numerica`, bandera) y entrega a ESTE flujo un `opt:` que
firma el servidor, con `Toque.via = "text"`: de ahí en adelante todo es igual (firma, dedupe, autorización fail-closed,
tool, respuesta fija, rastro con `via=text`), salvo que una acción que pide confirmación no se ejecuta por texto
(`SENSIBLE_POR_TEXTO`: texto fijo, sin activación ni tool; la confirmación numerada queda fuera de v1).

Decisiones que el ADR deja abiertas, tomadas aquí: ver `docs/adr/0172-…` (enmienda) y la descripción del PR.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from core.db.models import utcnow
from core.kernel import confirmacion
from core.kernel.opciones import activaciones, argumentos, lint, metricas, payload
from core.kernel.opciones.activaciones import Activacion, Envio
from core.kernel.opciones.argumentos import ArgumentosInvalidos
from core.kernel.opciones.definicion import Definicion, OpcionDeLaPlantilla
from core.kernel.opciones.definicion import leer as leer_definicion
from core.kernel.opciones.lint import FichaTool
from core.kernel.opciones.payload import PayloadLeido
from core.kernel.opciones.textos import texto
from core.shared.audit import record_audit
from core.shared.logger import get_logger

log = get_logger(__name__)

VIGENCIA_CONFIRMACION = timedelta(minutes=15)
UMBRAL_EN_CURSO = timedelta(seconds=60)

# Resultados de un toque (etiqueta `outcome` de la métrica y del log): conjunto cerrado.
OK = "ok"
TOOL_ERROR = "tool_error"
DENEGADA = "denied"
DERIVA = "args_drift"
CONFIRMACION_PEDIDA = "confirm_requested"
CANCELADA = "cancelled"
VENCIDA = "expired"
RETIRADA = "retired"
DUPLICADO = "duplicate"
EN_CURSO = "in_progress"
SIN_CONFIRMAR = "unconfirmed"
FIRMA_INVALIDA = "bad_signature"
ENVIO_DESCONOCIDO = "unknown_send"
CONVERSACION_AJENA = "foreign_conversation"
ERROR_INTERNO = "internal_error"
SENSIBLE_POR_TEXTO = "sensitive_via_text"
_DESCARTADOS = frozenset({FIRMA_INVALIDA, ENVIO_DESCONOCIDO, CONVERSACION_AJENA})
_CODIGO = re.compile(r"^[a-z0-9_]{1,60}$")


# Cómo llegó el toque (log y auditoría `via`): el botón de un canal con botones, o el texto que un cliente escribió
# contestando un menú numerado (pista C2): el consumer lo traduce a un `opt:` que firma el SERVIDOR.
VIA_BOTON = "button"
VIA_TEXTO = "text"


@dataclass(frozen=True)
class Toque:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    payload: str
    wamid: str | None = None
    via: str = VIA_BOTON


class Entorno(Protocol):
    """Lo que solo el orquestador sabe: la release y el spec del turno, el binding y la ejecución de la tool."""

    async def ficha(self, tool: str) -> FichaTool | None: ...

    async def denegacion(self, tool: str) -> str | None: ...

    async def ejecutar(self, tool: str, args: dict[str, Any], activation_id: uuid.UUID) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Bordes:
    abrir_entorno: Callable[[], AbstractAsyncContextManager[Entorno]]
    enviar_texto: Callable[[str], Awaitable[bool]]
    # El mensaje interactivo con «Confirmar» y «Cancelar». False si el canal no lo puede mostrar.
    enviar_confirmacion: Callable[[dict[str, Any]], Awaitable[bool]]
    secreto: str | None = None
    ahora: Callable[[], datetime] = utcnow


@dataclass
class _Traza:
    """Lo que se sabe del toque mientras avanza; se emite UNA vez (log, métrica, auditoría)."""

    toque: Toque
    outcome: str = ERROR_INTERNO
    tipo: str = ""
    envio: Envio | None = None
    version_n: int | None = None
    option_id: str = ""
    tool: str = ""
    activation_id: uuid.UUID | None = None
    codigo: str = ""
    claves_resultado: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Plan:
    tool: str
    args: dict[str, Any]
    args_hash: str
    confirmar: bool
    parameters: dict[str, Any]


@dataclass(frozen=True)
class _Rechazo:
    outcome: str
    codigo: str


# ------------------------------------------------------------------ entrada
async def atender_toque(toque: Toque, bordes: Bordes) -> str:
    """Resuelve el toque y devuelve el `outcome`. Nunca lanza: ante un fallo inesperado contesta con el
    texto genérico (el cliente no queda en silencio y el toque no pasa al LLM) y deja la activación como
    estaba (`running` = «sin confirmar»: no se reejecuta)."""
    inicio = time.monotonic()
    traza = _Traza(toque)
    try:
        traza.outcome = await _atender(toque, bordes, traza)
    except Exception as exc:
        traza.outcome = ERROR_INTERNO
        traza.codigo = type(exc).__name__
        log.error("option_tap_internal_error", tenant_id=str(toque.tenant_id), error=type(exc).__name__)
        await _decir(bordes, texto("error_generico"))
    await _emitir(traza, time.monotonic() - inicio)
    return traza.outcome


async def _atender(toque: Toque, bordes: Bordes, t: _Traza) -> str:
    secreto = bordes.secreto or payload.secreto_del_servidor()
    leido = payload.leer(toque.payload)
    if leido is None:
        return _descartar(t, FIRMA_INVALIDA, "forma")
    t.tipo, t.option_id = leido.tipo, leido.option_id
    if leido.tipo == "opt" and not payload.firma_valida(leido, secreto, toque.tenant_id, toque.conversation_id):
        return _descartar(t, FIRMA_INVALIDA, "firma")
    envio = await activaciones.cargar_envio(toque.tenant_id, leido.send_id)
    if envio is None:
        return _descartar(t, ENVIO_DESCONOCIDO, "envio")
    if envio.conversation_id != toque.conversation_id:
        return _descartar(t, CONVERSACION_AJENA, "conversacion")
    t.envio, t.version_n = envio, envio.version_n
    try:
        definicion = leer_definicion(envio.definition)
    except ValidationError:
        # Sin definición ni opción no hay con qué verificar la firma de un `optc`/`optn` (ata los
        # argumentos): solo un `opt` ya firmado merece respuesta.
        if leido.tipo != "opt":
            return _descartar(t, FIRMA_INVALIDA, "definicion")
        return await _fallo_sin_opcion(bordes, t, "definicion_invalida")
    opcion = next((o for o in definicion.opciones if o.id == leido.option_id), None)
    if opcion is None:
        if leido.tipo != "opt":
            return _descartar(t, FIRMA_INVALIDA, "opcion")
        return await _fallo_sin_opcion(bordes, t, "opcion_inexistente")
    t.tool = opcion.accion.tool

    args_hash = ""
    if leido.tipo != "opt":
        # Los argumentos que el cliente confirma se recalculan del envío (inmutable): la firma los ata.
        try:
            args = argumentos.resolver(opcion.accion, envio.context)
        except ArgumentosInvalidos:
            return _descartar(t, FIRMA_INVALIDA, "argumentos")
        args_hash = payload.hash_de_argumentos(opcion.accion.tool, args)
        if not payload.firma_valida(leido, secreto, toque.tenant_id, toque.conversation_id, args_hash):
            return _descartar(t, FIRMA_INVALIDA, "firma")

    ahora = bordes.ahora()
    if ahora >= envio.expires_at:
        if leido.tipo != "opt":
            await _cerrar_pendiente(toque, envio.send_id, leido.option_id, VENCIDA, ahora)
        return await _responder_sistema(bordes, VENCIDA, "envio_vencido")
    if envio.version_retirada or envio.template_retirada:
        return await _responder_sistema(bordes, RETIRADA, "opcion_retirada")

    if leido.tipo == "optn":
        return await _cancelar(toque, leido, definicion, opcion, envio, bordes, ahora, t)
    if leido.tipo == "optc":
        return await _confirmar(toque, leido, definicion, opcion, envio, args_hash, bordes, ahora, t)
    return await _tocar(toque, leido, definicion, opcion, envio, bordes, ahora, t)


# ------------------------------------------------------------------ los tres payloads
async def _tocar(
    toque: Toque, leido: PayloadLeido, definicion: Definicion, opcion: OpcionDeLaPlantilla, envio: Envio, bordes: Bordes,
    ahora: datetime, t: _Traza,
) -> str:
    clave = _dedupe_key(definicion, envio, opcion)
    previa = await activaciones.por_dedupe(toque.tenant_id, clave)
    if previa is not None and _confirmacion_vencida(previa, ahora):
        # La confirmación que quedó sin contestar venció: se libera y este toque abre una nueva.
        await _cerrar_pendiente(toque, envio.send_id, previa.option_id, VENCIDA, ahora, solo_vencidas=True)
        previa = None
    if previa is not None:
        return await _segun_estado(previa, opcion, definicion, envio, bordes, ahora, t, reenviar=toque)

    async with bordes.abrir_entorno() as entorno:
        plan = await _planear(entorno, opcion, envio, t)
        if isinstance(plan, _Rechazo):
            return await _responder_error(bordes, t, opcion, envio, plan)
        if plan.confirmar and toque.via == VIA_TEXTO:
            # v1: una acción que pide confirmación NO se ejecuta contestando con un número (no hay botón «Confirmar»
            # firmado ni resumen que el cliente toque). Sin activación: el menú sigue disponible para otra opción.
            t.codigo = "sensible_por_texto"
            return await _responder_sistema(bordes, SENSIBLE_POR_TEXTO, "sensible_por_texto")
        activation_id = uuid.uuid4()
        t.activation_id = activation_id
        if plan.confirmar:
            return await _pedir_confirmacion(toque, definicion, opcion, envio, plan, activation_id, clave, bordes, ahora, t)
        ganado = await activaciones.insertar(
            toque.tenant_id, id=activation_id, send_id=envio.send_id, option_id=opcion.id, state="running",
            dedupe_key=clave, wamid=toque.wamid, args_hash=plan.args_hash, ahora=ahora,
        )
        if not ganado:
            t.activation_id = None
            existente = await activaciones.por_dedupe(toque.tenant_id, clave)
            return await _segun_estado(existente, opcion, definicion, envio, bordes, ahora, t, reenviar=None)
        return await _ejecutar(toque, entorno, plan, opcion, envio, activation_id, bordes, t)


async def _confirmar(
    toque: Toque, leido: PayloadLeido, definicion: Definicion, opcion: OpcionDeLaPlantilla, envio: Envio, args_hash: str,
    bordes: Bordes, ahora: datetime, t: _Traza,
) -> str:
    async with bordes.abrir_entorno() as entorno:
        plan = await _planear(entorno, opcion, envio, t)
        if isinstance(plan, _Rechazo):
            # Lo confirmado ya no es ejecutable (la tool dejó de ser elegible o derivó): no se ejecuta, se
            # libera la opción y el cliente recibe la respuesta de error.
            cerrada = await activaciones.cerrar_pendiente(
                toque.tenant_id, envio.send_id, opcion.id, "cancelled", ahora=ahora, error_code=plan.codigo
            )
            if cerrada is None:
                # No había nada pendiente (un `optc` repetido tras ejecutarse, o ya cerrado): responde según
                # lo que pasó, no con el error de una acción que sí se hizo.
                previa = await activaciones.ultima_de(toque.tenant_id, envio.send_id, opcion.id)
                if previa is None:
                    return await _responder_sistema(bordes, VENCIDA, "confirmacion_vencida")
                return await _segun_estado(previa, opcion, definicion, envio, bordes, ahora, t, reenviar=None)
            t.activation_id = cerrada
            return await _responder_error(bordes, t, opcion, envio, plan)
        if plan.args_hash != args_hash:  # no debería pasar: ambos salen del mismo envío inmutable
            return _descartar(t, FIRMA_INVALIDA, "argumentos")
        activation_id = await activaciones.reclamar_confirmada(
            toque.tenant_id, envio.send_id, opcion.id, args_hash,
            creada_desde=ahora - VIGENCIA_CONFIRMACION, ahora=ahora,
        )
        if activation_id is None:
            previa = await activaciones.ultima_de(toque.tenant_id, envio.send_id, opcion.id)
            if previa is not None and previa.state == "pending_confirm":
                await _cerrar_pendiente(toque, envio.send_id, previa.option_id, VENCIDA, ahora, solo_vencidas=True)
                return await _responder_sistema(bordes, VENCIDA, "confirmacion_vencida")
            if previa is None:
                return await _responder_sistema(bordes, VENCIDA, "confirmacion_vencida")
            return await _segun_estado(previa, opcion, definicion, envio, bordes, ahora, t, reenviar=None)
        t.activation_id = activation_id
        return await _ejecutar(toque, entorno, plan, opcion, envio, activation_id, bordes, t)


async def _cancelar(
    toque: Toque, leido: PayloadLeido, definicion: Definicion, opcion: OpcionDeLaPlantilla, envio: Envio, bordes: Bordes,
    ahora: datetime, t: _Traza,
) -> str:
    cancelada = await activaciones.cerrar_pendiente(
        toque.tenant_id, envio.send_id, leido.option_id, "cancelled", ahora=ahora
    )
    if cancelada is not None:
        t.activation_id = cancelada
        return await _responder_sistema(bordes, CANCELADA, "cancelado")
    previa = await activaciones.ultima_de(toque.tenant_id, envio.send_id, leido.option_id)
    if previa is None:
        return await _responder_sistema(bordes, VENCIDA, "confirmacion_vencida")
    return await _segun_estado(previa, opcion, definicion, envio, bordes, ahora, t, reenviar=None)


# ------------------------------------------------------------------ autorización y argumentos
async def _planear(entorno: Entorno, opcion: OpcionDeLaPlantilla, envio: Envio, t: _Traza) -> _Plan | _Rechazo:
    """Autorización fail-closed y argumentos válidos de ESTE momento. Nada se ejecuta aquí."""
    accion = opcion.accion
    try:
        args = argumentos.resolver(accion, envio.context)
    except ArgumentosInvalidos as exc:
        log.warning("option_args_drift", tenant_id=str(t.toque.tenant_id), send_id=str(envio.send_id),
                    option_id=opcion.id, tool=accion.tool, motivo=exc.codigo, rutas=exc.rutas[:5])
        return _Rechazo(DERIVA, exc.codigo)
    ficha = await entorno.ficha(accion.tool)
    if ficha is None:
        return _Rechazo(DENEGADA, "tool_inexistente")
    motivos = lint.motivos_de_rechazo(accion.tool, ficha)
    if motivos:
        return _Rechazo(DENEGADA, ",".join(c for c, _ in motivos)[:120])
    if await entorno.denegacion(accion.tool) is not None:
        return _Rechazo(DENEGADA, "tool_denegada")
    try:
        argumentos.validar(args, ficha.parameters)
    except ArgumentosInvalidos as exc:
        log.warning("option_args_drift", tenant_id=str(t.toque.tenant_id), send_id=str(envio.send_id),
                    option_id=opcion.id, tool=accion.tool, motivo=exc.codigo, rutas=exc.rutas[:5])
        return _Rechazo(DERIVA, exc.codigo)
    return _Plan(
        tool=accion.tool,
        args=args,
        args_hash=payload.hash_de_argumentos(accion.tool, args),
        confirmar=lint.confirmacion_efectiva(accion, ficha),
        parameters=dict(ficha.parameters),
    )


# ------------------------------------------------------------------ confirmación
async def _pedir_confirmacion(
    toque: Toque, definicion: Definicion, opcion: OpcionDeLaPlantilla, envio: Envio, plan: _Plan, activation_id: uuid.UUID,
    clave: str, bordes: Bordes, ahora: datetime, t: _Traza,
) -> str:
    secreto = bordes.secreto or payload.secreto_del_servidor()
    try:
        interactivo = _mensaje_de_confirmacion(secreto, toque, envio, opcion, plan)
    except confirmacion.ResumenInvalido:
        # No hay un resumen fiel de lo que se ejecutaría: no se pide confirmar a ciegas ni se ejecuta.
        log.warning("option_confirmacion_sin_resumen", tenant_id=str(toque.tenant_id), send_id=str(envio.send_id),
                    option_id=opcion.id, tool=opcion.accion.tool)
        t.codigo = "resumen_invalido"
        return await _responder_error(bordes, t, opcion, envio, _Rechazo(DENEGADA, "resumen_invalido"))
    ganado = await activaciones.insertar(
        toque.tenant_id, id=activation_id, send_id=envio.send_id, option_id=opcion.id, state="pending_confirm",
        dedupe_key=clave, wamid=toque.wamid, args_hash=plan.args_hash, ahora=ahora,
    )
    if not ganado:
        t.activation_id = None
        existente = await activaciones.por_dedupe(toque.tenant_id, clave)
        return await _segun_estado(existente, opcion, definicion, envio, bordes, ahora, t, reenviar=None)
    if not await bordes.enviar_confirmacion(interactivo):
        # Sin botones no hay confirmación posible: la pendiente no queda viva reteniendo la opción (en un
        # envío exclusivo, todo el envío) hasta que venza.
        await _cerrar_pendiente(toque, envio.send_id, opcion.id, CANCELADA, ahora, codigo="confirmacion_no_disponible")
        await _decir(bordes, texto("confirmacion_no_disponible"))
        t.codigo = "confirmacion_no_disponible"
    return CONFIRMACION_PEDIDA


def _mensaje_de_confirmacion(
    secreto: str, toque: Toque, envio: Envio, opcion: OpcionDeLaPlantilla, plan: _Plan
) -> dict[str, Any]:
    """El resumen de lo que se va a ejecutar (los argumentos EXACTOS, ya saneados por el kernel) con los
    botones firmados `optc:`/`optn:`. `ResumenInvalido` si no cabe o un valor no se puede mostrar entero."""
    confirmar = payload.construir(
        secreto, "optc", toque.tenant_id, toque.conversation_id, envio.send_id, opcion.id, plan.args_hash
    )
    cancelar = payload.construir(
        secreto, "optn", toque.tenant_id, toque.conversation_id, envio.send_id, opcion.id, plan.args_hash
    )
    declaradas = plan.parameters.get("properties")
    propiedades = list(declaradas) if isinstance(declaradas, dict) else []
    resumen = confirmacion.armar(
        opcion.titulo,
        confirmacion.datos_de_args(plan.args, propiedades),
        confirmar,
        completo=True,
        instrucciones=(texto("instruccion_boton"), texto("instruccion_texto")),
    )
    return {
        "type": "button",
        "body": resumen.cuerpo,
        "buttons": [
            {"id": confirmar, "title": texto("boton_confirmar")},
            {"id": cancelar, "title": texto("boton_cancelar")},
        ],
    }


# ------------------------------------------------------------------ ejecución y respuesta
async def _ejecutar(
    toque: Toque, entorno: Entorno, plan: _Plan, opcion: OpcionDeLaPlantilla, envio: Envio, activation_id: uuid.UUID,
    bordes: Bordes, t: _Traza,
) -> str:
    """La activación YA es nuestra (`running`). Sin transacción abierta: la tool corre sola."""
    resultado: dict[str, Any] | None = None
    codigo = ""
    try:
        resultado = await entorno.ejecutar(plan.tool, plan.args, activation_id)
    except Exception as exc:
        codigo = f"excepcion_{type(exc).__name__}"[:60]
        log.error("option_tool_exception", tenant_id=str(toque.tenant_id), tool=plan.tool, error=type(exc).__name__)
    if codigo == "":
        codigo = _codigo_de_fallo(resultado)
    exito = codigo == ""
    if exito:
        t.claves_resultado = sorted(str(k) for k in (resultado or {}))[:20]
    else:
        metricas.error_de_tool(plan.tool)
    t.codigo = codigo
    try:
        terminada = await activaciones.terminar(
            toque.tenant_id, activation_id, "done" if exito else "failed", ahora=bordes.ahora(), error_code=codigo or None
        )
    except Exception as exc:
        # La tool YA corrió y no se pudo registrar: la fila queda `running` (no se reejecuta) y el cliente
        # no recibe un «no pude» sobre algo que pudo haberse hecho.
        log.error("option_terminar_fallo", tenant_id=str(toque.tenant_id), activation_id=str(activation_id),
                  error=type(exc).__name__)
        t.codigo = "terminar_fallo"
        return await _responder_sistema(bordes, SIN_CONFIRMAR, "sin_confirmar")
    if not terminada:
        # La fila ya no estaba en `running` (otro proceso la tocó): no se pisa; el toque ya ejecutó.
        log.warning("option_activation_not_running", tenant_id=str(toque.tenant_id), activation_id=str(activation_id))
    if exito:
        cuerpo = argumentos.renderizar_respuesta(opcion.respuesta_ok, envio.context, resultado, opcion.result_keys)
        if cuerpo is None:
            log.warning("option_respuesta_incompleta", tenant_id=str(toque.tenant_id), option_id=opcion.id, tool=plan.tool)
            cuerpo = texto("ok_generico")
        await _decir(bordes, cuerpo)
        return OK
    await _decir(bordes, _texto_de_error(opcion, envio))
    return TOOL_ERROR


def _codigo_de_fallo(resultado: Any) -> str:
    """"" si la tool tuvo éxito; si no, un código corto (nunca el texto del error: puede traer datos)."""
    if not isinstance(resultado, dict):
        return "resultado_invalido"
    if resultado.get("status") == "confirmation_required":
        return "confirmacion_requerida"
    error = resultado.get("error")
    if error:
        return error if isinstance(error, str) and _CODIGO.fullmatch(error) else "tool_error"
    if resultado.get("ok") is False:
        return "tool_error"
    return ""


async def _segun_estado(
    previa: Activacion | None, opcion: OpcionDeLaPlantilla, definicion: Definicion, envio: Envio, bordes: Bordes,
    ahora: datetime, t: _Traza, *, reenviar: Toque | None,
) -> str:
    """Respuesta fija a un toque que choca con una activación que ya existe (doble toque, reintento de Meta,
    otra opción de un envío exclusivo). Nada de esto ejecuta."""
    if previa is None:  # la que chocó se liberó entre medio: que el cliente vuelva a tocar
        return await _responder_sistema(bordes, DUPLICADO, "ya_atendido")
    t.activation_id = previa.id
    propia = previa.option_id == opcion.id
    if previa.state == "done":
        # La tabla no guarda resultados: solo se repite una respuesta que no cita `{result.k}`.
        if propia and not argumentos.usa_resultado(opcion.respuesta_ok):
            cuerpo = argumentos.renderizar_respuesta(opcion.respuesta_ok, envio.context)
            if cuerpo is not None:
                await _decir(bordes, cuerpo)
                return DUPLICADO
        return await _responder_sistema(bordes, DUPLICADO, "ya_atendido")
    if previa.state == "failed":
        origen = next((o for o in definicion.opciones if o.id == previa.option_id), opcion)
        await _decir(bordes, _texto_de_error(origen, envio))
        return DUPLICADO
    if previa.state == "running":
        reciente = previa.claimed_at is not None and ahora - previa.claimed_at < UMBRAL_EN_CURSO
        if reciente:
            return await _responder_sistema(bordes, EN_CURSO, "en_curso")
        return await _responder_sistema(bordes, SIN_CONFIRMAR, "sin_confirmar")
    if previa.state == "pending_confirm":
        if propia and reenviar is not None:
            # Volvió a tocar la opción con la confirmación sin contestar: se le pide de nuevo (no ejecuta).
            return await _reenviar_confirmacion(reenviar, opcion, envio, previa, bordes, t)
        return await _responder_sistema(bordes, EN_CURSO, "confirmacion_pendiente")
    return await _responder_sistema(bordes, VENCIDA if previa.state == "expired" else CANCELADA, "confirmacion_vencida")


async def _reenviar_confirmacion(
    toque: Toque, opcion: OpcionDeLaPlantilla, envio: Envio, previa: Activacion, bordes: Bordes, t: _Traza
) -> str:
    async with bordes.abrir_entorno() as entorno:
        plan = await _planear(entorno, opcion, envio, t)
    if isinstance(plan, _Rechazo):
        return await _responder_error(bordes, t, opcion, envio, plan)
    if previa.args_hash != plan.args_hash:
        return await _responder_error(bordes, t, opcion, envio, _Rechazo(DERIVA, "args_distintos"))
    try:
        interactivo = _mensaje_de_confirmacion(bordes.secreto or payload.secreto_del_servidor(), toque, envio, opcion, plan)
    except confirmacion.ResumenInvalido:
        return await _responder_error(bordes, t, opcion, envio, _Rechazo(DENEGADA, "resumen_invalido"))
    if not await bordes.enviar_confirmacion(interactivo):
        await _decir(bordes, texto("confirmacion_no_disponible"))
    return CONFIRMACION_PEDIDA


# ------------------------------------------------------------------ utilidades


def _dedupe_key(definicion: Definicion, envio: Envio, opcion: OpcionDeLaPlantilla) -> str:
    """Exclusivo: UNA activación por envío (la clave no incluye la opción), así el UNIQUE de la base es la
    exclusividad atómica. No exclusivo: una por (envío, opción)."""
    return envio.send_id.hex if definicion.exclusivo else f"{envio.send_id.hex}:{opcion.id}"


def _confirmacion_vencida(previa: Activacion, ahora: datetime) -> bool:
    return previa.state == "pending_confirm" and ahora - previa.created_at >= VIGENCIA_CONFIRMACION


async def _cerrar_pendiente(
    toque: Toque, send_id: uuid.UUID, option_id: str, estado: str, ahora: datetime, *,
    codigo: str | None = None, solo_vencidas: bool = False,
) -> None:
    """Cierra (sin ejecutar) la confirmación pendiente de (envío, opción) y libera la opción.
    `solo_vencidas`: solo si ya pasó su vigencia (dos toques que ven la misma pendiente vencida: el
    segundo no debe cerrar la nueva que abrió el primero)."""
    await activaciones.cerrar_pendiente(
        toque.tenant_id, send_id, option_id, "cancelled" if estado == CANCELADA else "expired",
        ahora=ahora, error_code=codigo,
        creada_antes_de=ahora - VIGENCIA_CONFIRMACION if solo_vencidas else None,
    )


def _texto_de_error(opcion: OpcionDeLaPlantilla, envio: Envio) -> str:
    return argumentos.renderizar_respuesta(opcion.respuesta_error, envio.context) or texto("error_generico")


async def _responder_error(bordes: Bordes, t: _Traza, opcion: OpcionDeLaPlantilla, envio: Envio, rechazo: _Rechazo) -> str:
    t.codigo = rechazo.codigo
    await _decir(bordes, _texto_de_error(opcion, envio))
    return rechazo.outcome


async def _responder_sistema(bordes: Bordes, outcome: str, clave: str) -> str:
    await _decir(bordes, texto(clave))
    return outcome


async def _fallo_sin_opcion(bordes: Bordes, t: _Traza, codigo: str) -> str:
    """Firma válida pero la definición no se lee o no tiene la opción: no debería pasar (las versiones son
    inmutables). Se contesta genérico y se deja rastro."""
    t.codigo = codigo
    log.error("option_tap_definicion", tenant_id=str(t.toque.tenant_id), motivo=codigo)
    await _decir(bordes, texto("error_generico"))
    return ERROR_INTERNO


def _descartar(t: _Traza, outcome: str, motivo: str) -> str:
    """Payload que no valida (forma, firma, envío ajeno o inexistente, conversación ajena): sin respuesta."""
    t.codigo = motivo
    log.warning("option_tap_discarded", tenant_id=str(t.toque.tenant_id), conversation_id=str(t.toque.conversation_id),
                outcome=outcome, motivo=motivo)
    return outcome


async def _decir(bordes: Bordes, cuerpo: str) -> None:
    try:
        await bordes.enviar_texto(cuerpo)
    except Exception as exc:
        log.error("option_tap_reply_failed", error=type(exc).__name__)


async def _emitir(t: _Traza, segundos: float) -> None:
    metricas.toque(t.outcome, segundos)
    envio = t.envio
    log.info(
        "option_tap",
        tenant_id=str(t.toque.tenant_id),
        conversation_id=str(t.toque.conversation_id),
        send_id=str(envio.send_id) if envio else None,
        template_key=envio.template_key if envio else None,
        version=t.version_n,
        option_id=t.option_id or None,
        tool=t.tool or None,
        activation_id=str(t.activation_id) if t.activation_id else None,
        tipo=t.tipo or None,
        via=t.toque.via,
        outcome=t.outcome,
        codigo=t.codigo or None,
        latency_ms=round(segundos * 1000),
    )
    if envio is None or t.outcome in _DESCARTADOS:
        return  # un payload que no valida no ensucia la auditoría del negocio (solo log y métrica)
    await record_audit(
        actor=f"option:{envio.template_key}",
        action="option_tap",
        conversation_id=t.toque.conversation_id,
        input_data={
            "send_id": str(envio.send_id),
            "version": t.version_n,
            "option_id": t.option_id,
            "tipo": t.tipo,
            "via": t.toque.via,
            "tool": t.tool or None,
            "activation_id": str(t.activation_id) if t.activation_id else None,
        },
        output_data={"outcome": t.outcome, "codigo": t.codigo or None, "claves": t.claves_resultado},
    )
