"""Asistente de configuración: preguntas por sección que se traducen a opciones del registro.

El dueño responde unas pocas preguntas en lenguaje de negocio («¿cómo quieres que suene?») y el
asistente propone valores para las opciones del registro (SPEC-37, ADR-0166). Es lógica pura: no lee
ni escribe nada. Quien lo llama pasa los valores actuales y aplica los cambios que el dueño confirmó.

Las preguntas, ayudas y títulos son texto de interfaz para personas y nunca entran al contexto del
LLM (invariante 3): igual que el resto del paquete, ningún módulo del runtime de turnos lo importa.

Reglas:
- Una pregunta sin respuesta no cambia nada.
- Lo propuesto se valida contra el registro (lista cerrada o rango) antes de salir.
- Con `aos_runtime = v3` la voz de la conexión sale de la release publicada, no de la persona que se
  edita aquí; las opciones de persona se omiten y la propuesta lo dice (no promete cambios inertes).
- Una opción cuya `depende_de` no se cumple en el estado resultante se omite y se dice por qué
  (por ejemplo, el modo «fuera de horario» cuando se elige atender siempre).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import DEBOUNCE_DELAY_MS
from core.config_registro.modelo import Dependencia
from core.config_registro.opciones import POR_CLAVE
from core.config_registro.validar import validar_valor

Seccion = Literal["voz", "atencion"]
Respuestas = Mapping[str, str | list[str]]

MOTIVO_VOZ_V3 = "La voz de este negocio se cambia al publicar una versión del agente, no desde aquí."


# ------------------------------------------------------------------------------------- el guion
class RespuestaPosible(BaseModel):
    """Una opción que el dueño puede elegir y lo que cambia en el registro."""

    model_config = ConfigDict(extra="forbid")

    id: str
    titulo: str
    ayuda: str = ""
    # clave del registro → valor propuesto (lista de textos en las preguntas «varias»).
    cambios: dict[str, Any]


class Pregunta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    titulo: str
    ayuda: str = ""
    tipo: Literal["una", "varias"]
    opciones: list[RespuestaPosible]

    @model_validator(mode="after")
    def _coherente(self) -> Pregunta:
        ids = [o.id for o in self.opciones]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.id}: ids de opción repetidos")
        for o in self.opciones:
            for clave, valor in o.cambios.items():
                opcion = POR_CLAVE.get(clave)
                if opcion is None or opcion.solo_lectura or opcion.editable_por != "dueno":
                    raise ValueError(f"{self.id}/{o.id}: «{clave}» no es una opción editable por el dueño")
                if self.tipo == "varias" and opcion.tipo != "lista_de_textos":
                    raise ValueError(f"{self.id}/{o.id}: una pregunta «varias» solo cambia listas de textos")
                validar_valor(clave, valor)
        return self

    @property
    def claves(self) -> set[str]:
        return {c for o in self.opciones for c in o.cambios}


class Guion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seccion: Seccion
    titulo: str
    ayuda: str = ""
    preguntas: list[Pregunta]


def _segundos(modo: str) -> int:
    return round(DEBOUNCE_DELAY_MS[modo] / 1000)


def _franjas(dias: list[int], desde: str, hasta: str) -> list[dict[str, Any]]:
    return [{"day": d, "from": desde, "to": hasta} for d in dias]


_VOZ = Guion(
    seccion="voz",
    titulo="Cómo habla tu asistente",
    ayuda="Cuatro preguntas para dejar el tono, el largo y las reglas de la voz. Después puedes ajustar cada detalle a mano.",
    preguntas=[
        Pregunta(
            id="tono",
            titulo="¿Cómo quieres que suene?",
            ayuda="Es la forma de hablar con tus clientes.",
            tipo="una",
            opciones=[
                RespuestaPosible(
                    id="cercano",
                    titulo="Cercano",
                    ayuda="«¡Hola! Cuéntame, ¿en qué te ayudo?»",
                    cambios={"persona_tone": "cercano", "persona_warmth": 4},
                ),
                RespuestaPosible(
                    id="profesional",
                    titulo="Profesional",
                    ayuda="«Buenas tardes. ¿En qué puedo ayudarle?»",
                    cambios={"persona_tone": "formal", "persona_warmth": 3},
                ),
                RespuestaPosible(
                    id="directo",
                    titulo="Directo",
                    ayuda="«Hola. Dime qué necesitas.»",
                    cambios={"persona_tone": "neutral", "persona_warmth": 2},
                ),
                RespuestaPosible(
                    id="relajado",
                    titulo="Relajado",
                    ayuda="«¡Ey! ¿Qué se te ofrece?»",
                    cambios={"persona_tone": "informal", "persona_warmth": 4},
                ),
            ],
        ),
        Pregunta(
            id="emojis",
            titulo="¿Usa emojis?",
            tipo="una",
            opciones=[
                RespuestaPosible(id="ninguno", titulo="Ninguno", ayuda="Nunca usa emojis.", cambios={"persona_emojis": "none"}),
                RespuestaPosible(id="pocos", titulo="Pocos", ayuda="Alguno de vez en cuando.", cambios={"persona_emojis": "sparse"}),
                RespuestaPosible(id="muchos", titulo="Muchos", ayuda="Los usa con naturalidad.", cambios={"persona_emojis": "free"}),
            ],
        ),
        Pregunta(
            id="largo",
            titulo="¿Qué tan largas deben ser sus respuestas?",
            ayuda="Las respuestas largas se parten en pocos mensajes para que se lean bien en el celular.",
            tipo="una",
            opciones=[
                RespuestaPosible(
                    id="cortas",
                    titulo="Cortas",
                    ayuda="Directas, hasta unos 300 caracteres.",
                    cambios={"persona_max_length": 300, "split_style": "un_mensaje"},
                ),
                RespuestaPosible(
                    id="normales",
                    titulo="Normales",
                    ayuda="Lo habitual, hasta unos 500 caracteres.",
                    cambios={"persona_max_length": 500, "split_style": "un_mensaje"},
                ),
                RespuestaPosible(
                    id="detalladas",
                    titulo="Detalladas",
                    ayuda="Explican con más detalle, hasta unos 900 caracteres, en pocos mensajes.",
                    cambios={"persona_max_length": 900, "split_style": "pocos_cortos"},
                ),
            ],
        ),
        Pregunta(
            id="prohibidas",
            titulo="¿Hay frases que nunca debe decir?",
            ayuda="Marca las que quieras evitar. Las frases que ya agregaste tú a mano se conservan.",
            tipo="varias",
            opciones=[
                RespuestaPosible(id=slug, titulo=f"«{frase}»", cambios={"persona_forbidden": [frase]})
                for slug, frase in (
                    ("garantizado", "garantizado"),
                    ("cien_seguro", "100% seguro"),
                    ("gratis", "gratis"),
                    ("sin_falta", "sin falta"),
                    ("prometo", "te lo prometo"),
                )
            ],
        ),
    ],
)

_MOTIVOS_DE_ESCALAR: tuple[tuple[str, str, list[str]], ...] = (
    ("molesto", "Cliente molesto", ["molesto", "enojado", "indignado", "pésimo servicio"]),
    ("persona", "Pide hablar con alguien", ["asesor", "humano", "persona", "hablar con alguien"]),
    ("reclamo", "Reclamos", ["reclamo", "queja", "devolución", "garantía"]),
    ("pago", "Problemas de pago", ["me cobraron", "cobro doble", "pago no llegó", "reembolso"]),
    ("grande", "Pedidos grandes", ["al por mayor", "mayorista", "pedido grande", "cotización grande"]),
)

_ATENCION = Guion(
    seccion="atencion",
    titulo="Cuándo y cómo atiende",
    ayuda="Cinco preguntas para dejar el horario, la velocidad y cuándo pasa con una persona. Después puedes ajustar cada detalle a mano.",
    preguntas=[
        Pregunta(
            id="horario",
            titulo="¿Cuándo atiende tu equipo?",
            ayuda="Si eliges «Siempre», el asistente responde a toda hora y no se guarda ningún horario. Las horas exactas las afinas después.",
            tipo="una",
            opciones=[
                RespuestaPosible(
                    id="semana",
                    titulo="Lunes a viernes, 8:00 a 18:00",
                    cambios={"business_ranges": _franjas([0, 1, 2, 3, 4], "08:00", "18:00")},
                ),
                RespuestaPosible(
                    id="semana_sabado",
                    titulo="Lunes a sábado, 8:00 a 18:00",
                    cambios={"business_ranges": _franjas([0, 1, 2, 3, 4, 5], "08:00", "18:00")},
                ),
                RespuestaPosible(
                    id="todos",
                    titulo="Todos los días, 8:00 a 18:00",
                    cambios={"business_ranges": _franjas([0, 1, 2, 3, 4, 5, 6], "08:00", "18:00")},
                ),
                RespuestaPosible(
                    id="siempre",
                    titulo="Siempre, sin horario",
                    ayuda="El asistente responde a toda hora.",
                    cambios={"business_ranges": []},
                ),
            ],
        ),
        Pregunta(
            id="fuera_de_horario",
            titulo="¿Qué hace cuando escriben fuera de horario?",
            ayuda="Solo aplica si definiste un horario.",
            tipo="una",
            opciones=[
                RespuestaPosible(
                    id="responde",
                    titulo="Responde igual",
                    ayuda="Atiende a toda hora y le cuenta al cliente cuándo atiende tu equipo.",
                    cambios={"business_hours_mode": "informativo"},
                ),
                RespuestaPosible(
                    id="avisa",
                    titulo="Avisa y deja la conversación para después",
                    ayuda="Envía el aviso de fuera de horario y no responde hasta que vuelva el horario.",
                    # Vacío = el aviso por defecto, que sigue el idioma del negocio.
                    cambios={"business_hours_mode": "bloquea", "out_of_hours_message": ""},
                ),
            ],
        ),
        Pregunta(
            id="velocidad",
            titulo="¿Qué tan rápido responde?",
            ayuda="Espera a que el cliente termine de escribir. Esperar más junta varios mensajes seguidos en una sola respuesta.",
            tipo="una",
            opciones=[
                RespuestaPosible(
                    id="rapida",
                    titulo="Rápida",
                    ayuda=f"Espera unos {_segundos('rapido')} segundos.",
                    cambios={"debounce_mode": "rapido"},
                ),
                RespuestaPosible(
                    id="natural",
                    titulo="Natural",
                    ayuda=f"Espera unos {_segundos('natural')} segundos, como una persona.",
                    cambios={"debounce_mode": "natural"},
                ),
                RespuestaPosible(
                    id="pausada",
                    titulo="Pausada",
                    ayuda=f"Espera unos {_segundos('pausado')} segundos. Útil si tus clientes escriben en trozos.",
                    cambios={"debounce_mode": "pausado"},
                ),
            ],
        ),
        Pregunta(
            id="escalar_motivos",
            titulo="¿Cuándo debe pasar la conversación a una persona?",
            ayuda="Si el cliente escribe algo parecido, el asistente avisa a tu equipo. Las palabras que ya agregaste tú a mano se conservan.",
            tipo="varias",
            opciones=[
                RespuestaPosible(id=slug, titulo=titulo, cambios={"escalation_keywords": palabras})
                for slug, titulo, palabras in _MOTIVOS_DE_ESCALAR
            ],
        ),
        Pregunta(
            id="escalar_turnos",
            titulo="Si no logra resolver una conversación, ¿pasa a una persona?",
            tipo="una",
            opciones=[
                RespuestaPosible(
                    id="nunca",
                    titulo="Solo si el cliente lo pide",
                    cambios={"auto_escalate_after_turns": 0},
                ),
                RespuestaPosible(
                    id="cinco",
                    titulo="Después de 5 turnos sin resolver",
                    cambios={"auto_escalate_after_turns": 5},
                ),
                RespuestaPosible(
                    id="diez",
                    titulo="Después de 10 turnos sin resolver",
                    cambios={"auto_escalate_after_turns": 10},
                ),
            ],
        ),
    ],
)

GUIONES: dict[str, Guion] = {g.seccion: g for g in (_VOZ, _ATENCION)}


def _es_de_voz(clave: str) -> bool:
    """Las que viven en la persona del AgentSpec v2 (con `aos_runtime = v3` no surten efecto)."""
    return POR_CLAVE[clave].almacen.startswith("spec:persona.")


def guiones_para(aos_runtime: str) -> list[Guion]:
    """El guion que ve un negocio: con `v3` sin las preguntas que solo cambian la voz."""
    if aos_runtime != "v3":
        return [g.model_copy(deep=True) for g in GUIONES.values()]
    visibles: list[Guion] = []
    for g in GUIONES.values():
        preguntas = [p.model_copy(deep=True) for p in g.preguntas if not all(_es_de_voz(c) for c in p.claves)]
        visibles.append(g.model_copy(update={"preguntas": preguntas}, deep=True))
    return visibles


# ------------------------------------------------------------------------------- la propuesta
class RespuestasInvalidas(ValueError):
    """Respuestas fuera del guion: un mensaje por pregunta (id → texto para el dueño)."""

    def __init__(self, por_pregunta: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in por_pregunta.items()))
        self.por_pregunta = por_pregunta


class Cambio(BaseModel):
    clave: str
    etiqueta: str
    actual: Any = None
    propuesto: Any = None
    # El valor actual no es el que trae el sistema: el dueño lo tocó antes.
    editado_a_mano: bool = False


class Omitido(BaseModel):
    clave: str
    etiqueta: str
    motivo: str


class Propuesta(BaseModel):
    seccion: Seccion
    cambios: list[Cambio] = Field(default_factory=list)
    omitidos: list[Omitido] = Field(default_factory=list)
    # Huella de lo propuesto: `aplicar` la exige para no escribir algo distinto de lo que se vio.
    huella: str = ""


def _normalizar(valor: Any) -> Any:
    if isinstance(valor, list):
        return [_normalizar(v) for v in valor]
    if isinstance(valor, dict):
        return {k: _normalizar(v) for k, v in sorted(valor.items())}
    return valor


def _cumple(dep: Dependencia, estado: Mapping[str, Any]) -> bool:
    valor = estado.get(dep.clave)
    if dep.condicion == "no_vacia":
        return bool(valor)
    if dep.condicion == "verdadera":
        return valor is True
    return str(valor) == dep.valor


def _huella(seccion: str, cambios: list[Cambio]) -> str:
    cuerpo = json.dumps(
        [(c.clave, _normalizar(c.propuesto)) for c in cambios] + [seccion], sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(cuerpo.encode()).hexdigest()[:16]


def _validar_respuestas(guion: Guion, respuestas: Respuestas) -> None:
    por_id = {p.id: p for p in guion.preguntas}
    errores: dict[str, str] = {}
    for clave, valor in respuestas.items():
        pregunta = por_id.get(clave)
        if pregunta is None:
            errores[str(clave)] = "Esta pregunta no existe en esta sección."
            continue
        ids = {o.id for o in pregunta.opciones}
        if pregunta.tipo == "una":
            if not isinstance(valor, str) or valor not in ids:
                errores[clave] = "Elige una de las opciones."
        elif not isinstance(valor, list) or not all(isinstance(v, str) and v in ids for v in valor):
            errores[clave] = "Elige solo entre las opciones que se muestran."
    if errores:
        raise RespuestasInvalidas(errores)


def _lista_con_extras(pregunta: Pregunta, clave: str, elegidas: list[str], actual: Mapping[str, Any]) -> list[str]:
    """Lo que ya había y no es de este guion (lo escribió el dueño) más lo elegido, sin repetir."""
    conocidas = {str(v).lower() for o in pregunta.opciones for v in o.cambios.get(clave, [])}
    propias = [str(v) for v in (actual.get(clave) or []) if str(v).lower() not in conocidas]
    elegidas_vistas = [v for o in pregunta.opciones if o.id in elegidas for v in o.cambios.get(clave, [])]
    resultado: list[str] = []
    for valor in [*propias, *elegidas_vistas]:
        if valor.lower() not in {r.lower() for r in resultado}:
            resultado.append(valor)
    return resultado


def resolver(
    seccion: str,
    respuestas: Respuestas,
    actual: Mapping[str, Any],
    *,
    aos_runtime: str = "legacy",
) -> Propuesta:
    """Traduce las `respuestas` (`id de pregunta` → id de opción, o lista de ids) a cambios sobre
    los valores `actual`es (clave del registro → valor). No toca nada: devuelve la propuesta."""
    guion = GUIONES.get(seccion)
    if guion is None:
        raise ValueError(f"Sección desconocida: «{seccion}».")
    _validar_respuestas(guion, respuestas)

    propuestos: dict[str, Any] = {}
    for pregunta in guion.preguntas:
        if pregunta.id not in respuestas:
            continue
        valor = respuestas[pregunta.id]
        if pregunta.tipo == "una":
            propuestos.update(next(o for o in pregunta.opciones if o.id == valor).cambios)
        else:
            for clave in pregunta.claves:
                propuestos[clave] = _lista_con_extras(pregunta, clave, list(valor), actual)

    omitidos: list[Omitido] = []
    if aos_runtime == "v3":
        for clave in [c for c in propuestos if _es_de_voz(c)]:
            omitidos.append(Omitido(clave=clave, etiqueta=POR_CLAVE[clave].etiqueta, motivo=MOTIVO_VOZ_V3))
            del propuestos[clave]

    estado = {**actual, **propuestos}
    for clave in [c for c in propuestos if POR_CLAVE[c].depende_de]:
        dep = POR_CLAVE[clave].depende_de
        if dep is not None and not _cumple(dep, estado):
            omitidos.append(Omitido(clave=clave, etiqueta=POR_CLAVE[clave].etiqueta, motivo=dep.motivo))
            del propuestos[clave]

    cambios: list[Cambio] = []
    for clave, nuevo in propuestos.items():
        opcion = POR_CLAVE[clave]
        antes = actual.get(clave)
        if _normalizar(antes) == _normalizar(nuevo):
            continue
        cambios.append(
            Cambio(
                clave=clave,
                etiqueta=opcion.etiqueta,
                actual=antes,
                propuesto=nuevo,
                editado_a_mano=_normalizar(antes) != _normalizar(opcion.por_defecto),
            )
        )
    return Propuesta(seccion=guion.seccion, cambios=cambios, omitidos=omitidos, huella=_huella(guion.seccion, cambios))
