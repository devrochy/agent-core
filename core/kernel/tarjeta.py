"""Tarjeta de estado de la conversación (ADR-0114 §2, HU-AOS-04).

Lo que el modelo ve en cada turno sobre las tareas: la que está en foco, las que están en
pausa, los datos que ya dio el cliente (con su origen), lo que falta, lo que espera
confirmación y las acciones de la etapa.

- Va en el bloque dinámico del prompt, justo antes de la fecha y hora, que sigue siendo lo
  último (ADR-0091); los bloques estables no cambian y la caché del prefijo se conserva.
- Los rótulos e instrucciones viven en el playbook del kernel `estado-conversacion`
  (invariante 3); este módulo solo serializa.
- Los valores son dato (invariante 7): un dato del cliente o el título de un procedimiento
  (que puede escribir el negocio) va siempre como cadena JSON, con comillas y escapes, y además
  se escapan los separadores de línea Unicode y los caracteres de control o de formato. Los
  nombres (dato, etapa, acción) se validan con los patrones del contrato y lo que no cumple se
  descarta. Así un valor nunca abre una línea ni un encabezado nuevo del prompt.
- Determinista: no lleva horas ni nada que cambie entre corridas con el mismo estado.
- Solo con `tenants.aos_procedures` encendida. Apagada, la tarjeta es "" y el prompt es
  idéntico al de siempre. La arma el motor al empezar el turno
  (`core/kernel/procedures/ejecucion.contexto_del_motor`, HU-AOS-05), con las definiciones de
  los procedimientos (título, etapas, datos requeridos y acciones).
- Con tarea en foco, una línea dice qué falta para salir de la etapa (`para_avanzar`, adenda
  2026-10-12 de ADR-0113, `motor.faltas_de_salida`): si el motor sabe por qué no avanza, el modelo
  también, y no inventa que le falta una herramienta.
- Con tarea en foco, otra línea dice qué datos pedir AHORA (`pide_ahora`, adenda 2026-10-13 de
  ADR-0113, `motor.siguientes_datos`): todos los que faltan, en el orden del procedimiento y con
  el rótulo de la habilidad, para pedirlos en UN mensaje como lista numerada (el cliente los
  puede mandar juntos); si ya dio una parte, solo lo que falta. Si la etapa no espera datos del
  cliente, `no_pidas_datos`.
- Sin tarea en foco y con procedimientos que iniciar, el motor le agrega la pista de iniciar el
  recorrido (`pista_sin_foco`, adenda 2026-10-12 de ADR-0113): la tarjeta deja de ser "" aunque no
  haya tareas.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import Any

from agentcore_contracts.procedure import Procedure, Slot

from core.kernel.state import Proyeccion
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "estado-conversacion"
CLAVES = {
    "encabezado": (),
    "aviso": (),
    "en_foco": ("procedimiento", "version", "etapa"),
    "en_pausa": ("procedimiento", "version", "etapa"),
    "etapa_con_posicion": ("posicion", "total", "etapa"),
    "dato": ("dato", "valor", "origen"),
    "dato_confirmado": ("dato", "valor", "origen"),
    "dato_falta": ("dato",),
    "por_confirmar": ("objetivo",),
    "objetivo_etapa": ("nombre",),
    "objetivo_dato": ("nombre",),
    "objetivo_accion": ("nombre",),
    "origen_accion": ("nombre",),
    "origen_boton": (),
    "origen_contenido": ("nombre",),
    "origen_cliente": (),
    "origen_persona": (),
    "acciones": ("acciones",),
    "mas_en_pausa": ("cantidad",),
    "sin_foco": (),
    "sin_foco_acciones": ("acciones",),
    # Qué bloquea la salida de la etapa (adenda 2026-10-12 de ADR-0113).
    "para_avanzar": ("faltas",),
    "falta_dato_cliente": ("nombre",),
    "falta_dato_boton": ("nombre",),
    "falta_dato_contenido": ("nombre",),
    "falta_dato_accion": ("nombre",),
    "falta_confirmar_dato": ("nombre",),
    "falta_confirmar_etapa": ("nombre",),
    "falta_accion": ("nombre",),
    # La acción de salida que exige confirmación (smoke de test 2026-10-20): la confirmación la
    # pide el sistema con su resumen, no el modelo.
    "falta_accion_confirmable": ("nombre",),
    # Qué pedir ahora (adenda 2026-10-13 de ADR-0113).
    "pide_ahora": ("datos",),
    "dato_a_pedir": ("numero", "dato", "rotulo"),
    "no_pidas_datos": (),
    # Pila de tareas (HU-AOS-20, adenda de ADR-0116): solo aparecen con tareas en pausa.
    "dato_falta_en_pausa": ("dato",),
    "guia_pila": (),
    "sobrantes": ("procedimientos",),
    "retomada_de_otro": ("agente",),
}
# Tope de tareas en pausa que se pintan: `procedures.maxStack - 1` de la release (el de abajo es
# el del contrato, 2, para quien no lo pasa).
MAX_EN_PAUSA = 1
MAX_VALOR_CHARS = 200
MAX_TARJETA_CHARS = 2000

_SECCION = re.compile(r"^## ([a-z_]+)$")
_NOMBRE = re.compile(r"^[a-z0-9_]{1,40}$")
_ACCION = re.compile(r"^[a-zA-Z0-9_]{1,80}$")
_SEMVER = re.compile(r"^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$")
# Control, formato (bidi, ancho cero, etiquetas), separadores de línea, privados y sustitutos;
# además las comillas tipográficas (Pi/Pf), que el modelo podría leer como cierre del delimitador.
_ESCAPAR = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Pi", "Pf"})
# Selectores de variación (Mn): sirven para esconder texto que el modelo sí lee.
_SELECTORES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0x180B, 0x180F))

ResolverProcedimiento = Callable[[str, str], Procedure | None]
# Rótulos humanos de los datos de un procedimiento (`MCPHost.register_rotulos` de su habilidad).
RotulosDe = Callable[[Procedure], Mapping[str, str]]


class RotulosInvalidos(ValueError):
    """El playbook de rótulos no tiene todas las claves o una plantilla no encaja."""


def parsear_rotulos(contenido: str) -> dict[str, str]:
    """Secciones `## clave` del playbook → plantilla. Exige todas las claves y sus campos."""
    rotulos: dict[str, list[str]] = {}
    actual: str | None = None
    for linea in contenido.splitlines():
        m = _SECCION.match(linea)
        if m:
            actual = str(m.group(1))
            rotulos[actual] = []
        elif actual is not None:
            rotulos[actual].append(linea)
    plantillas = {clave: "\n".join(lineas).strip("\n") for clave, lineas in rotulos.items()}
    faltan = sorted(set(CLAVES) - set(plantillas))
    if faltan:
        raise RotulosInvalidos(f"faltan rótulos: {faltan}")
    for clave, campos in CLAVES.items():
        plantilla = plantillas[clave]
        if not plantilla or "\n" in plantilla:
            raise RotulosInvalidos(f"el rótulo {clave} debe ser una sola línea")
        try:
            plantilla.format(**{c: "x" for c in campos})
        except (KeyError, IndexError, ValueError) as exc:
            raise RotulosInvalidos(f"el rótulo {clave} usa un campo desconocido: {exc}") from exc
    return plantillas


@cache
def rotulos() -> dict[str, str]:
    """Rótulos del playbook del kernel (fail-closed: si el archivo está roto, lanza)."""
    from core.agents.playbooks import load_core_playbook

    return parsear_rotulos(load_core_playbook(PLAYBOOK))


def _rotulos_del_playbook() -> dict[str, str]:
    return rotulos()


# ---------------------------------------------------------------- serialización


def escapar(valor: Any) -> str:
    """Un valor como cadena JSON de una sola línea, delimitada y escapada (invariante 7)."""
    if isinstance(valor, bool | int | float) or valor is None:
        texto = json.dumps(valor)
    else:
        if not isinstance(valor, str):
            valor = json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)
        if len(valor) > MAX_VALOR_CHARS:
            valor = valor[:MAX_VALOR_CHARS] + "…"
        texto = json.dumps(valor, ensure_ascii=False)
    return "".join(_escapar_caracter(c) for c in texto)


def _escapar_caracter(c: str) -> str:
    codigo = ord(c)
    if unicodedata.category(c) not in _ESCAPAR and not any(a <= codigo <= b for a, b in _SELECTORES):
        return c
    if codigo <= 0xFFFF:
        return f"\\u{codigo:04x}"
    alto, bajo = divmod(codigo - 0x10000, 0x400)  # par sustituto: sigue siendo JSON válido
    return f"\\u{0xD800 + alto:04x}\\u{0xDC00 + bajo:04x}"


def _origen(r: Mapping[str, str], fuente: Any) -> str | None:
    if not isinstance(fuente, str):
        return None
    if fuente.startswith("action:") and _ACCION.fullmatch(fuente[7:]):
        return r["origen_accion"].format(nombre=fuente[7:])
    if fuente.startswith("interactive:"):
        return r["origen_boton"]
    if fuente.startswith("content:") and _NOMBRE.fullmatch(fuente[8:]):
        return r["origen_contenido"].format(nombre=fuente[8:])
    if fuente == "llm":
        return r["origen_cliente"]
    if fuente == "human":
        return r["origen_persona"]
    return None


def _requerido(slot: Slot, valores: Mapping[str, Any]) -> bool:
    if slot.required:
        return True
    cond = slot.requiredWhen
    return cond is not None and valores.get(cond.slot) == cond.equals


def _lineas_de_tarea(
    r: Mapping[str, str],
    tarea: Proyeccion,
    proc: Procedure | None,
    *,
    en_foco: bool,
    rotulos: Mapping[str, str] | None = None,
    instrucciones: bool = True,
) -> list[str]:
    etapa = tarea.stage if _NOMBRE.fullmatch(tarea.stage or "") else "?"
    titulo = proc.title if proc is not None else tarea.procedure_id
    etapa_txt = etapa
    if proc is not None:
        ids = [s.id for s in proc.stages]
        if etapa in ids:
            etapa_txt = r["etapa_con_posicion"].format(posicion=ids.index(etapa) + 1, total=len(ids), etapa=etapa)
    version = tarea.procedure_version if _SEMVER.fullmatch(tarea.procedure_version or "") else "?"
    lineas = [
        r["en_foco" if en_foco else "en_pausa"].format(procedimiento=escapar(titulo), version=version, etapa=etapa_txt)
    ]

    definidos = list(proc.slots) if proc is not None else []
    orden = definidos + sorted(n for n in tarea.slots if n not in definidos)
    valores = {n: (m or {}).get("value") for n, m in tarea.slots.items()}
    for nombre in orden:
        if not _NOMBRE.fullmatch(nombre):
            log.warning("state_card_slot_discarded", procedure_id=tarea.procedure_id)
            continue
        meta = tarea.slots.get(nombre)
        if isinstance(meta, Mapping) and meta.get("value") is not None:
            if meta.get("data_class") == "payment" and not en_foco:
                continue
            clave = "dato_confirmado" if meta.get("confirmed") is True else "dato"
            origen = _origen(r, meta.get("source")) or "?"
            # Un nombre que el procedimiento no declara lo eligió quien fijó el dato: va escapado
            # como cualquier valor, para que no cuele una orden fuera de las comillas.
            dato = nombre if nombre in definidos else escapar(nombre)
            lineas.append(r[clave].format(dato=dato, valor=escapar(meta["value"]), origen=origen))
        elif proc is not None and nombre in proc.slots and _requerido(proc.slots[nombre], valores):
            # En pausa se dice qué falta, pero no se pide todavía (una sola pregunta, la del foco).
            lineas.append(r["dato_falta" if en_foco and instrucciones else "dato_falta_en_pausa"].format(dato=nombre))

    pendiente = tarea.pending_confirmation
    if en_foco and instrucciones and isinstance(pendiente, Mapping):
        clase, nombre = pendiente.get("kind"), pendiente.get("name")
        patron = _ACCION if clase == "action" else _NOMBRE
        rotulo = {"stage": "objetivo_etapa", "slot": "objetivo_dato", "action": "objetivo_accion"}.get(str(clase))
        if rotulo and isinstance(nombre, str) and patron.fullmatch(nombre):
            lineas.append(r["por_confirmar"].format(objetivo=r[rotulo].format(nombre=nombre)))

    if en_foco and instrucciones and proc is not None:
        etapa_def = next((s for s in proc.stages if s.id == etapa), None)
        if etapa_def is not None:
            pedir = describir_lo_que_se_pide(proc, etapa_def, tarea, r, rotulos)
            faltas = describir_faltas(proc, etapa_def, tarea, r)
            # Con datos que pedir, la lista numerada ya dice qué falta (adenda 2026-10-13).
            if faltas and (pedir is None or pedir == r["no_pidas_datos"]):
                lineas.append(r["para_avanzar"].format(faltas=faltas))
            if pedir is not None:
                lineas.append(pedir)
        acciones = [a for a in (etapa_def.actions if etapa_def else []) if _ACCION.fullmatch(a)]
        if acciones:
            lineas.append(r["acciones"].format(acciones=", ".join(acciones)))
    return lineas


def describir_faltas(proc: Procedure, stage: Any, tarea: Proyeccion, r: Mapping[str, str] | None = None) -> str:
    """Lo que falta para salir de la etapa (`motor.faltas_de_salida`) en una línea, con los
    rótulos del playbook; "" si no falta nada. Solo nombres validados (dato, etapa, acción)."""
    from core.kernel.procedures.motor import faltas_de_salida, requiere_confirmacion

    r = r or rotulos()
    piezas: list[str] = []
    for falta in faltas_de_salida(proc, stage, tarea):
        patron = _ACCION if falta.clase == "accion" else _NOMBRE
        clave = f"falta_{falta.clase}"
        if falta.clase == "accion" and tarea.pending_confirmation is None and requiere_confirmacion(stage, falta.nombre):
            clave = "falta_accion_confirmable"
        if clave in r and patron.fullmatch(falta.nombre):
            piezas.append(r[clave].format(nombre=falta.nombre))
    return "; ".join(piezas)


def describir_lo_que_se_pide(
    proc: Procedure,
    stage: Any,
    tarea: Proyeccion,
    r: Mapping[str, str] | None = None,
    rotulos: Mapping[str, str] | None = None,
) -> str | None:
    """La línea `pide_ahora` (los datos que faltan, en orden, numerados, con su rótulo escapado
    como dato) o `no_pidas_datos` si la etapa espera otra cosa; None en una etapa sin salida.
    Solo nombres validados."""
    from core.kernel.procedures.motor import siguientes_datos

    if stage.exitWhen is None:
        return None
    r = r or _rotulos_del_playbook()
    datos = [n for n in siguientes_datos(proc, stage, tarea) if _NOMBRE.fullmatch(n)]
    if not datos:
        return r["no_pidas_datos"]
    piezas = [
        r["dato_a_pedir"].format(
            numero=i, dato=n, rotulo=escapar((rotulos or {}).get(n) or n.replace("_", " "))
        )
        for i, n in enumerate(datos, start=1)
    ]
    return r["pide_ahora"].format(datos="; ".join(piezas))


def renderizar_tarjeta(
    tareas: Sequence[Proyeccion],
    *,
    r: Mapping[str, str] | None = None,
    procedimientos: ResolverProcedimiento | None = None,
    max_chars: int = MAX_TARJETA_CHARS,
    rotulos_de: RotulosDe | None = None,
    max_en_pausa: int = MAX_EN_PAUSA,
    sobrantes: Sequence[str] = (),
    foco_de_otro: str | None = None,
) -> str:
    """La tarjeta para el prompt, o "" si no hay tareas abiertas ni sobrantes. Pura.

    Con tope de tamaño: se quitan líneas o tareas ENTERAS (primero las en pausa), nunca se
    corta un valor por la mitad. Pila de tareas (HU-AOS-20): con tareas en pausa sale la guía de
    una sola pregunta; `sobrantes` son los títulos de lo que el cliente pidió y no cupo; con
    `foco_de_otro` (la tarea en foco la sigue otro agente) no salen instrucciones de esa tarea.
    """
    r = r or rotulos()
    abiertas = [t for t in tareas if t.status in ("active", "suspended") and t.parent_task_id is None]
    if not abiertas and not sobrantes:
        return ""
    foco = next((t for t in abiertas if t.status == "active" and t.stack_position == 0), None)
    pausa = sorted(
        (t for t in abiertas if t is not foco), key=lambda t: (t.stack_position, t.created_at)
    )

    def proc(t: Proyeccion) -> Procedure | None:
        return procedimientos(t.procedure_id, t.procedure_version) if procedimientos else None

    salida = [r["encabezado"], r["aviso"]]
    usado = sum(len(linea) + 1 for linea in salida)
    if foco is not None:
        proc_foco = proc(foco)
        propios = rotulos_de(proc_foco) if rotulos_de is not None and proc_foco is not None else None
        lineas = _lineas_de_tarea(r, foco, proc_foco, en_foco=True, rotulos=propios, instrucciones=foco_de_otro is None)
        if foco_de_otro is not None:
            lineas.append(r["retomada_de_otro"].format(agente=escapar(foco_de_otro)))
        # Si no cabe todo, primero las instrucciones (líneas sin sangría: qué falta, qué pedir,
        # acciones) y después los datos, sin cambiar el orden.
        elegidas: set[int] = set()
        for prioridad in (False, True):
            for i, linea in enumerate(lineas):
                if linea.startswith(" ") is prioridad and usado + len(linea) + 1 <= max_chars:
                    elegidas.add(i)
                    usado += len(linea) + 1
        salida.extend(linea for i, linea in enumerate(lineas) if i in elegidas)
    tope = max(0, max_en_pausa)
    if pausa and foco is not None and foco_de_otro is None:
        guia = r["guia_pila"]
        salida.append(guia)
        usado += len(guia) + 1
    omitidas = max(0, len(pausa) - tope)
    for tarea in pausa[:tope]:
        bloque = _lineas_de_tarea(r, tarea, proc(tarea), en_foco=False)
        largo = sum(len(linea) + 1 for linea in bloque)
        if usado + largo > max_chars:
            omitidas += 1
            continue
        salida.extend(bloque)
        usado += largo
    if omitidas:
        salida.append(r["mas_en_pausa"].format(cantidad=omitidas))
    if sobrantes:
        salida.append(r["sobrantes"].format(procedimientos=", ".join(escapar(t) for t in sobrantes)))
    return "\n".join(salida)


def pista_sin_foco(tarjeta: str, acciones: Sequence[str], r: Mapping[str, str] | None = None) -> str:
    """La tarjeta con la pista de iniciar el recorrido (adenda 2026-10-12 de ADR-0113): con el motor
    activo, sin tarea en foco y con procedimientos que iniciar. Sin tareas la tarjeta es solo el
    encabezado y la pista; con tareas en pausa, la pista va al final sin repetir el encabezado.
    `acciones` son las puertas que el negocio tiene como tools; se descartan los nombres que no
    cumplen el patrón de acción. Pura."""
    r = r or rotulos()
    lineas = [tarjeta] if tarjeta else [r["encabezado"]]
    lineas.append(r["sin_foco"])
    validas = [a for a in acciones if _ACCION.fullmatch(a)]
    if validas:
        lineas.append(r["sin_foco_acciones"].format(acciones=", ".join(validas)))
    return "\n".join(lineas)
