"""Reglas de cada primer paso (HU-SH-06, ADR-0124).

Funciones puras sobre lo que `calculo.py` leyó del negocio: deciden el estado, el motivo (una
clave estable que el panel traduce) y, si el dato lo dice, cuándo se cumplió. Ningún paso se
marca a mano: si el dato cambia, el paso cambia.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from core.primeros_pasos.contrato import ContenidoCargado

Estado = Literal["pendiente", "en_curso", "completo"]
Motivo = Literal[
    "sin_descripcion",
    "sin_conexion",
    "sin_verificar",
    "con_problemas",
    "sin_terminar",
    "sin_habilidad",
    "sin_contenido",
    "sin_pruebas",
    "sin_invitaciones",
    "faltan_datos",
    "plan_insuficiente",
]

# Pasos del checklist, en el orden en que se muestran.
DATOS_NEGOCIO = "datos_negocio"
WHATSAPP = "whatsapp"
CATALOGO = "catalogo"
PROBAR_ASISTENTE = "probar_asistente"
EQUIPO = "equipo"
ACTIVAR_PRESET = "activar_preset"
PREFIJO_CONFIGURAR = "configurar_"
PASOS_FIJOS = (DATOS_NEGOCIO, WHATSAPP, CATALOGO, PROBAR_ASISTENTE, EQUIPO)

# Hitos del embudo que no son pasos del checklist: los bordes del recorrido de la HU, desde la
# invitación aceptada hasta el primer mensaje de un cliente real.
INVITACION_ACEPTADA = "invitacion_aceptada"
PRIMER_MENSAJE = "primer_mensaje"

# Secciones del RBAC (ADR-0047) con las que se actúa sobre cada paso.
SECCION_CONFIGURACION = "settings"
SECCION_CONEXIONES = "connections"
SECCION_HABILIDADES = "plugins"
SECCION_COLABORADORES = "collaborators"
SECCION_PLAN = "plan"

_ID_DE_PACK = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


@dataclass(frozen=True)
class Paso:
    clave: str
    estado: Estado
    motivo: Motivo | None = None
    # Cuándo se cumplió según el propio dato (la conexión, el primer producto). None = el dato no
    # lo dice; el hito toma la hora en que se detectó.
    completado_en: datetime | None = None
    cantidad: int | None = None
    # Sección del RBAC para actuar: el panel muestra la acción solo si el usuario puede usarla.
    seccion: str | None = None
    # Habilidad del paso (los de configurar y el de catálogo).
    pack: str | None = None
    tipo: str | None = None
    # Solo los pasos de una habilidad o de un tipo de negocio: textos de su catálogo (HU-FWK-04).
    titulo: str | None = None
    explicacion: str | None = None
    plan_requerido: str | None = None

    @property
    def completo(self) -> bool:
        return self.estado == "completo"


def _completo(clave: str, en: datetime | None, **extra: Any) -> Paso:
    return Paso(clave=clave, estado="completo", completado_en=en, **extra)


def _primero(fechas: Iterable[datetime | None]) -> datetime | None:
    return min((f for f in fechas if f is not None), default=None)


# ------------------------------------------------------------------ datos del negocio
def paso_datos_negocio(descripcion: str | None, actualizado_en: datetime | None) -> Paso:
    """Se cumple cuando el negocio se describió (lo que usa el asistente para presentarse).
    La hora es la de la última edición de la configuración: es la mejor cota que hay."""
    if (descripcion or "").strip():
        return _completo(DATOS_NEGOCIO, actualizado_en, seccion=SECCION_CONFIGURACION)
    return Paso(DATOS_NEGOCIO, "pendiente", motivo="sin_descripcion", seccion=SECCION_CONFIGURACION)


# ------------------------------------------------------------------ WhatsApp
@dataclass(frozen=True)
class ConexionMeta:
    status: str
    health: Mapping[str, Any] | None
    conectada_en: datetime | None


@dataclass(frozen=True)
class SesionWaweb:
    status: str
    conectada_en: datetime | None


def numero_verificado(health: Mapping[str, Any] | None) -> bool:
    """¿El último diagnóstico (HU-CONN-04) vio algún número conectado a la Cloud API?
    Con varios números el check es `number_registered:<phone_number_id>`."""
    for clave, valor in (health or {}).items():
        es_del_numero = clave == "number_registered" or clave.startswith("number_registered:")
        if es_del_numero and isinstance(valor, Mapping) and valor.get("ok") is True:
            return True
    return False


_WAWEB_A_MEDIAS = ("pending_qr", "connecting")


def paso_whatsapp(
    meta: Sequence[ConexionMeta], *, waweb: Sequence[SesionWaweb], manual_en: datetime | None
) -> Paso:
    """Conectado y verificado: una conexión de Meta `connected` cuyo diagnóstico ve el número,
    una sesión de WhatsApp Web vinculada o una conexión manual de Meta activa (se verifica
    contra Meta antes de guardarse). Una conexión desactivada (`revoked`) no cuenta."""
    verificadas = [c for c in meta if c.status == "connected" and numero_verificado(c.health)]
    if verificadas:
        return _completo(WHATSAPP, _primero(c.conectada_en for c in verificadas), seccion=SECCION_CONEXIONES)
    vinculadas = [s for s in waweb if s.status == "connected"]
    if vinculadas:
        return _completo(WHATSAPP, _primero(s.conectada_en for s in vinculadas), seccion=SECCION_CONEXIONES)
    if manual_en is not None:
        return _completo(WHATSAPP, manual_en, seccion=SECCION_CONEXIONES)

    estados = {c.status for c in meta} - {"revoked"}
    motivo: Motivo | None = None
    if "connected" in estados:
        motivo = "sin_verificar"
    elif estados & {"degraded", "error"}:
        motivo = "con_problemas"
    elif estados & {"pending", "draft"} or any(s.status in _WAWEB_A_MEDIAS for s in waweb):
        motivo = "sin_terminar"
    elif waweb:
        motivo = "con_problemas"  # desvinculada, con error o cerrada desde el teléfono
    if motivo is not None:
        return Paso(WHATSAPP, "en_curso", motivo=motivo, seccion=SECCION_CONEXIONES)
    return Paso(WHATSAPP, "pendiente", motivo="sin_conexion", seccion=SECCION_CONEXIONES)


# ------------------------------------------------------------------ catálogo o servicios
def paso_catalogo(contenidos: Sequence[tuple[str, ContenidoCargado]]) -> Paso:
    """Se cumple cuando alguna habilidad habilitada tiene contenido cargado (productos, tipos
    de cita, clientes). `contenidos` viene en el orden de las habilidades: sin contenido, el
    paso apunta a la primera. Sin ninguna habilidad que aporte contenido, a Habilidades."""
    if not contenidos:
        return Paso(CATALOGO, "pendiente", motivo="sin_habilidad", seccion=SECCION_HABILIDADES)
    con_algo = [(pack, c) for pack, c in contenidos if c.cantidad > 0]
    if con_algo:
        pack, principal = max(con_algo, key=lambda par: par[1].cantidad)
        return _completo(
            CATALOGO,
            _primero(c.primero_en for _, c in con_algo),
            cantidad=sum(c.cantidad for _, c in con_algo),
            seccion=principal.seccion,
            pack=pack,
            tipo=principal.tipo,
        )
    pack, primero = contenidos[0]
    return Paso(
        CATALOGO,
        "pendiente",
        motivo="sin_contenido",
        cantidad=0,
        seccion=primero.seccion,
        pack=pack,
        tipo=primero.tipo,
    )


# ------------------------------------------------------------------ probar el asistente y equipo
def paso_probar_asistente(primer_turno_en: datetime | None) -> Paso:
    """Se cumple con el primer turno que respondió el asistente: en «Probar asistente» o con un
    cliente real (el turno queda en `usage_ledger`)."""
    if primer_turno_en is not None:
        return _completo(PROBAR_ASISTENTE, primer_turno_en, seccion=SECCION_CONEXIONES)
    return Paso(PROBAR_ASISTENTE, "pendiente", motivo="sin_pruebas", seccion=SECCION_CONEXIONES)


def paso_equipo(primera_invitacion_en: datetime | None, primer_colaborador_en: datetime | None) -> Paso:
    """Se cumple con la primera invitación a un colaborador (o el primer colaborador dado de
    alta directo). La invitación del propio dueño no cuenta."""
    primera = _primero((primera_invitacion_en, primer_colaborador_en))
    if primera is not None:
        return _completo(EQUIPO, primera, seccion=SECCION_COLABORADORES)
    return Paso(EQUIPO, "pendiente", motivo="sin_invitaciones", seccion=SECCION_COLABORADORES)


# ------------------------------------------------------------------ habilidades pendientes de configurar
@dataclass(frozen=True)
class Habilidad:
    """Textos de la habilidad en su catálogo (HU-FWK-04): los escribe el pack, no el LLM."""

    nombre: str
    guia: str


def pasos_de_habilidades(
    pendientes: Sequence[Any], *, habilitadas: set[str], habilidades: Mapping[str, Habilidad]
) -> list[Paso]:
    """Un paso por habilidad que el preset dejó «pendiente de configurar» (ADR-0110 § 3:
    `tenants.settings["preset"]["pendientes"]`). Se cumple solo cuando la habilidad quedó
    habilitada (el negocio cargó sus claves). Lo que no es una habilidad instalada no se muestra."""
    pasos: list[Paso] = []
    vistos: set[str] = set()
    for pendiente in pendientes:
        pack = pendiente.get("pack") if isinstance(pendiente, Mapping) else None
        if not isinstance(pack, str) or not _ID_DE_PACK.match(pack) or pack in vistos:
            continue
        habilidad = habilidades.get(pack)
        if habilidad is None:
            continue
        vistos.add(pack)
        configurada = pack in habilitadas
        pasos.append(
            Paso(
                f"{PREFIJO_CONFIGURAR}{pack}",
                "completo" if configurada else "pendiente",
                motivo=None if configurada else "faltan_datos",
                seccion=SECCION_HABILIDADES,
                pack=pack,
                titulo=habilidad.nombre,
                explicacion=habilidad.guia,
            )
        )
    return pasos


# ------------------------------------------------------------------ tipo de negocio que espera un plan
@dataclass(frozen=True)
class RequisitoDePreset:
    nombre: str  # nombre del preset ("Comercio")
    plan_minimo: str  # nombre del plan que necesita ("Basic")
    alcanza: bool  # ¿el plan actual del negocio lo cubre?


def paso_activar_preset(
    alta_preset: Mapping[str, Any] | None,
    *,
    preset_aplicado_id: str | None,
    preset_aplicado_en: datetime | None,
    requisito: RequisitoDePreset | None,
    packs_ya_habilitados: bool = False,
) -> Paso | None:
    """El tipo de negocio que eligió el comprador quedó pendiente en el alta (ADR-0121 § 3.7)
    porque su plan no alcanza. Se cumple cuando el preset se aplica (HU-SH-04 lo hace al subir
    de plan). Si el plan ya alcanza y no se aplicó, no hay nada que el dueño pueda hacer desde
    el panel: no se muestra. Tampoco si las habilidades del preset ya están habilitadas (elegir
    la plantilla del asistente las habilita): no hay nada pendiente que esperar del plan."""
    if not isinstance(alta_preset, Mapping) or alta_preset.get("estado") != "pendiente":
        return None
    preset_id = alta_preset.get("id")
    if not preset_id:
        return None
    titulo = requisito.nombre if requisito else None
    if preset_aplicado_id == preset_id:
        return _completo(ACTIVAR_PRESET, preset_aplicado_en, seccion=SECCION_PLAN, titulo=titulo)
    if packs_ya_habilitados or requisito is None or requisito.alcanza:
        return None
    return Paso(
        ACTIVAR_PRESET,
        "pendiente",
        motivo="plan_insuficiente",
        seccion=SECCION_PLAN,
        titulo=titulo,
        plan_requerido=requisito.plan_minimo,
    )


# ------------------------------------------------------------------ medición
def segundos_desde_alta(alta_en: datetime, en: datetime) -> int:
    return max(0, int((en - alta_en).total_seconds()))


def contar(pasos: Sequence[Paso]) -> tuple[int, int]:
    """(completos, total)."""
    return sum(1 for p in pasos if p.completo), len(pasos)
