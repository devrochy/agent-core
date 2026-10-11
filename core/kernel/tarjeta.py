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
  idéntico al de siempre.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import Any

from agentcore_contracts.procedure import Procedure, Slot

from core.kernel.state import Proyeccion, proyeccion_de
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
}
MAX_EN_PAUSA = 3          # maxStack por defecto (KernelSpec, HU-AOS-12)
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
    r: Mapping[str, str], tarea: Proyeccion, proc: Procedure | None, *, en_foco: bool
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
        elif proc is not None and en_foco and nombre in proc.slots and _requerido(proc.slots[nombre], valores):
            lineas.append(r["dato_falta"].format(dato=nombre))

    pendiente = tarea.pending_confirmation
    if en_foco and isinstance(pendiente, Mapping):
        clase, nombre = pendiente.get("kind"), pendiente.get("name")
        patron = _ACCION if clase == "action" else _NOMBRE
        rotulo = {"stage": "objetivo_etapa", "slot": "objetivo_dato", "action": "objetivo_accion"}.get(str(clase))
        if rotulo and isinstance(nombre, str) and patron.fullmatch(nombre):
            lineas.append(r["por_confirmar"].format(objetivo=r[rotulo].format(nombre=nombre)))

    if en_foco and proc is not None:
        etapa_def = next((s for s in proc.stages if s.id == etapa), None)
        acciones = [a for a in (etapa_def.actions if etapa_def else []) if _ACCION.fullmatch(a)]
        if acciones:
            lineas.append(r["acciones"].format(acciones=", ".join(acciones)))
    return lineas


def renderizar_tarjeta(
    tareas: Sequence[Proyeccion],
    *,
    r: Mapping[str, str] | None = None,
    procedimientos: ResolverProcedimiento | None = None,
    max_chars: int = MAX_TARJETA_CHARS,
) -> str:
    """La tarjeta para el prompt, o "" si no hay tareas abiertas. Pura.

    Con tope de tamaño: se quitan líneas o tareas ENTERAS (primero las en pausa), nunca se
    corta un valor por la mitad.
    """
    r = r or rotulos()
    abiertas = [t for t in tareas if t.status in ("active", "suspended") and t.parent_task_id is None]
    if not abiertas:
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
        for linea in _lineas_de_tarea(r, foco, proc(foco), en_foco=True):
            if usado + len(linea) + 1 > max_chars:
                continue
            salida.append(linea)
            usado += len(linea) + 1
    omitidas = max(0, len(pausa) - MAX_EN_PAUSA)
    for tarea in pausa[:MAX_EN_PAUSA]:
        bloque = _lineas_de_tarea(r, tarea, proc(tarea), en_foco=False)
        largo = sum(len(linea) + 1 for linea in bloque)
        if usado + largo > max_chars:
            omitidas += 1
            continue
        salida.extend(bloque)
        usado += largo
    if omitidas:
        salida.append(r["mas_en_pausa"].format(cantidad=omitidas))
    return "\n".join(salida)


async def tarjeta_del_turno(conversation_id: uuid.UUID) -> str:
    """Tarjeta para el turno en curso; "" con `aos_procedures` apagada (sin escribir nada).

    Encendida: abre el episodio si corresponde (expira las tareas viejas) y arma la tarjeta
    con las tareas abiertas. Fail-closed: un error de base o del playbook sube por el camino
    de error del turno, como cualquier otra falla de BD.

    Las definiciones de los procedimientos (título, etapas, datos requeridos, acciones) llegan
    con el motor y las releases (HU-AOS-05/14); hasta entonces la tarjeta muestra lo que hay
    en la tarea.
    """
    from core.db.session import get_session
    from core.kernel import state
    from core.kernel.banderas import banderas_del_negocio
    from core.kernel.episodio import DEFAULT_IDLE_MINUTES, abrir_episodio
    from core.shared import release_vigente
    from core.shared.tenant import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return ""
    async with get_session() as session:
        if not (await banderas_del_negocio(session, tenant_id)).procedimientos:
            return ""
        # Un solo episodio por turno: con release (aos_runtime = v3) manda su `idleMinutes`.
        await abrir_episodio(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            idle_minutes=release_vigente.idle_minutes(DEFAULT_IDLE_MINUTES),
        )
        filas = await state.tareas_abiertas(session, tenant_id=tenant_id, conversation_id=conversation_id)
        tareas = [proyeccion_de(f) for f in filas]
    return renderizar_tarjeta(tareas)
