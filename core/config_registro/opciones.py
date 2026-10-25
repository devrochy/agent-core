"""Las 31 opciones del primer recorte de SPEC-37: Identidad y comportamiento.

Cada `por_defecto`, rango y lista de valores sale de la constante del runtime que ya los usa
(`DEBOUNCE_DELAY_MS`, `CAPS`, `TOGGLES`, los umbrales de dificultad, la política de WhatsApp
Web…): el registro las referencia, no las copia. Las etiquetas y explicaciones son texto para
personas y no entran a ningún prompt.
"""

from __future__ import annotations

from typing import Any, get_args

from agentcore_contracts import Persona

from core.admin.envios_segmento import PALABRAS_DE_BAJA_POR_DEFECTO
from core.agents.spec import DEFAULT_SPEC
from core.config import DEBOUNCE_DELAY_MS, Settings
from core.config_registro.modelo import Dependencia, Opcion, ValorPosible
from core.db.models import DEFAULT_ASSISTANT_NAME
from core.gateway.message_splitter import CAPS
from core.kernel.fuera_de_horario import IDIOMA_POR_DEFECTO, IDIOMAS
from core.llm.difficulty_router import (
    DIFFICULTY_SCORE_THRESHOLD,
    LONG_MESSAGE_THRESHOLD,
    TURNOS_SIN_RESOLVER_THRESHOLD,
)
from core.llm.prompt_builder import DEFAULT_TIMEZONE
from core.orchestrator.escalation import DEFAULT_ESCALATION_KEYWORDS
from core.orchestrator.guardrails import MAX_GUARDRAIL_RETRIES
from core.waweb.policy import (
    _CAP_BOUNDS,
    _MAX_DELAY_BOUNDS,
    _MIN_DELAY_BOUNDS,
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
        minimo=0,
        maximo=5,
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
        minimo=50,
        maximo=5000,
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
        minimo=1,
        maximo=20,
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
        minimo=1,
        maximo=10,
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
        minimo=_MIN_DELAY_BOUNDS[0],
        maximo=_MIN_DELAY_BOUNDS[1],
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
        minimo=_MAX_DELAY_BOUNDS[0],
        maximo=_MAX_DELAY_BOUNDS[1],
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
        minimo=_CAP_BOUNDS[0],
        maximo=_CAP_BOUNDS[1],
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
        minimo=0,
        maximo=1000,
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
        por_defecto=DEFAULT_TIMEZONE,
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
        minimo=0,
        maximo=5,
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
        minimo=0,
        maximo=10000,
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

OPCIONES: tuple[Opcion, ...] = tuple(
    IDENTIDAD + RESPUESTA + CALIDAD + BAJAS + WHATSAPP_WEB + ESCALAMIENTO + VOZ
)
POR_CLAVE: dict[str, Opcion] = {o.clave: o for o in OPCIONES}
