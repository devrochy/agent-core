"""Envío de una plantilla con opciones: la decisión de la vía y lo que sale por cada una (SPEC-40, ADR-0172 §2-§3,
HU-OPC-02). La parte con base y cola está en `core/gateway/opciones_envio.py`; aquí solo lo puro.

**Cascada** (`decidir_via`):

0. Playground (canal `test`): interactivo si cabe en los topes de Meta, texto si no; sin ventana ni plantilla. El
   navegador pinta los botones y el toque pasa por el mismo resolver y la misma firma que en WhatsApp.
1. Canal sin botones (web, WhatsApp Web, Messenger, Instagram, correo; o un WhatsApp cuya conexión no es de Meta):
   texto numerado con las opciones y SIN ejecución directa del envío (ni botón ni payload): si el canal declara
   `respuesta_numerica`, la respuesta «1» o el título la resuelve el consumer (pista C2), no este envío.
2. WhatsApp Cloud con la ventana de 24 h ABIERTA (conocida y vigente: desconocida cuenta como cerrada, ADR-0108):
   mensaje interactivo, `button` hasta 3 opciones y `list` de 4 a 10. Si el payload no cabe en los topes de Meta,
   texto.
3. WhatsApp Cloud con la ventana cerrada y la contraparte de Meta de ESTA versión `APPROVED` en la WABA de la
   conversación y compatible con la definición: plantilla con los botones QUICK_REPLY y su payload.
4. WhatsApp Cloud con la ventana cerrada y sin esa plantilla (sin publicar, pendiente, rechazada, pausada, de otra
   cuenta, distinta): no hay nada que enviar. Un texto libre ahí lo rechaza Meta (131047), así que no se intenta:
   se deja un mensaje `failed` con `sin_plantilla` (como los avisos por evento) y ninguna opción se ofrece.

**Payload del QUICK_REPLY — PENDIENTE DE VERIFICAR CON META.** El diseño supone que el payload de un botón de
plantilla se manda AL ENVIAR (`components` con `sub_type: "quick_reply"` y `parameters: [{type: "payload"}]`) y
vuelve en el webhook como `type: "button"`. No está confirmado contra la documentación oficial ni contra el
sandbox (ADR-0172 §3, «corrección al requerimiento»). Toda esa construcción vive en `componentes_con_payload`,
el ÚNICO punto del código que la conoce: si la prueba real muestra otra forma (otro `index`, otro nombre de
campo, o que el payload se fija al crear), se cambia aquí y en los tests que la fijan
(`tests/test_opciones_envio_cascada.py`). Mientras no se verifique, la bandera de las opciones directas se
queda apagada. El test contra Meta (`tests/integration/test_opciones_envio_meta_sandbox.py`) se salta salvo con
`OPCIONES_META_SANDBOX=1`.

Puro: ni base, ni red, ni entorno.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.gateway.connection_types import capacidades_de
from core.kernel.opciones.definicion import MAX_TTL_HORAS, Definicion
from core.kernel.opciones.mapeo import nombre_de_parametro, variables_del_cuerpo

# Vías (valores del CHECK `ck_option_sends_via`) y su etiqueta en logs y métricas.
INTERACTIVO = "interactive"
PLANTILLA = "template"
TEXTO = "text"
ETIQUETA_VIA = {INTERACTIVO: "interactivo", PLANTILLA: "plantilla", TEXTO: "texto"}

# Resultados de un envío (etiqueta `outcome` del log): conjunto cerrado.
ENVIADO = "sent"
BANDERA_APAGADA = "flag_off"
CLAVE_INVALIDA = "invalid_key"
NO_ENCONTRADA = "template_not_found"
CONVERSACION_NO_ENCONTRADA = "conversation_not_found"
NO_PUBLICADA = "not_published"
CONTEXTO_INCOMPLETO = "context_incomplete"
SIN_PLANTILLA = "sin_plantilla"
PAUSADA = "paused"
NO_ENCOLADO = "enqueue_failed"
DATOS_INVALIDOS = "invalid_data"
DUPLICADO = "duplicate"
ERROR_INTERNO = "internal_error"

# Motivos por los que se degrada o no se envía (códigos para el log y el resultado; nunca datos del cliente).
CANAL_SIN_BOTONES = "canal_sin_botones"
FUERA_DE_TOPES = "interactivo_fuera_de_topes"
SIN_VERSION_EN_META = "version_sin_plantilla_de_meta"
PLANTILLA_NO_APROBADA = "plantilla_no_aprobada"
PLANTILLA_OTRA_CUENTA = "plantilla_de_otra_cuenta"
PLANTILLA_DISTINTA = "plantilla_distinta_de_la_version"


@dataclass(frozen=True)
class ResultadoEnvio:
    """Lo que devuelve `send_options_to_conversation`. `via` y `send_id` solo si salió (o se encoló) algo.
    `degradado`: salió el texto de respaldo en vez del interactivo; `ejecucion_directa`: el cliente puede elegir
    una opción y ejecutarla (tocar el botón del interactivo o de la plantilla, o —pista C2— contestar «1»/el título
    de un menú de texto en un canal con `respuesta_numerica`). `por_respuesta`: el menú de texto se contesta así.
    `motivo` es un código, nunca un dato del cliente."""

    enviado: bool
    outcome: str
    via: str | None = None
    send_id: uuid.UUID | None = None
    motivo: str = ""
    degradado: bool = False
    por_respuesta: bool = False

    @property
    def ejecucion_directa(self) -> bool:
        return self.enviado and (self.via in (INTERACTIVO, PLANTILLA) or (self.via == TEXTO and self.por_respuesta))


@dataclass(frozen=True)
class Decision:
    """`via` None = no hay nada que enviar (WhatsApp con la ventana cerrada y sin plantilla utilizable)."""

    via: str | None
    motivo: str = ""


def decidir_via(
    *,
    canal: str,
    proveedor: str,
    ventana_abierta: bool,
    interactivo_valido: bool,
    plantilla_utilizable: bool,
    motivo_plantilla: str = "",
) -> Decision:
    """La vía de un envío. Ver la cascada en el docstring del módulo. Lo que cada canal admite sale de
    `connection_types.capacidades_de` (pista C1), no de comparar el nombre del canal."""
    cap = capacidades_de(canal, proveedor)
    if not cap.botones:
        return Decision(TEXTO, CANAL_SIN_BOTONES)
    if ventana_abierta or not cap.ventana_24h:
        # Sin ventana de 24 h (playground: el navegador pinta los botones) siempre se puede escribir.
        return Decision(INTERACTIVO) if interactivo_valido else Decision(TEXTO, FUERA_DE_TOPES)
    if cap.plantilla_aprobada and plantilla_utilizable:
        return Decision(PLANTILLA)
    return Decision(None, motivo_plantilla or SIN_VERSION_EN_META)


def expira_en(ahora: datetime, ttl_horas: int) -> datetime:
    """`expires_at` del envío: `ttl_horas` de la definición, acotado a 1..30 días. El ADR propone 24 h en el
    interactivo y 7 días en la plantilla como valores por defecto sin el OK del dueño; la definición no distingue
    un valor explícito de su default, así que se usa tal cual para las tres vías."""
    return ahora + timedelta(hours=min(max(int(ttl_horas), 1), MAX_TTL_HORAS))


def ventana_abierta(expira: datetime | None, ahora: datetime) -> bool:
    """Ventana de 24 h conocida y vigente. Desconocida cuenta como cerrada (ADR-0108)."""
    return expira is not None and expira >= ahora


# ------------------------------------------------------------------ payloads y componentes
def payloads_de(definicion: Definicion, construir: Callable[[str], str]) -> dict[str, str]:
    """`option_id → payload firmado`, en el orden de la definición. `construir(option_id)` arma el payload de
    UNA opción (lo cierra quien llama con el secreto, el negocio, la conversación y el envío)."""
    return {o.id: construir(o.id) for o in definicion.opciones}


def componentes_con_payload(payloads: Sequence[str]) -> list[dict[str, Any]]:
    """Componentes `button` de un envío de plantilla: UNO por botón QUICK_REPLY, en el orden de los botones de la
    plantilla, cada uno con el payload firmado de esa opción.

    **Forma supuesta, SIN verificar con Meta** (ver el docstring del módulo): `index` como texto (así lo muestran
    los ejemplos de la Cloud API para los botones de plantilla), `sub_type: "quick_reply"` y un parámetro
    `payload`. Es el único lugar que la construye."""
    return [
        {"type": "button", "sub_type": "quick_reply", "index": str(i), "parameters": [{"type": "payload", "payload": p}]}
        for i, p in enumerate(payloads)
    ]


def componentes_de_plantilla(cuerpo: Sequence[dict[str, Any]], payloads: Sequence[str]) -> list[dict[str, Any]]:
    """Componentes completos del envío: el cuerpo (`body` con sus parámetros) y los botones con su payload."""
    return [*cuerpo, *componentes_con_payload(payloads)]


# ------------------------------------------------------------------ compatibilidad con la contraparte de Meta
def _componente(componentes: Any, tipo: str) -> dict[str, Any] | None:
    for c in componentes or []:
        if isinstance(c, dict) and str(c.get("type", "")).upper() == tipo:
            return c
    return None


def compatibilidad_plantilla(componentes: Any, definicion: Definicion) -> str | None:
    """None si la plantilla de Meta aprobada puede enviar ESTA versión; si no, `PLANTILLA_DISTINTA`.

    Un botón se ejecuta por su POSICIÓN (la opción `i` lleva el payload `i`), así que exigir que los botones de la
    plantilla sean exactamente los de la definición, en el mismo orden, es lo que impide que un toque ejecute una
    acción distinta de la que el botón dice. Lo mismo con las variables del cuerpo: la plantilla pide exactamente
    las que la definición resuelve, y ninguna en cabecera, pie ni botones."""
    from core.gateway.event_notices import template_has_non_body_variables, template_placeholders

    if template_has_non_body_variables(componentes):
        return PLANTILLA_DISTINTA
    esperadas = {nombre_de_parametro(r) for r in variables_del_cuerpo(definicion.body)}
    if set(template_placeholders(componentes)) != esperadas:
        return PLANTILLA_DISTINTA
    botones = (_componente(componentes, "BUTTONS") or {}).get("buttons") or []
    quick = [b for b in botones if isinstance(b, dict) and str(b.get("type", "")).upper() == "QUICK_REPLY"]
    if len(quick) != len(botones) or len(quick) != len(definicion.opciones):
        return PLANTILLA_DISTINTA
    if [str(b.get("text", "")) for b in quick] != [o.titulo for o in definicion.opciones]:
        return PLANTILLA_DISTINTA
    return None


# ------------------------------------------------------------------ resultado para el agente
_MENSAJE_AGENTE = {
    BANDERA_APAGADA: "Las opciones con botones no están habilitadas en este negocio.",
    CLAVE_INVALIDA: "La clave de la plantilla no es válida.",
    NO_ENCONTRADA: "No existe una plantilla de opciones con esa clave.",
    NO_PUBLICADA: "Esa plantilla de opciones no está publicada.",
    CONVERSACION_NO_ENCONTRADA: "No se encontró la conversación.",
    CONTEXTO_INCOMPLETO: "Faltan datos del cliente que la plantilla necesita; no se envió.",
    SIN_PLANTILLA: "El cliente está fuera de la ventana de 24 h y la plantilla no está aprobada por Meta; no se envió.",
    DATOS_INVALIDOS: "No se pudo armar el contexto de la plantilla.",
    PAUSADA: "El envío está pausado en esta conexión; no se envió.",
    NO_ENCOLADO: "No se pudo enviar la plantilla.",
    ERROR_INTERNO: "No se pudo enviar la plantilla.",
}


def para_el_agente(r: ResultadoEnvio) -> dict[str, Any]:
    """Lo que la tool `enviar_plantilla_opciones` devuelve al modelo: el resultado como dato, sin ids ni contexto.
    Los errores de negocio son resultado, no excepción (`new-tool`)."""
    if r.outcome == DUPLICADO:
        return {"status": "already_sent"}
    if r.enviado:
        # El agente corre DENTRO de un turno: su texto final sale después de un menú de texto y lo deja de ser el
        # último saliente, así que el «1» llegaría al agente. Por eso aquí solo cuentan el botón y la plantilla (no
        # `r.ejecucion_directa`, que sí es True para el API `enviar-prueba` de un menú de texto contestable, pista C2).
        directa = r.enviado and r.via in (INTERACTIVO, PLANTILLA)
        return {"status": "sent", "via": ETIQUETA_VIA.get(r.via or "", ""), "ejecucion_directa": directa}
    return {"error": r.outcome, "mensaje": _MENSAJE_AGENTE.get(r.outcome, "No se pudo enviar la plantilla.")}
