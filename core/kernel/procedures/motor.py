"""Reglas del motor de procedimientos, puras (HU-AOS-05, ADR-0113 §1).

Sin base, red ni reloj: dada la definición (`Procedure`), la proyección de la tarea
(`state.Proyeccion`) y sus eventos, dicen qué etapa rige, qué acciones permite, qué falta,
si la etapa puede salir, qué pide confirmación y si un valor es válido. La persistencia y los
efectos están en `ejecucion.py`.

Semántica (adenda de implementación de ADR-0113, HU-AOS-05):
- **Acciones de la etapa:** `stage.actions`. Las del core se permiten siempre (las filtra el
  llamador); cualquier otra se deniega.
- **Confirmación:** una sola puerta por etapa. Si la etapa declara `confirmation` y su
  `exitWhen.action`, la puerta es esa acción (la que compromete); si `exitWhen` no tiene acción,
  la puerta es la salida de la etapa. `confirmActions` agrega puertas por acción. La
  confirmación de una acción queda atada al hash de sus argumentos.
- **Sin tarea en foco** (adenda 2026-10-12): las puertas de acción de los procedimientos
  disponibles (`acciones_de_puerta`) no se ofrecen ni se ejecutan; hay que iniciar el recorrido.
- **Salida:** `exitWhen.slots` llenos (los `requiredWhen` que no aplican cuentan como llenos),
  `confirmed` confirmados y `action` terminada en la etapa (con `ok`, si se indica).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from agentcore_contracts import Procedure, Slot, Stage

from core.kernel.procedures.linter import PREFIJO_CONFIRMAR as _PREFIJO_RESERVADO
from core.kernel.state import Evento, Proyeccion

_EMAIL = re.compile(r"^[^@\s\"'<>]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")
_URI = re.compile(r"^https?://[^\s\"'<>]{1,2000}$")
_PHONE = re.compile(r"^\+?[0-9][0-9 ()-]{5,19}$")
# Prefijo reservado del botón de confirmación: `confirmar:<task_id>:<seq>` (el linter lo reserva).
PREFIJO_CONFIRMAR = _PREFIJO_RESERVADO
_CONFIRMAR = re.compile(r"^confirmar:([0-9a-f-]{36}):([0-9]{1,9}):([0-9a-f]{16})$")


# ---------------------------------------------------------------- etapas


def etapa(proc: Procedure, stage_id: str) -> Stage | None:
    return next((s for s in proc.stages if s.id == stage_id), None)


def siguiente(proc: Procedure, stage: Stage) -> Stage | None:
    """Etapa a la que se pasa al salir: `next` o la siguiente de la lista (None = no hay)."""
    if stage.next:
        return etapa(proc, stage.next)
    ids = [s.id for s in proc.stages]
    i = ids.index(stage.id)
    return proc.stages[i + 1] if i + 1 < len(proc.stages) else None


def es_terminal(stage: Stage) -> bool:
    return stage.exitWhen is None


def valores(tarea: Proyeccion) -> dict[str, Any]:
    return {n: (m or {}).get("value") for n, m in tarea.slots.items()}


def requerido(slot: Slot, vals: Mapping[str, Any]) -> bool:
    if slot.required:
        return True
    cond = slot.requiredWhen
    return cond is not None and vals.get(cond.slot) == cond.equals


def faltan(proc: Procedure, tarea: Proyeccion) -> list[str]:
    """Datos obligatorios (o que aplican por condición) sin valor."""
    vals = valores(tarea)
    return [n for n, s in proc.slots.items() if requerido(s, vals) and vals.get(n) is None]


def eventos_de_la_etapa(eventos: Sequence[Evento]) -> list[Evento]:
    """Eventos desde que se entró a la etapa vigente (el último `stage_entered` o `started`)."""
    corte = 0
    for i, e in enumerate(eventos):
        if e.type in ("started", "stage_entered", "revalidated"):
            corte = i
    return list(eventos[corte + 1 :])


def accion_terminada(eventos_etapa: Sequence[Evento], accion: str, ok: bool | None) -> bool:
    for e in eventos_etapa:
        if e.data.get("action") != accion:
            continue
        if e.type == "action_succeeded" and ok in (None, True):
            return True
        if e.type == "action_failed" and ok in (None, False):
            return True
    return False


def puerta_de_salida(stage: Stage) -> bool:
    """La salida de la etapa espera confirmación (etapa con `confirmation` sin acción de salida)."""
    return stage.confirmation is not None and stage.exitWhen is not None and stage.exitWhen.action is None


def etapa_confirmada(eventos_etapa: Sequence[Evento], stage: Stage, valores_hash: str) -> bool:
    """¿El cliente confirmó la salida de la etapa con ESTOS valores? Si un dato cambió después del
    sí, la confirmación ya no vale."""
    return any(
        e.type == "confirmed" and (e.data.get("target") or {}).get("kind") == "stage"
        and (e.data.get("target") or {}).get("name") == stage.id
        and e.data.get("args_hash") == valores_hash
        for e in eventos_etapa
    )


def salida_cumplida(proc: Procedure, stage: Stage, tarea: Proyeccion, eventos_etapa: Sequence[Evento]) -> bool:
    """¿Se cumple `exitWhen`? (sin contar la puerta de confirmación de la salida)."""
    salida = stage.exitWhen
    if salida is None:
        return False
    vals = valores(tarea)

    def aplica(nombre: str) -> bool:
        slot = proc.slots.get(nombre)
        if slot is None or slot.requiredWhen is None:
            return True
        return vals.get(slot.requiredWhen.slot) == slot.requiredWhen.equals

    for nombre in salida.slots:
        if nombre not in proc.slots or (aplica(nombre) and vals.get(nombre) is None):
            return False
    for nombre in salida.confirmed:
        if aplica(nombre) and (tarea.slots.get(nombre) or {}).get("confirmed") is not True:
            return False
    return not (salida.action is not None and not accion_terminada(eventos_etapa, salida.action, salida.ok))


# ---------------------------------------------------------------- cambios de datos (HU-AOS-07)


def dependientes(tarea: Proyeccion, seq_anterior: int, excluir: frozenset[str] = frozenset()) -> list[str]:
    """Datos que fijó una acción (`source = action:*`) DESPUÉS del valor que acaba de cambiar:
    se calcularon con el valor viejo (la cotización del plan anterior) y dejan de valer. Es una
    heurística de causalidad por `seq` que invalida de más, hacia el lado seguro: la acción se
    vuelve a llamar. `excluir`: los datos que se acaban de escribir en el mismo lote."""
    salida: list[str] = []
    for nombre, meta in tarea.slots.items():
        if nombre in excluir or not isinstance(meta, Mapping):
            continue
        if str(meta.get("source") or "").startswith("action:") and int(meta.get("seq") or 0) > seq_anterior:
            salida.append(nombre)
    return salida


def etapa_de_retroceso(proc: Procedure, tarea: Proyeccion) -> Stage | None:
    """La primera etapa del camino, ANTES de la vigente, cuyas condiciones de datos (`exitWhen`
    `slots` y `confirmed`) ya no se cumplen; None si todas siguen cumpliéndose. Las salidas por
    acción de etapas ya pasadas cuentan como cumplidas (sus eventos quedaron atrás)."""
    stage: Stage | None = proc.stages[0]
    vistas: set[str] = set()
    while stage is not None and stage.id != tarea.stage and stage.id not in vistas:
        vistas.add(stage.id)
        salida = stage.exitWhen
        if salida is None:
            return None
        if salida.slots or salida.confirmed:
            solo_datos = stage.model_copy(update={"exitWhen": salida.model_copy(update={"action": None, "ok": None})})
            if not salida_cumplida(proc, solo_datos, tarea, ()):
                return stage
        stage = siguiente(proc, stage)
    return None


def datos_congelados(proc: Procedure, eventos: Sequence[Evento]) -> bool:
    """Desde la primera acción que terminó bien en una visita a una etapa `risk: high` (hubo un
    efecto: el pedido existe), los datos con valor ya no cambian por el resto de la tarea, también
    en las etapas que siguen (la espera del pago, HU-AOS-07): cambiarlos dejaría el efecto viejo
    vivo junto al nuevo (dos pedidos). La salida es cancelar el recorrido. Solo frena lo que
    escriben el modelo o un botón; el resultado de una acción (un link nuevo) sí se anota.
    `eventos`: todos los de la tarea, en orden de `seq`."""
    etapa_vigente: Stage | None = None
    for e in eventos:
        if e.type == "started":
            etapa_vigente = etapa(proc, str(e.data.get("stage") or ""))
        elif e.type in ("stage_entered", "revalidated"):
            etapa_vigente = etapa(proc, str(e.data.get("to_stage") or ""))
        elif e.type == "action_succeeded" and etapa_vigente is not None and etapa_vigente.risk == "high":
            return True
    return False


# ---------------------------------------------------------------- argumentos ligados (HU-AOS-07)


@dataclass(frozen=True)
class Distinto:
    """Un argumento ligado que el modelo mandó con otro valor que el dato de la tarea."""

    dato: str
    padre: str | None  # el objeto de primer nivel que lo contiene (`customer_data`), o None
    valor: Any


@dataclass(frozen=True)
class Ligadura:
    args: dict[str, Any]
    distintos: tuple[Distinto, ...] = ()


def mismo_valor(slot: Slot, valor_tarea: Any, valor_arg: Any) -> bool:
    """¿El argumento dice lo mismo que el dato? Se normaliza como el dato (tipo, formato, opciones)."""
    v, rechazo = validar_valor(slot, valor_arg)
    return rechazo is None and v == valor_tarea


def ligar_argumentos(
    proc: Procedure, tarea: Proyeccion, esquema: Mapping[str, Any], args: Mapping[str, Any]
) -> Ligadura:
    """Argumentos efectivos de una acción gobernada: los que tienen el nombre de un dato de la
    tarea con valor (en el primer nivel del input schema o dentro de un objeto de primer nivel,
    p. ej. `customer_data.email`) se atan a ese dato. Misma convención por nombre que las
    precondiciones.

    - Igual (normalizado) → el valor del dato (canónico: el hash de la confirmación no depende de
      cómo lo escribió el modelo).
    - Distinto → `distintos` (quien llama lo pasa por el resolver del dato o lo deniega). También
      un valor para un dato que fija una acción (`setBy: action:`) y la tarea aún no tiene.
    - Ausente → se completa solo si es obligatorio (`required` de primer nivel, o dentro de un
      objeto de primer nivel obligatorio o presente). Un argumento opcional no se completa: puede
      ser una alternativa (`sku` o `variant_id`) y completarlo cambiaría la consulta.
    """
    vals = valores(tarea)
    propiedades = esquema.get("properties") if isinstance(esquema, Mapping) else None
    if not isinstance(propiedades, Mapping):
        return Ligadura(dict(args))
    requeridos = set(esquema.get("required") or [])
    efectivos: dict[str, Any] = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in args.items()}
    distintos: list[Distinto] = []

    def atar(destino: dict[str, Any], nombre: str, padre: str | None, obligatorio: bool) -> None:
        slot = proc.slots.get(nombre)
        if slot is None:
            return
        if vals.get(nombre) is None:
            # Un id que fija el servidor y todavía no existe en la tarea (el pedido de esta
            # compra): un valor del modelo sería de otra compra (el pedido anterior, ADR-0130).
            if nombre in destino and str(slot.setBy or "").startswith("action:"):
                distintos.append(Distinto(nombre, padre, destino[nombre]))
            return
        if nombre not in destino:
            if obligatorio:
                destino[nombre] = vals[nombre]
            return
        if mismo_valor(slot, vals[nombre], destino[nombre]):
            destino[nombre] = vals[nombre]
        else:
            distintos.append(Distinto(nombre, padre, destino[nombre]))

    for nombre, sub in propiedades.items():
        nombre = str(nombre)
        atar(efectivos, nombre, None, nombre in requeridos)
        internas = sub.get("properties") if isinstance(sub, Mapping) and sub.get("type") == "object" else None
        if not isinstance(internas, Mapping) or nombre in proc.slots:
            continue
        objeto = efectivos.get(nombre)
        if objeto is None and nombre in requeridos and any(
            str(n) in proc.slots and vals.get(str(n)) is not None for n in internas
        ):
            objeto = efectivos[nombre] = {}
        if isinstance(objeto, dict):
            for interna in internas:
                atar(objeto, str(interna), nombre, True)
    return Ligadura(efectivos, tuple(distintos))


# ---------------------------------------------------------------- confirmación


def requiere_confirmacion(stage: Stage, accion: str) -> bool:
    if accion in stage.confirmActions:
        return True
    return (
        stage.confirmation is not None
        and stage.exitWhen is not None
        and stage.exitWhen.action == accion
    )


def acciones_de_puerta(proc: Procedure) -> frozenset[str]:
    """Las acciones que son puerta de confirmación en alguna etapa (`confirmActions` y la
    `exitWhen.action` de una etapa con `confirmation`): las que comprometen algo con el sí del
    cliente. Sin tarea en foco no existen fuera del procedimiento (adenda 2026-10-12 de ADR-0113,
    "sin foco no hay camino de siempre"). Regla estructural hasta que el manifiesto de acciones v2
    (HU-AOS-34) declare el efecto de cada acción."""
    return frozenset(a for stage in proc.stages for a in stage.actions if requiere_confirmacion(stage, a))


def hash_args(args: Mapping[str, Any] | None) -> str:
    """Hash de los argumentos canónicos: la confirmación de una acción vale para ESOS args."""
    canon = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


def accion_confirmada(eventos: Sequence[Evento], accion: str, h: str) -> bool:
    """¿Hay un `confirmed` de esa acción con esos args que ninguna ejecución consumió, dado en la
    visita vigente a la etapa? Un cambio de etapa (también el retroceso por un dato que cambió,
    HU-AOS-07) vence las confirmaciones anteriores: al volver se pide otra vez."""
    for e in reversed(eventos):
        if e.type in ("started", "stage_entered", "revalidated"):
            return False
        if e.data.get("action") == accion and e.type in ("action_requested", "action_succeeded", "action_failed"):
            return False
        objetivo = e.data.get("target") or {}
        if e.type == "confirmed" and objetivo.get("kind") == "action" and objetivo.get("name") == accion:
            return e.data.get("args_hash") == h
    return False


def huella_pedida(eventos: Sequence[Evento], accion: str, h: str) -> str | None:
    """La huella del resumen que vio el cliente en la última petición de confirmación de esa
    acción con esos args (`confirmation_requested.data.resumen`), o None si no la tiene."""
    for e in reversed(eventos):
        objetivo = e.data.get("target") or {}
        if (
            e.type == "confirmation_requested" and objetivo.get("kind") == "action"
            and objetivo.get("name") == accion and e.data.get("args_hash") == h
        ):
            valor = e.data.get("resumen")
            return str(valor) if valor else None
    return None


def huella_de_resumen(lineas: Sequence[Any]) -> str:
    """Huella de un resumen del pack (nombre, valor, monto): si el estado que lo produjo cambia
    (otro total con el mismo `quote_id`), la confirmación dada sobre el viejo no vale."""
    canon = [[str(getattr(x, "nombre", "")), getattr(x, "valor", None), bool(getattr(x, "es_monto", False))] for x in lineas]
    return hash_args({"resumen": canon})


@dataclass(frozen=True)
class BotonConfirmar:
    task_id: str
    seq: int
    firma: str


def _firma(secreto: str, tenant_id: str, task_id: str, seq: int, args_hash: str) -> str:
    mensaje = f"{tenant_id}|{task_id}|{seq}|{args_hash}".encode()
    return hmac.new(secreto.encode(), mensaje, hashlib.sha256).hexdigest()[:16]


def payload_boton_confirmar(secreto: str, tenant_id: str, task_id: str, seq: int, args_hash: str) -> str:
    """Id del botón (o payload de botón de plantilla, HU-AOS-09) que confirma UNA petición:
    `confirmar:<task_id>:<seq>:<firma>`. La firma (HMAC con el secreto del servidor) ata el negocio,
    la tarea, la petición y los argumentos: el cliente no puede fabricar el de otra petición."""
    return f"{PREFIJO_CONFIRMAR}{task_id}:{seq}:{_firma(secreto, tenant_id, task_id, seq, args_hash)}"


def leer_boton_confirmar(payload: str) -> BotonConfirmar | None:
    m = _CONFIRMAR.fullmatch(payload or "")
    return BotonConfirmar(task_id=m.group(1), seq=int(m.group(2)), firma=m.group(3)) if m else None


def boton_valido(boton: BotonConfirmar, secreto: str, tenant_id: str, args_hash: str) -> bool:
    esperada = _firma(secreto, tenant_id, boton.task_id, boton.seq, args_hash)
    return hmac.compare_digest(esperada, boton.firma)


# ---------------------------------------------------------------- valores


@dataclass(frozen=True)
class Rechazo:
    codigo: str          # tipo_invalido | enum_invalido | formato_invalido
    detalle: str = ""    # tipo o formato esperado (del contrato, nunca del cliente)


def validar_valor(slot: Slot, valor: Any) -> tuple[Any, Rechazo | None]:
    """Valida y normaliza un valor contra `type`, `enum` y `format` del dato."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None, Rechazo("tipo_invalido", slot.type or "string")
    tipo = slot.type or ("string" if slot.enum is not None else None)
    v: Any = valor.strip() if isinstance(valor, str) else valor
    if tipo is not None:
        v, ok = _por_tipo(tipo, v)
        if not ok:
            return None, Rechazo("tipo_invalido", tipo)
    if slot.enum is not None and str(v) not in slot.enum:
        return None, Rechazo("enum_invalido", "")
    if slot.format is not None and isinstance(v, str):
        patron = {"email": _EMAIL, "uri": _URI, "phone": _PHONE}[slot.format]
        if not patron.fullmatch(v):
            return None, Rechazo("formato_invalido", slot.format)
        if slot.format == "email":
            v = v.lower()
    return v, None


def _por_tipo(tipo: str, v: Any) -> tuple[Any, bool]:
    if tipo == "string":
        return (v, True) if isinstance(v, str) and len(v) <= 1000 else (None, False)
    if tipo == "boolean":
        return (v, True) if isinstance(v, bool) else (None, False)
    if tipo == "integer":
        if isinstance(v, bool):
            return None, False
        if isinstance(v, int):
            return v, True
        if isinstance(v, str) and re.fullmatch(r"-?[0-9]{1,15}", v):
            return int(v), True
        return None, False
    if tipo == "number":
        if isinstance(v, bool):
            return None, False
        if isinstance(v, int | float):
            return v, v == v and abs(v) != float("inf")
        if isinstance(v, str) and re.fullmatch(r"-?[0-9]{1,15}([.,][0-9]{1,6})?", v):
            return float(v.replace(",", ".")), True
        return None, False
    if tipo in ("date", "time", "datetime"):
        if not isinstance(v, str):
            return None, False
        try:
            {"date": date.fromisoformat, "time": time.fromisoformat, "datetime": datetime.fromisoformat}[tipo](v)
        except ValueError:
            return None, False
        return v, True
    if tipo == "location":
        ok = (
            isinstance(v, Mapping)
            and all(isinstance(v.get(k), int | float) and not isinstance(v.get(k), bool) for k in ("lat", "lng"))
            and -90 <= v["lat"] <= 90
            and -180 <= v["lng"] <= 180
        )
        return ({"lat": v["lat"], "lng": v["lng"]}, True) if ok else (None, False)
    if tipo == "media":
        ok = isinstance(v, Mapping) and isinstance(v.get("media_id"), str) and 0 < len(v["media_id"]) <= 200
        return ({"media_id": v["media_id"], "mime": str(v.get("mime") or "")[:100]}, True) if ok else (None, False)
    return None, False


# ---------------------------------------------------------------- entradas sin LLM

_TIPO_POR_MIME = (("image/", "image"), ("audio/", "audio"), ("application/", "document"), ("text/", "document"))


def tipo_de_contenido(mime: str) -> str | None:
    """`image/jpeg` → `image`. El video no es un `ContentKind` del contrato: se ignora."""
    mime = (mime or "").lower()
    if mime in ("image", "audio", "document"):
        return mime
    return next((k for pref, k in _TIPO_POR_MIME if mime.startswith(pref)), None)


def dato_de_boton(proc: Procedure, payload: str) -> tuple[str, str] | None:
    """`plan:premium` → (`<dato con setBy interactive:plan:>`, `premium`)."""
    prefijo, sep, valor = (payload or "").partition(":")
    if not sep or not valor or payload.startswith(PREFIJO_CONFIRMAR):
        return None
    origen = f"interactive:{prefijo}:"
    nombre = next((n for n, s in proc.slots.items() if s.setBy == origen), None)
    return (nombre, valor[:100]) if nombre else None


def dato_de_contenido(proc: Procedure, tarea: Proyeccion, tipo: str) -> str | None:
    """Primer dato `setBy: content:<tipo>` sin valor (o el primero, si todos tienen)."""
    nombres = [n for n, s in proc.slots.items() if s.setBy == f"content:{tipo}"]
    vacios = [n for n in nombres if (tarea.slots.get(n) or {}).get("value") is None]
    return (vacios or nombres or [None])[0]


def candidatos_de_resultado(proc: Procedure, accion: str, resultado: Mapping[str, Any]) -> dict[str, list[str]]:
    """Opciones que ofreció una acción para los datos con `candidates.source: action:<acción>.<campo>`
    (solo ids: el escalar de cada opción o su `id`; hasta `max`, cada una ≤100 caracteres)."""
    salida: dict[str, list[str]] = {}
    for nombre, slot in proc.slots.items():
        cand = slot.candidates
        if cand is None or not cand.source.startswith(f"action:{accion}"):
            continue
        _, _, campo = cand.source.removeprefix("action:").partition(".")
        lista = resultado.get(campo) if campo else resultado.get(nombre)
        if not isinstance(lista, list):
            continue
        ids: list[str] = []
        for item in lista:
            valor = item.get("id") if isinstance(item, Mapping) else item
            if isinstance(valor, str | int) and not isinstance(valor, bool) and len(str(valor)) <= 100:
                ids.append(str(valor))
            if len(ids) >= cand.max:
                break
        salida[nombre] = ids
    return salida


def ultimos_candidatos(eventos: Sequence[Evento], nombre: str) -> list[str] | None:
    for e in reversed(eventos):
        cands = e.data.get("candidates") if e.type == "action_succeeded" else None
        if isinstance(cands, Mapping) and isinstance(cands.get(nombre), list):
            return [str(c) for c in cands[nombre]]
    return None


def correlaciona(
    proc: Procedure, tarea: Proyeccion, payload: Mapping[str, Any], claves: Sequence[str]
) -> bool:
    """El evento es de esta tarea: una clave de correlación del topic (`claves`) con el MISMO
    nombre que un dato fijado por una acción del servidor (`source = action:*`, nunca por el LLM ni
    el cliente) y el mismo valor. Ej.: dato `reference` (de la acción que crea el link) ↔
    `payment.confirmed.reference`. Montos o proveedores nunca correlacionan."""
    for nombre in claves:
        meta = tarea.slots.get(nombre) or {}
        if nombre not in proc.slots or not str(meta.get("source") or "").startswith("action:"):
            continue
        valor, recibido = meta.get("value"), payload.get(nombre)
        if valor is not None and recibido is not None and str(valor) == str(recibido) and len(str(valor)) <= 200:
            return True
    return False
