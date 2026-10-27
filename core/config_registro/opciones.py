"""Opciones de configuración: 31 de Identidad y comportamiento (SPEC-37) y las del Coordinador y el
Agent OS (HU-CFG-07).

Cada `por_defecto`, rango y lista de valores sale de la constante del runtime que ya los usa
(`DEBOUNCE_DELAY_MS`, `CAPS`, `TOGGLES`, los umbrales de dificultad, la política de WhatsApp
Web…): el registro las referencia, no las copia. Las etiquetas y explicaciones son texto para
personas y no entran a ningún prompt.
"""

from __future__ import annotations

from typing import Any, get_args

from agentcore_contracts import KernelSpec, Persona
from annotated_types import Ge, Le
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from core.admin.agent_os import RUNTIMES
from core.admin.envios_segmento import PALABRAS_DE_BAJA_POR_DEFECTO
from core.agents.spec import DEFAULT_SPEC
from core.config import DEBOUNCE_DELAY_MS, Settings
from core.config_limites import rango
from core.config_registro.modelo import Dependencia, Opcion, ValorPosible
from core.db.models import DEFAULT_ASSISTANT_NAME
from core.gateway.message_splitter import CAPS
from core.kernel.banderas import APAGADAS, MODOS_PILA, MODOS_VERIFICACION, Colaboracion
from core.kernel.fuera_de_horario import IDIOMA_POR_DEFECTO, IDIOMAS
from core.kernel.router import MODOS as MODOS_RUTEO
from core.llm.difficulty_router import (
    DIFFICULTY_SCORE_THRESHOLD,
    LONG_MESSAGE_THRESHOLD,
    TURNOS_SIN_RESOLVER_THRESHOLD,
)
from core.llm.prompt_builder import DEFAULT_TIMEZONE
from core.orchestrator.escalation import DEFAULT_ESCALATION_KEYWORDS
from core.orchestrator.guardrails import MAX_GUARDRAIL_RETRIES
from core.waweb.policy import (
    DEFAULT_DAILY_OUTBOUND_CAP,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MIN_DELAY_MS,
    DEFAULT_WARMUP_ENABLED,
    MODE_ALLOW_OUTBOUND,
    MODE_REPLY_ONLY,
)

_PERSONA_POR_DEFECTO: dict[str, Any] = DEFAULT_SPEC["persona"]
_CAMPOS_SETTINGS = Settings.model_fields


def _valores(*pares: tuple[str, str, str]) -> list[ValorPosible]:
    return [ValorPosible(valor=v, etiqueta=e, nota=n) for v, e, n in pares]


# --------------------------------------------------------------------- valores derivados
_ETIQUETA_VELOCIDAD = {
    "inmediato": "Inmediata",
    "corto": "Corta",
    "rapido": "Rápida",
    "natural": "Natural",
    "pausado": "Pausada",
}


def _nota_velocidad(ms: int) -> str:
    return "responde sin esperar" if ms == 0 else f"espera {ms // 1000} s"


VALORES_VELOCIDAD = [
    ValorPosible(
        valor=modo,
        etiqueta=_ETIQUETA_VELOCIDAD.get(modo, modo.capitalize()),
        nota=_nota_velocidad(ms),
    )
    for modo, ms in DEBOUNCE_DELAY_MS.items()
]

_ETIQUETA_ENVIO = {
    "un_mensaje": "Un solo mensaje",
    "pocos_cortos": "Pocos mensajes cortos",
    "varios_cortos": "Varios mensajes cortos",
}
VALORES_ENVIO = [
    ValorPosible(
        valor=estilo,
        etiqueta=_ETIQUETA_ENVIO.get(estilo, estilo),
        nota="1 por respuesta" if tope == 1 else f"hasta {tope} por respuesta",
    )
    for estilo, tope in CAPS.items()
]

_ETIQUETA_TONO = {
    "formal": "Formal",
    "neutral": "Neutral",
    "cercano": "Cercano",
    "informal": "Informal",
}
_NOTA_TONO = {
    "formal": "trata de usted y evita coloquialismos",
    "neutral": "correcto y sin adornos",
    "cercano": "amable y cálido, de tú",
    "informal": "relajado, como entre conocidos",
}
VALORES_TONO = [
    ValorPosible(valor=t, etiqueta=_ETIQUETA_TONO.get(t, t), nota=_NOTA_TONO.get(t, ""))
    for t in get_args(Persona.model_fields["tone"].annotation)
]

_ETIQUETA_EMOJIS = {"none": "Ninguno", "sparse": "Pocos", "free": "Libres"}
_NOTA_EMOJIS = {
    "none": "nunca usa emojis",
    "sparse": "alguno de vez en cuando",
    "free": "los usa con naturalidad",
}
VALORES_EMOJIS = [
    ValorPosible(valor=e, etiqueta=_ETIQUETA_EMOJIS.get(e, e), nota=_NOTA_EMOJIS.get(e, ""))
    for e in get_args(Persona.model_fields["emojis"].annotation)
]

_ETIQUETA_IDIOMA = {"es": "Español", "en": "Inglés", "pt": "Portugués"}
VALORES_IDIOMA = [ValorPosible(valor=i, etiqueta=_ETIQUETA_IDIOMA.get(i, i)) for i in IDIOMAS]

VALORES_MONEDA = _valores(
    ("COP", "Peso colombiano (COP)", ""),
    ("USD", "Dólar (USD)", ""),
    ("MXN", "Peso mexicano (MXN)", ""),
    ("PEN", "Sol peruano (PEN)", ""),
)

VALORES_ZONA_HORARIA = _valores(
    ("America/Bogota", "Colombia (Bogotá)", ""),
    ("America/Lima", "Perú (Lima)", ""),
    ("America/Guayaquil", "Ecuador (Guayaquil)", ""),
    ("America/Panama", "Panamá", ""),
    ("America/Mexico_City", "México (Ciudad de México)", ""),
    ("America/Guatemala", "Guatemala", ""),
    ("America/Costa_Rica", "Costa Rica", ""),
    ("America/Caracas", "Venezuela (Caracas)", ""),
    ("America/Santo_Domingo", "República Dominicana", ""),
    ("America/Santiago", "Chile (Santiago)", ""),
    ("America/Argentina/Buenos_Aires", "Argentina (Buenos Aires)", ""),
    ("America/Sao_Paulo", "Brasil (São Paulo)", ""),
    ("America/New_York", "Estados Unidos (Nueva York)", ""),
    ("Europe/Madrid", "España (Madrid)", ""),
    ("UTC", "UTC", ""),
)

# --------------------------------------------------------------------- las opciones
IDENTIDAD = [
    Opcion(
        clave="assistant_name",
        grupo="identidad",
        tipo="texto",
        etiqueta="Nombre del asistente",
        explicacion="Cómo se presenta el asistente a tus clientes y cómo firma sus mensajes.",
        ejemplo="Sofía",
        por_defecto=DEFAULT_ASSISTANT_NAME,
        almacen="columna:assistant_name",
    ),
    Opcion(
        clave="business_description",
        grupo="identidad",
        tipo="texto_largo",
        etiqueta="Descripción del negocio",
        explicacion="Qué hace tu negocio, en pocas líneas. El asistente la usa para contestar con contexto.",
        ejemplo="Panadería artesanal en Manizales con domicilios en el centro.",
        por_defecto="",
        almacen="columna:business_description",
    ),
    Opcion(
        clave="brand_voice",
        grupo="identidad",
        tipo="texto_largo",
        etiqueta="Voz de marca",
        explicacion="Guía el estilo de las respuestas. Si queda vacía, el asistente no tiene una voz propia de tu marca.",
        ejemplo="Cercana y breve; nunca promete tiempos de entrega.",
        por_defecto="",
        almacen="columna:brand_voice",
    ),
    Opcion(
        clave="language",
        grupo="identidad",
        tipo="elegir_una",
        etiqueta="Idioma del negocio",
        explicacion="Idioma de los avisos automáticos, como el mensaje fuera de horario. Se fija al crear el negocio.",
        valores=VALORES_IDIOMA,
        por_defecto=IDIOMA_POR_DEFECTO,
        solo_lectura=True,
        almacen="columna:language",
    ),
    Opcion(
        clave="currency",
        grupo="identidad",
        tipo="elegir_una",
        etiqueta="Moneda del negocio",
        explicacion=(
            "Con qué moneda se muestran los montos de comercio, cobranza y plan. "
            "Los costos de proveedor (IA, Meta) se muestran siempre en USD."
        ),
        valores=VALORES_MONEDA,
        por_defecto="COP",
        almacen="columna:currency",
    ),
    Opcion(
        clave="timezone",
        grupo="identidad",
        tipo="elegir_una",
        etiqueta="Zona horaria del negocio",
        explicacion=(
            "Con esta zona el asistente sabe qué día y hora es («hoy», «mañana») "
            "y aplica el horario de atención."
        ),
        valores=VALORES_ZONA_HORARIA,
        por_defecto=DEFAULT_TIMEZONE,
        almacen="columna:timezone",
    ),
    Opcion(
        clave="bot_paused",
        grupo="identidad",
        tipo="booleano",
        etiqueta="Pausar el asistente",
        explicacion=(
            "Con la pausa activa el asistente deja de contestar en todos los canales del negocio "
            "hasta que lo reactives. Las conversaciones siguen entrando a la bandeja."
        ),
        por_defecto=False,
        almacen="columna:bot_paused",
    ),
]

RESPUESTA = [
    Opcion(
        clave="debounce_mode",
        grupo="respuesta",
        tipo="elegir_una",
        etiqueta="Velocidad de respuesta",
        explicacion=(
            "Cuánto espera el asistente a que el cliente termine de escribir antes de responder. "
            "Esperar más junta varios mensajes seguidos en una sola respuesta."
        ),
        valores=VALORES_VELOCIDAD,
        por_defecto=_CAMPOS_SETTINGS["debounce_mode"].default,
        almacen="behavior_config:debounce_mode",
    ),
    Opcion(
        clave="split_style",
        grupo="respuesta",
        tipo="elegir_una",
        etiqueta="Estilo de envío",
        explicacion=(
            "Cuántos mensajes envía el asistente por respuesta. Meta cobra cada mensaje de servicio: "
            "«Un solo mensaje» gasta menos."
        ),
        valores=VALORES_ENVIO,
        por_defecto=_CAMPOS_SETTINGS["message_split_style"].default,
        almacen="behavior_config:split_style",
    ),
    Opcion(
        clave="guardrail_fail_action",
        grupo="respuesta",
        tipo="elegir_una",
        etiqueta="Si el asistente no logra corregir su respuesta",
        explicacion=(
            "Qué hace cuando agota los reintentos de una validación. Responder que va a verificar "
            "no pausa el asistente; el escalamiento por turnos sin resolver sigue activo."
        ),
        valores=_valores(
            ("escalate", "Escalar a una persona", "pasa la conversación a tu equipo"),
            ("neutral", "Responder que va a verificar", "el asistente sigue atendiendo"),
        ),
        por_defecto="escalate",
        almacen="behavior_config:guardrail_fail_action",
    ),
]

CALIDAD = [
    Opcion(
        clave="guardrail_max_retries",
        grupo="calidad",
        tipo="entero",
        etiqueta="Reintentos de validación",
        explicacion=(
            "Intentos para corregir una respuesta que falló una validación o parece inventada. "
            "Más intentos mejoran la calidad pero gastan más."
        ),
        minimo=rango("guardrail_max_retries").minimo,
        maximo=rango("guardrail_max_retries").maximo,
        por_defecto=MAX_GUARDRAIL_RETRIES,
        almacen="behavior_config:guardrail_max_retries",
    ),
    Opcion(
        clave="disabled_guardrails",
        grupo="calidad",
        tipo="elegir_varias",
        etiqueta="Validaciones desactivadas",
        explicacion=(
            "Validaciones de calidad que el asistente no aplica en este negocio. "
            "La lista depende de los módulos que tengas activos."
        ),
        por_defecto=[],
        almacen="behavior_config:disabled_guardrails",
    ),
    Opcion(
        clave="difficulty_long_message_threshold",
        grupo="calidad",
        tipo="entero",
        etiqueta="Mensaje largo",
        explicacion="Desde cuántos caracteres un mensaje cuenta como difícil y se manda al modelo de razonamiento.",
        unidad="caracteres",
        minimo=rango("difficulty_long_message_threshold").minimo,
        maximo=rango("difficulty_long_message_threshold").maximo,
        por_defecto=LONG_MESSAGE_THRESHOLD,
        almacen="behavior_config:difficulty_long_message_threshold",
    ),
    Opcion(
        clave="difficulty_turnos_sin_resolver_threshold",
        grupo="calidad",
        tipo="entero",
        etiqueta="Turnos sin resolver",
        explicacion="Cuántos turnos seguidos sin resolver el problema antes de pasar al modelo de razonamiento.",
        unidad="turnos",
        minimo=rango("difficulty_turnos_sin_resolver_threshold").minimo,
        maximo=rango("difficulty_turnos_sin_resolver_threshold").maximo,
        por_defecto=TURNOS_SIN_RESOLVER_THRESHOLD,
        almacen="behavior_config:difficulty_turnos_sin_resolver_threshold",
    ),
    Opcion(
        clave="difficulty_score_threshold",
        grupo="calidad",
        tipo="entero",
        etiqueta="Puntaje para razonamiento",
        explicacion="Puntaje de dificultad (1 a 10) desde el cual la conversación pasa al modelo de razonamiento.",
        unidad="puntos",
        minimo=rango("difficulty_score_threshold").minimo,
        maximo=rango("difficulty_score_threshold").maximo,
        por_defecto=DIFFICULTY_SCORE_THRESHOLD,
        almacen="behavior_config:difficulty_score_threshold",
    ),
]

BAJAS = [
    Opcion(
        clave="palabras_de_baja",
        grupo="bajas",
        tipo="lista_de_textos",
        etiqueta="Palabras de baja",
        explicacion=(
            "Si un cliente escribe una de estas frases (el mensaje entero), deja de recibir envíos "
            "a segmentos. Vacío = usa las palabras por defecto."
        ),
        ejemplo="baja, stop, no quiero más promociones",
        por_defecto=list(PALABRAS_DE_BAJA_POR_DEFECTO),
        almacen="behavior_config:palabras_de_baja",
    ),
]

WHATSAPP_WEB = [
    Opcion(
        clave="waweb_mode",
        grupo="whatsapp_web",
        tipo="elegir_una",
        etiqueta="Modo de envío en WhatsApp Web",
        explicacion=(
            "El canal no oficial puede terminar en suspensión de la cuenta. Por defecto solo contesta "
            "a quien escribió primero; permitir envío no solicitado aumenta el riesgo de bloqueo."
        ),
        valores=_valores(
            (MODE_REPLY_ONLY, "Solo responder", "contesta a quien escribió primero"),
            (MODE_ALLOW_OUTBOUND, "Permitir envío no solicitado", "riesgo de suspensión de la cuenta"),
        ),
        por_defecto=MODE_REPLY_ONLY,
        editable_por="master",
        almacen="behavior_config:waweb_mode",
    ),
    Opcion(
        clave="waweb_send_min_delay_ms",
        grupo="whatsapp_web",
        tipo="entero",
        etiqueta="Pausa mínima entre mensajes",
        explicacion="Espera más corta entre un mensaje y el siguiente, para que el envío parezca humano.",
        unidad="ms",
        minimo=rango("waweb_send_min_delay_ms").minimo,
        maximo=rango("waweb_send_min_delay_ms").maximo,
        por_defecto=DEFAULT_MIN_DELAY_MS,
        editable_por="master",
        almacen="behavior_config:waweb_send_min_delay_ms",
    ),
    Opcion(
        clave="waweb_send_max_delay_ms",
        grupo="whatsapp_web",
        tipo="entero",
        etiqueta="Pausa máxima entre mensajes",
        explicacion="Espera más larga entre mensajes. Si queda por debajo de la mínima, se usa la mínima.",
        unidad="ms",
        minimo=rango("waweb_send_max_delay_ms").minimo,
        maximo=rango("waweb_send_max_delay_ms").maximo,
        por_defecto=DEFAULT_MAX_DELAY_MS,
        editable_por="master",
        almacen="behavior_config:waweb_send_max_delay_ms",
    ),
    Opcion(
        clave="waweb_daily_outbound_cap",
        grupo="whatsapp_web",
        tipo="entero",
        etiqueta="Tope diario de envíos no solicitados",
        explicacion="Máximo de mensajes que el asistente inicia por día. Aplica solo si se permite el envío no solicitado.",
        unidad="mensajes",
        minimo=rango("waweb_daily_outbound_cap").minimo,
        maximo=rango("waweb_daily_outbound_cap").maximo,
        por_defecto=DEFAULT_DAILY_OUTBOUND_CAP,
        depende_de=Dependencia(
            clave="waweb_mode",
            condicion="igual",
            valor=MODE_ALLOW_OUTBOUND,
            motivo="Solo cuenta cuando el modo permite envío no solicitado.",
        ),
        editable_por="master",
        almacen="behavior_config:waweb_daily_outbound_cap",
    ),
    Opcion(
        clave="waweb_warmup_enabled",
        grupo="whatsapp_web",
        tipo="booleano",
        etiqueta="Calentamiento de la cuenta",
        explicacion="Sube el ritmo de envíos poco a poco en una cuenta nueva para reducir el riesgo de bloqueo.",
        por_defecto=DEFAULT_WARMUP_ENABLED,
        editable_por="master",
        almacen="behavior_config:waweb_warmup_enabled",
    ),
]

ESCALAMIENTO = [
    Opcion(
        clave="escalation_keywords",
        grupo="escalamiento",
        tipo="lista_de_textos",
        etiqueta="Palabras que disparan escalamiento",
        explicacion=(
            "Si el cliente escribe alguna, la conversación pasa a una persona de tu equipo. "
            "Vacío = usa las palabras por defecto."
        ),
        ejemplo="asesor, humano, reclamo",
        por_defecto=list(DEFAULT_ESCALATION_KEYWORDS),
        almacen="escalation_config:keywords",
    ),
    Opcion(
        clave="auto_escalate_after_turns",
        grupo="escalamiento",
        tipo="entero",
        etiqueta="Escalar tras N turnos",
        explicacion="Pasa la conversación a una persona si el asistente no la resuelve en este número de turnos. 0 = nunca.",
        unidad="turnos",
        minimo=rango("auto_escalate_after_turns").minimo,
        maximo=rango("auto_escalate_after_turns").maximo,
        por_defecto=0,
        almacen="escalation_config:auto_escalate_after_turns",
    ),
    Opcion(
        clave="business_tz",
        grupo="horario",
        tipo="elegir_una",
        etiqueta="Zona del horario de atención",
        explicacion="Zona en la que se leen las franjas del horario. Si no eliges una, se usa la zona del negocio.",
        valores=VALORES_ZONA_HORARIA,
        # Sin valor propio hereda la zona del negocio (`timezone`): no hay un defecto fijo.
        por_defecto=None,
        depende_de=Dependencia(
            clave="business_ranges",
            condicion="no_vacia",
            motivo="Sin franjas de atención, la zona del horario no se usa.",
        ),
        almacen="escalation_config:business_hours.tz",
    ),
    Opcion(
        clave="business_ranges",
        grupo="horario",
        tipo="franjas",
        etiqueta="Horario de atención",
        explicacion=(
            "Franjas en las que el asistente atiende: día (0=lunes … 6=domingo) y hora de inicio y fin "
            "en HH:MM. Sin franjas, el asistente responde siempre."
        ),
        ejemplo="0 08:00-18:00 es el lunes de 8 a 18",
        por_defecto=[],
        almacen="escalation_config:business_hours.ranges",
    ),
    Opcion(
        clave="business_hours_mode",
        grupo="horario",
        tipo="elegir_una",
        etiqueta="Qué hace el asistente fuera del horario",
        explicacion=(
            "Con «Informativo» el horario es el de tu equipo: el asistente responde a toda hora y lo "
            "conoce para decírselo a tus clientes. Con «Pausa» deja de atender fuera del horario y "
            "responde el mensaje de abajo."
        ),
        valores=_valores(
            ("informativo", "Informativo", "el asistente responde 24/7 y conoce el horario del equipo"),
            ("bloquea", "Pausa", "fuera del horario responde solo el mensaje fuera de horario"),
        ),
        por_defecto="bloquea",
        depende_de=Dependencia(
            clave="business_ranges",
            condicion="no_vacia",
            motivo="Sin horario de atención, el asistente responde siempre.",
        ),
        almacen="escalation_config:business_hours_mode",
    ),
    Opcion(
        clave="out_of_hours_message",
        grupo="horario",
        tipo="texto_largo",
        etiqueta="Mensaje fuera de horario",
        explicacion=(
            "Lo que recibe el cliente cuando escribe fuera del horario, sin pasar por la IA. "
            "{assistant_name} se reemplaza por el nombre del asistente. Vacío = aviso por defecto."
        ),
        por_defecto="",
        depende_de=Dependencia(
            clave="business_ranges",
            condicion="no_vacia",
            motivo="Sin horario de atención, el asistente responde siempre y este mensaje no se usa.",
        ),
        almacen="escalation_config:out_of_hours_message",
    ),
]

VOZ = [
    Opcion(
        clave="persona_tone",
        grupo="voz",
        tipo="elegir_una",
        etiqueta="Tono de personalidad",
        explicacion="Cómo suena el asistente: más formal o más cercano al hablar con tus clientes.",
        valores=VALORES_TONO,
        por_defecto=_PERSONA_POR_DEFECTO["tone"],
        almacen="spec:persona.tone",
    ),
    Opcion(
        clave="persona_warmth",
        grupo="voz",
        tipo="entero",
        etiqueta="Calidez",
        explicacion="Qué tan afectuoso es el trato, de 0 (seco) a 5 (muy cálido).",
        minimo=rango("persona_warmth").minimo,
        maximo=rango("persona_warmth").maximo,
        por_defecto=_PERSONA_POR_DEFECTO["warmth"],
        almacen="spec:persona.warmth",
    ),
    Opcion(
        clave="persona_emojis",
        grupo="voz",
        tipo="elegir_una",
        etiqueta="Emojis",
        explicacion="Con qué frecuencia usa emojis en sus respuestas.",
        valores=VALORES_EMOJIS,
        por_defecto=_PERSONA_POR_DEFECTO["emojis"],
        almacen="spec:persona.emojis",
    ),
    Opcion(
        clave="persona_max_length",
        grupo="voz",
        tipo="entero",
        etiqueta="Longitud máxima de respuesta",
        explicacion="Largo máximo de cada respuesta. 0 = sin límite.",
        unidad="caracteres",
        minimo=rango("persona_max_length").minimo,
        maximo=rango("persona_max_length").maximo,
        por_defecto=_PERSONA_POR_DEFECTO["maxLength"],
        almacen="spec:persona.maxLength",
    ),
    Opcion(
        clave="persona_forbidden",
        grupo="voz",
        tipo="lista_de_textos",
        etiqueta="Frases prohibidas",
        explicacion="Frases que el asistente nunca debe decir.",
        ejemplo="garantizado, sin falta",
        por_defecto=list(_PERSONA_POR_DEFECTO["forbiddenPhrases"]),
        almacen="spec:persona.forbiddenPhrases",
    ),
]


# ------------------------------------------------------------------ Coordinador (KernelSpec)
# HU-CFG-07. El KernelSpec (`kernelspec/1`) vive en la versión del agente `core` de cada CONEXIÓN y
# se cambia con una versión nueva y su release: el registro solo lo DESCRIBE. Rangos y defectos salen
# de los campos del contrato congelado, no se copian.
def _campo(ruta: str) -> FieldInfo:
    modelo: type[BaseModel] = KernelSpec
    campo: FieldInfo | None = None
    for parte in ruta.split("."):
        campo = modelo.model_fields[parte]
        anotacion = campo.annotation
        if isinstance(anotacion, type) and issubclass(anotacion, BaseModel):
            modelo = anotacion
    assert campo is not None
    return campo


def _rango_kernel(ruta: str) -> tuple[float, float]:
    metadatos = _campo(ruta).metadata
    minimo = next(m.ge for m in metadatos if isinstance(m, Ge))
    maximo = next(m.le for m in metadatos if isinstance(m, Le))
    return minimo, maximo


def _defecto_kernel(ruta: str) -> Any:
    return _campo(ruta).default


def hojas_del_kernelspec(modelo: type[BaseModel] = KernelSpec, prefijo: str = "") -> list[str]:
    """Rutas punteadas de cada campo que no es un submodelo (listas y uniones incluidas)."""
    rutas: list[str] = []
    for nombre, campo in modelo.model_fields.items():
        anotacion = campo.annotation
        if isinstance(anotacion, type) and issubclass(anotacion, BaseModel):
            rutas += hojas_del_kernelspec(anotacion, f"{prefijo}{nombre}.")
        else:
            rutas.append(f"{prefijo}{nombre}")
    return rutas


# Hojas del KernelSpec que el registro NO ofrece, y por qué. La prueba de consistencia exige que
# toda hoja esté registrada o aquí: un campo nuevo del contrato no puede pasar sin que alguien decida.
EXCLUIDAS_DEL_KERNELSPEC: dict[str, str] = {
    "contract": "es la versión del contrato, no una opción",
    "voice.role": "la voz es la de `persona_*`; ofrecerla aquí duplicaría el selector (SPEC-37)",
    "voice.tone": "la voz es la de `persona_*`; ofrecerla aquí duplicaría el selector (SPEC-37)",
    "voice.warmth": "la voz es la de `persona_*`; ofrecerla aquí duplicaría el selector (SPEC-37)",
    "voice.emojis": "la voz es la de `persona_*`; ofrecerla aquí duplicaría el selector (SPEC-37)",
    "routing.defaultAgent": "referencia un agente que debe existir en la conexión: necesita lista calculada",
    "routing.rules": "es un DSL de reglas, no un valor; lo edita la vista de enrutamiento (HU-AOS-26)",
    "routing.coprocessor.model": "id de modelo en texto libre: el runtime lo ignora si es de otra familia (ADR-0086, ADR-0132)",
    "routing.coprocessor.playbook": "elige qué playbook corre: es selección de prompt (invariante 3)",
    "modelPolicy.highRiskStage.model": "id de modelo en texto libre: el runtime lo ignora si es de otra familia (ADR-0086, ADR-0132)",
    "composition.style": "un solo valor posible: no hay nada que elegir",
    "responses": "elige qué playbook de respuestas corre: es selección de prompt (invariante 3)",
    "policies.budgetUsdPerConversation": "admite «sin tope» (nulo) y el registro aún no expresa opciones anulables",
    "policies.backofficeBudgetUsdMonthly": "admite «sin tope» (nulo) y el registro aún no expresa opciones anulables",
}  # fmt: skip

_SOLO_V3 = " Solo tiene efecto cuando el Agent OS del negocio está activo (runtime v3)."


def _k(
    clave: str,
    ruta: str,
    tipo: str,
    etiqueta: str,
    explicacion: str,
    *,
    ejemplo: str = "",
    unidad: str = "",
    valores: list[ValorPosible] | None = None,
    editable_por: str = "dueno",
) -> Opcion:
    """Una opción del KernelSpec: defecto y rango salen del contrato."""
    numero = tipo in ("entero", "decimal")
    minimo, maximo = _rango_kernel(ruta) if numero else (None, None)
    return Opcion(
        clave=clave,
        grupo="coordinador",
        tipo=tipo,  # type: ignore[arg-type]
        etiqueta=etiqueta,
        explicacion=explicacion + _SOLO_V3,
        ejemplo=ejemplo,
        unidad=unidad,
        valores=valores or [],
        minimo=minimo,
        maximo=maximo,
        por_defecto=_defecto_kernel(ruta),
        editable_por=editable_por,  # type: ignore[arg-type]
        almacen=f"kernelspec:{ruta}",
    )


_CONFIRMACION = {
    "interactive_button": ("Con un botón", "el cliente toca «Confirmar»; un «sí» escrito no basta"),
    "explicit": ("Escribiendo", "el cliente confirma con una frase clara"),
}
VALORES_CONFIRMACION = [
    ValorPosible(valor=v, etiqueta=_CONFIRMACION.get(v, (v, ""))[0], nota=_CONFIRMACION.get(v, (v, ""))[1])
    for v in get_args(_campo("procedures.confirmIrreversibleWith").annotation)
]

COORDINADOR: list[Opcion] = [
    _k(
        "kernel_episode_idle_minutes", "episode.idleMinutes", "entero", "Pausa que cierra un tramo de conversación",
        "Si el cliente deja de escribir tanto tiempo, el siguiente mensaje empieza un tramo nuevo y puede atenderlo otro agente.",
        unidad="minutos", ejemplo="720 = 12 horas",
    ),
    _k(
        "kernel_routing_shadow_mode", "routing.shadowMode", "booleano", "Probar el enrutamiento sin aplicarlo",
        "Calcula a qué agente pasaría cada conversación y lo registra, pero la atiende quien la atendía.",
    ),
    _k(
        "kernel_routing_triggers_embeddings", "routing.triggers.embeddings", "booleano", "Entender el sentido de la frase",
        "Además de las palabras clave, compara el significado del mensaje con las habilidades disponibles.",
    ),
    _k(
        "kernel_routing_triggers_min_score", "routing.triggers.minScore", "decimal", "Parecido mínimo para considerar una habilidad",
        "De 0 a 1. Por debajo de este parecido, una habilidad ni se considera para el mensaje del cliente.",
    ),
    _k(
        "kernel_routing_triggers_strong_score", "routing.triggers.strongScore", "decimal", "Parecido que decide sin dudar",
        "De 0 a 1. Con este parecido o más, se elige la habilidad sin desempatar. No puede ser menor que el parecido mínimo.",
    ),
    _k(
        "kernel_routing_triggers_margin", "routing.triggers.margin", "decimal", "Ventaja necesaria sobre la segunda",
        "De 0 a 1. Cuánto debe ganarle la mejor habilidad a la siguiente para elegirla sin desempatar.",
    ),
    _k(
        "kernel_routing_triggers_max_candidates", "routing.triggers.maxCandidates", "entero", "Habilidades que se comparan",
        "Cuántas habilidades candidatas se consideran por mensaje. Más candidatas ayudan a acertar y cuestan un poco más.",
    ),
    _k(
        "kernel_routing_coprocessor_when_candidates_above", "routing.coprocessor.whenCandidatesAbove", "entero",
        "Candidatas que activan al desempatador",
        "Si quedan más candidatas que este número, un modelo pequeño desempata cuál es la adecuada.",
    ),
    _k(
        "kernel_routing_coprocessor_thinking", "routing.coprocessor.thinking", "booleano", "Que el desempatador razone",
        "El desempatador razona antes de decidir. Acierta más en casos dudosos y cuesta más.",
    ),
    _k(
        "kernel_routing_coprocessor_max_cost_usd", "routing.coprocessor.maxCostUsd", "decimal", "Costo máximo por desempate",
        "Tope de gasto de cada desempate. Si lo superaría, se queda con la mejor candidata.",
        unidad="USD", editable_por="master",
    ),
    _k(
        "kernel_routing_owner_max_changes", "routing.owner.maxOwnerChangesPer6Turns", "entero", "Cambios de agente en 6 mensajes",
        "Evita que la conversación salte de un agente a otro en cada mensaje. 0 = nunca cambia dentro de esos 6 mensajes.",
    ),
    _k(
        "kernel_procedures_max_stack", "procedures.maxStack", "entero", "Gestiones abiertas a la vez",
        "Cuántas gestiones (como un pedido y una cita) puede tener abiertas un cliente al mismo tiempo.",
    ),
    _k(
        "kernel_procedures_confirm_irreversible_with", "procedures.confirmIrreversibleWith", "elegir_una",
        "Cómo se confirma lo que no se puede deshacer",
        "Para acciones irreversibles, como cobrar o cancelar. Siempre se confirman; aquí eliges cómo.",
        valores=VALORES_CONFIRMACION,
    ),
    _k(
        "kernel_model_policy_high_risk_thinking", "modelPolicy.highRiskStage.thinking", "booleano",
        "Razonar más en pasos delicados",
        "En las etapas de riesgo alto (dinero, datos personales) el modelo razona antes de responder. Cuesta algo más.",
    ),
    _k(
        "kernel_a2a_max_depth", "a2a.maxDepth", "entero", "Cadena máxima entre agentes",
        "Cuántos agentes pueden encargarse algo en cadena dentro de un mismo mensaje del cliente.",
    ),
    _k(
        "kernel_a2a_max_calls_per_turn", "a2a.maxCallsPerTurn", "entero", "Encargos máximos por mensaje",
        "Cuántas veces puede un agente pedirle algo a otro mientras responde un solo mensaje. 0 = ninguna.",
    ),
    _k(
        "kernel_a2a_turn_budget_usd", "a2a.turnBudgetUsd", "decimal", "Gasto máximo entre agentes por mensaje",
        "Tope de lo que pueden gastar los agentes colaborando para responder un mensaje. Cada enlace entre agentes solo puede bajarlo.",
        unidad="USD", editable_por="master",
    ),
    _k(
        "kernel_a2a_turn_budget_tokens", "a2a.turnBudgetTokens", "entero", "Texto máximo entre agentes por mensaje",
        "Tope del texto que pueden procesar los agentes colaborando para responder un mensaje. Cada enlace solo puede bajarlo.",
        unidad="tokens", editable_por="master",
    ),
    _k(
        "kernel_composition_max_questions", "composition.maxQuestions", "entero", "Preguntas por respuesta",
        "Máximo de preguntas que el asistente le hace al cliente en una misma respuesta. 0 = ninguna.",
    ),
]  # fmt: skip

# ------------------------------------------------------------------ banderas del Agent OS
_OPCION_RUNTIME = "aos_runtime"
_RUNTIME = {"legacy": ("Clásico", "el asistente de siempre"), "v3": ("Agent OS", "Coordinador y agentes versionados")}
_RUTEO = {
    "off": ("Apagado", "atiende siempre el agente de la conexión"),
    "shadow": ("Sombra", "calcula y registra, sin aplicar"),
    "active": ("Activo", "pasa la conversación al agente elegido"),
}
_PILA = {
    "off": ("Apagado", "atiende un pedido por mensaje, como siempre"),
    "shadow": ("Sombra", "anota lo que abriría, sin cambiar nada"),
    "active": ("Activo", "abre los dos pedidos: uno en curso y otro en espera"),
}
_VERIFICAR = {
    "shadow": ("Sombra", "solo cuenta"),
    "active": ("Activo", "corrige la respuesta"),
}
AGENT_OS: list[Opcion] = [
    Opcion(
        clave="aos_runtime",
        grupo="agent_os",
        tipo="elegir_una",
        etiqueta="Motor de conversación",
        explicacion=(
            "Con «Agent OS» el negocio usa el Coordinador y los agentes versionados; con «Clásico», el asistente "
            "de siempre. El Agent OS además exige AOS_CONNECTION_ROUTING en el entorno del despliegue."
        ),
        valores=[ValorPosible(valor=v, etiqueta=_RUNTIME.get(v, (v, ""))[0], nota=_RUNTIME.get(v, (v, ""))[1]) for v in RUNTIMES],
        por_defecto=APAGADAS.runtime,
        editable_por="master",
        almacen="tenants:aos_runtime",
    ),
    Opcion(
        clave="aos_procedures",
        grupo="agent_os",
        tipo="booleano",
        etiqueta="Procedimientos guiados",
        explicacion=(
            "El Coordinador lleva las gestiones (pedidos, citas, cobros) paso a paso y las verifica, en vez de "
            "dejar que el modelo las cierre solo. Antes de encenderlos, mide el pass^4 del negocio."
        ),
        por_defecto=APAGADAS.procedimientos,
        depende_de=Dependencia(
            clave=_OPCION_RUNTIME, condicion="igual", valor="v3", motivo="Los procedimientos exigen el Agent OS."
        ),
        editable_por="master",
        almacen="tenants:aos_procedures",
    ),
    Opcion(
        clave="aos_routing",
        grupo="agent_os",
        tipo="elegir_una",
        etiqueta="Enrutamiento entre agentes",
        explicacion=(
            "Decide qué agente atiende cada mensaje. «Sombra» calcula y registra sin aplicar; «Activo» sí pasa la "
            "conversación al agente elegido."
        ),
        valores=[ValorPosible(valor=v, etiqueta=_RUTEO.get(v, (v, ""))[0], nota=_RUTEO.get(v, (v, ""))[1]) for v in MODOS_RUTEO],
        por_defecto=APAGADAS.ruteo,
        depende_de=Dependencia(
            clave=_OPCION_RUNTIME, condicion="igual", valor="v3", motivo="El enrutamiento exige el Agent OS."
        ),
        editable_por="master",
        almacen="tenants:aos_routing",
    ),
    # Colaboración con pila de tareas (HU-AOS-20, adenda de ADR-0116), en `tenants.settings.agent_os`.
    Opcion(
        clave="agent_os_pila",
        grupo="agent_os",
        tipo="elegir_una",
        etiqueta="Dos pedidos en un mensaje",
        explicacion=(
            "Cuando el cliente pide dos cosas a la vez («el Premium y una demo»), el Coordinador abre las dos: una en "
            "curso y la otra en espera. «Sombra» solo anota lo que abriría; «Activo» las abre. Exige el enrutamiento."
        ),
        valores=[ValorPosible(valor=v, etiqueta=_PILA.get(v, (v, ""))[0], nota=_PILA.get(v, (v, ""))[1]) for v in MODOS_PILA],
        por_defecto=Colaboracion().pila,
        depende_de=Dependencia(
            clave=_OPCION_RUNTIME, condicion="igual", valor="v3", motivo="La pila de tareas exige el Agent OS."
        ),
        editable_por="master",
        almacen="tenants:settings.agent_os.pila",
    ),
    Opcion(
        clave="agent_os_verificar_horarios",
        grupo="agent_os",
        tipo="elegir_una",
        etiqueta="Verificar horarios",
        explicacion=(
            "El verificador revisa que cada hora o fecha que ofrece el asistente salga de la agenda. «Sombra» solo "
            "cuenta las que no tienen respaldo; «Activo» corrige la respuesta."
        ),
        valores=[ValorPosible(valor=v, etiqueta=_VERIFICAR.get(v, (v, ""))[0], nota=_VERIFICAR.get(v, (v, ""))[1]) for v in MODOS_VERIFICACION],
        por_defecto=Colaboracion().verificar_horarios,
        depende_de=Dependencia(
            clave=_OPCION_RUNTIME, condicion="igual", valor="v3", motivo="El verificador de horarios exige el Agent OS."
        ),
        editable_por="master",
        almacen="tenants:settings.agent_os.verificar_horarios",
    ),
    Opcion(
        clave="agent_os_verificar_preguntas",
        grupo="agent_os",
        tipo="elegir_una",
        etiqueta="Una pregunta por respuesta",
        explicacion=(
            "El verificador cuenta las preguntas de cada respuesta contra «Preguntas por respuesta». «Sombra» solo "
            "cuenta; «Activo» pide al asistente reescribirla con una sola pregunta."
        ),
        valores=[ValorPosible(valor=v, etiqueta=_VERIFICAR.get(v, (v, ""))[0], nota=_VERIFICAR.get(v, (v, ""))[1]) for v in MODOS_VERIFICACION],
        por_defecto=Colaboracion().verificar_preguntas,
        depende_de=Dependencia(
            clave=_OPCION_RUNTIME, condicion="igual", valor="v3", motivo="El verificador de preguntas exige el Agent OS."
        ),
        editable_por="master",
        almacen="tenants:settings.agent_os.verificar_preguntas",
    ),
]

OPCIONES: tuple[Opcion, ...] = tuple(
    IDENTIDAD + RESPUESTA + CALIDAD + BAJAS + WHATSAPP_WEB + ESCALAMIENTO + VOZ + COORDINADOR + AGENT_OS
)
POR_CLAVE: dict[str, Opcion] = {o.clave: o for o in OPCIONES}
