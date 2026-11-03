"""Detector y reescritor de referencias retiradas en un agente (HU-AOS-59, ADR-0184). Puro.

Compara el contenido de una versión (o un borrador) de `agentspec/3` o `kernelspec/1` con lo que
el binario trae hoy y dice qué nombra que ya no existe:

- **Acciones** (`accion_inexistente`): `tools[i].tool`, `guardrails[i].tool` (`required_tool`) y
  `exposes.consultas[i].action` que no están en el catálogo del binario ni entre las tools
  declarativas del propio agente. Es la misma regla de `lint_agentspec` al publicar; la prueba
  `tests/test_referencias_retiradas.py` comprueba que no se separan.
- **Procedimientos** (`procedimiento_inexistente`): `procedures[i].id` con rango de versión que
  ningún pack de la plataforma publica ya. Los abiertos (`=0.0.0`) no entran: los deriva el flujo
  del negocio, no el binario. En el `kernelspec/1`, `routing.rules[i].start` y
  `when.flowStage.procedure` solo se marcan si el registro de retiros los conoce: pueden apuntar a
  un procedimiento abierto que este módulo no puede ver.

`migrar_contenido` aplica el registro de retiros (`core.agents.retiros`): quita lo retirado y, con
reemplazo, pone el procedimiento nuevo si el llamador confirma que el negocio tiene su habilidad.
No decide nada que el registro no diga: un nombre inexistente sin entrada queda como pendiente.

No lee base, red ni entorno; el binario lo arma quien llama (`binario_de`, que sí lee los módulos
instalados, nunca la base).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agentcore_contracts import Procedure

from core.agents.retiros import Reemplazo, Retiro, TipoDeNombre, retiro_de

CODIGO_ACCION = "accion_inexistente"
CODIGO_PROCEDIMIENTO = "procedimiento_inexistente"
KERNEL = "kernelspec/1"
AGENTE = "agentspec/3"


@dataclass(frozen=True)
class Binario:
    """Lo que el binario trae: nombres de acciones y procedimientos publicados por id."""

    acciones: frozenset[str]
    procedimientos: Mapping[str, tuple[Procedure, ...]] = field(default_factory=dict)

    def tiene_procedimiento(self, id_: str) -> bool:
        return id_ in self.procedimientos


@dataclass(frozen=True)
class Hallazgo:
    ruta: str
    codigo: str
    tipo: TipoDeNombre
    nombre: str
    # La entrada del registro de retiros, o None si el nombre desapareció sin declararlo.
    retiro: Retiro | None = None

    @property
    def destino(self) -> str:
        return self.retiro.destino if self.retiro else "sin entrada en el registro de retiros: decide una persona"


@dataclass
class Migracion:
    contenido: dict[str, Any]
    cambios: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    # Retirados sin entrada en el registro: no se tocan.
    pendientes: list[Hallazgo] = field(default_factory=list)

    @property
    def cambia(self) -> bool:
        return bool(self.cambios)


# Decide si el reemplazo se puede agregar en este negocio: None = sí; un texto = por qué no.
Ofrecible = Callable[[Reemplazo], str | None]


# ------------------------------------------------------------------ el binario
def binario_de(modules: Iterable[Any]) -> Binario:
    """El binario de hoy: acciones de `catalogo_de_acciones` y procedimientos de los packs de la
    plataforma (los mismos archivos que registra el manifiesto de la versión del pack)."""
    from core.kernel.procedures import catalogo_de_acciones
    from core.kernel.procedures.de_pack import cargar
    from core.releases.packs import es_de_plataforma

    modulos = list(modules)
    publicados: dict[str, list[Procedure]] = {}
    for modulo in modulos:
        if not es_de_plataforma(modulo):
            continue  # los plugins del negocio no publican procedimientos (build.py)
        for proc in cargar(str(modulo.id)):
            publicados.setdefault(proc.id, []).append(proc)
    return Binario(
        acciones=frozenset(catalogo_de_acciones(modulos)),
        procedimientos={pid: tuple(procs) for pid, procs in publicados.items()},
    )


def nombres_del_binario(modules: Iterable[Any]) -> dict[str, list[str]]:
    """Instantánea de nombres para el lock del CI: solo el core y las habilidades de la
    plataforma, sin los plugins de un negocio (que dependen de dónde se corra)."""
    from core.releases.packs import es_de_plataforma

    plataforma = [m for m in modules if es_de_plataforma(m)]
    binario = binario_de(plataforma)
    return {
        "acciones": sorted(binario.acciones),
        "procedimientos": sorted(binario.procedimientos),
    }


# ------------------------------------------------------------------ detección
def _lista(valor: Any) -> list[Any]:
    return valor if isinstance(valor, list) else []


def _dicts(valor: Any) -> Iterable[tuple[int, Mapping[str, Any]]]:
    return ((i, v) for i, v in enumerate(_lista(valor)) if isinstance(v, Mapping))


def _es_abierto(version: Any) -> bool:
    from core.releases.procedimientos import es_abierto

    return isinstance(version, str) and es_abierto(version)


def _accion(ruta: str, nombre: str) -> Hallazgo:
    return Hallazgo(ruta, CODIGO_ACCION, "accion", nombre, retiro_de("accion", nombre))


def _procedimiento(ruta: str, nombre: str) -> Hallazgo:
    return Hallazgo(ruta, CODIGO_PROCEDIMIENTO, "procedimiento", nombre, retiro_de("procedimiento", nombre))


def hallazgos_de_contenido(contenido: Any, binario: Binario) -> list[Hallazgo]:
    """Referencias retiradas del contenido de una versión o un borrador. Lista vacía = limpio.
    Lee el contenido crudo: sirve aunque ya no pase el contrato por otra razón."""
    if not isinstance(contenido, Mapping):
        return []
    if contenido.get("contract") == KERNEL:
        return _hallazgos_kernel(contenido, binario)
    return _hallazgos_agente(contenido, binario)


def _hallazgos_agente(contenido: Mapping[str, Any], binario: Binario) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    propias = {
        t["name"] for _, t in _dicts(contenido.get("declarativeTools")) if isinstance(t.get("name"), str)
    }

    def existe(nombre: Any) -> bool:
        return not isinstance(nombre, str) or nombre in binario.acciones or nombre in propias

    for i, binding in _dicts(contenido.get("tools")):
        if not existe(binding.get("tool")):
            hallazgos.append(_accion(f"tools[{i}].tool", binding["tool"]))
    for i, regla in _dicts(contenido.get("guardrails")):
        if regla.get("kind") == "required_tool" and not existe(regla.get("tool")):
            hallazgos.append(_accion(f"guardrails[{i}].tool", regla["tool"]))
    exposes = contenido.get("exposes")
    for i, consulta in _dicts(exposes.get("consultas") if isinstance(exposes, Mapping) else None):
        if not existe(consulta.get("action")):
            hallazgos.append(_accion(f"exposes.consultas[{i}].action", consulta["action"]))
    for i, ref in _dicts(contenido.get("procedures")):
        pid = ref.get("id")
        if isinstance(pid, str) and not _es_abierto(ref.get("version")) and not binario.tiene_procedimiento(pid):
            hallazgos.append(_procedimiento(f"procedures[{i}].id", pid))
    return hallazgos


def _hallazgos_kernel(contenido: Mapping[str, Any], binario: Binario) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    routing = contenido.get("routing")
    for i, regla in _dicts(routing.get("rules") if isinstance(routing, Mapping) else None):
        when = regla.get("when")
        etapa = when.get("flowStage") if isinstance(when, Mapping) else None
        candidatos = [(f"routing.rules[{i}].start", regla.get("start"))]
        if isinstance(etapa, Mapping):
            candidatos.append((f"routing.rules[{i}].when.flowStage.procedure", etapa.get("procedure")))
        for ruta, pid in candidatos:
            if isinstance(pid, str) and not binario.tiene_procedimiento(pid) and retiro_de("procedimiento", pid):
                hallazgos.append(_procedimiento(ruta, pid))
    return hallazgos


# ------------------------------------------------------------------ migración
def migrar_contenido(contenido: Mapping[str, Any], binario: Binario, ofrecible: Ofrecible) -> Migracion:
    """El contenido con el registro de retiros aplicado, sin tocar el original. `ofrecible` dice si
    el reemplazo de un procedimiento se puede poner en este negocio (su habilidad está). El
    llamador valida el resultado con `revisar_spec` antes de publicarlo."""
    hallazgos = hallazgos_de_contenido(contenido, binario)
    nuevo = copy.deepcopy(dict(contenido))
    resultado = Migracion(nuevo)
    resultado.pendientes = [h for h in hallazgos if h.retiro is None]
    acciones = {h.nombre for h in hallazgos if h.tipo == "accion" and h.retiro}
    procedimientos = {h.nombre: h.retiro for h in hallazgos if h.tipo == "procedimiento" and h.retiro}
    if not acciones and not procedimientos:
        return resultado
    # El motivo por el que no se pone el reemplazo se pregunta una vez por procedimiento.
    sustitutos: dict[str, str | None] = {}
    for nombre, retiro in procedimientos.items():
        if retiro is not None and retiro.reemplazo is not None:
            sustitutos[nombre] = ofrecible(retiro.reemplazo)
    if contenido.get("contract") == KERNEL:
        _migrar_kernel(nuevo, procedimientos, sustitutos, resultado)
    else:
        _migrar_agente(nuevo, acciones, procedimientos, sustitutos, resultado)
    return resultado


def _migrar_agente(
    contenido: dict[str, Any],
    acciones: set[str],
    procedimientos: Mapping[str, Retiro | None],
    sustitutos: Mapping[str, str | None],
    resultado: Migracion,
) -> None:
    cambios, avisos = resultado.cambios, resultado.avisos

    if acciones:
        tools = _lista(contenido.get("tools"))
        for b in tools:
            if isinstance(b, Mapping) and b.get("tool") in acciones:
                cambios.append(f"quita la acción '{b['tool']}'")
        contenido["tools"] = [b for b in tools if not (isinstance(b, Mapping) and b.get("tool") in acciones)]
        reglas = _lista(contenido.get("guardrails"))
        for r in reglas:
            if isinstance(r, Mapping) and r.get("kind") == "required_tool" and r.get("tool") in acciones:
                cambios.append(f"quita la regla '{r.get('name')}' (exigía la acción retirada '{r['tool']}')")
        contenido["guardrails"] = [
            r for r in reglas
            if not (isinstance(r, Mapping) and r.get("kind") == "required_tool" and r.get("tool") in acciones)
        ]
        exposes = contenido.get("exposes")
        for c in _lista(exposes.get("consultas") if isinstance(exposes, Mapping) else None):
            if isinstance(c, Mapping) and c.get("action") in acciones:
                avisos.append(
                    f"la consulta '{c.get('id')}' dejó de ofrecerse: su acción '{c['action']}' ya no existe"
                )
                cambios.append(f"desliga la acción '{c['action']}' de la consulta '{c.get('id')}'")
                c["action"] = None

    if not procedimientos:
        return
    refs = _lista(contenido.get("procedures"))
    existentes = {r.get("id") for r in refs if isinstance(r, Mapping)}
    nuevos: list[Any] = []
    puestos: set[str] = set()
    cambio_de_id: dict[str, str | None] = {}
    for ref in refs:
        if not isinstance(ref, Mapping) or ref.get("id") not in procedimientos or _es_abierto(ref.get("version")):
            nuevos.append(ref)
            continue
        viejo = ref["id"]
        retiro = procedimientos[viejo]
        reemplazo = retiro.reemplazo if retiro else None
        if reemplazo is None:
            cambios.append(f"quita el procedimiento '{viejo}'")
            cambio_de_id[viejo] = None
            continue
        motivo = sustitutos.get(viejo)
        if motivo is not None:
            cambios.append(f"quita el procedimiento '{viejo}'")
            avisos.append(f"no se agregó {reemplazo}: {motivo}")
            cambio_de_id[viejo] = None
            continue
        if reemplazo.id in existentes or reemplazo.id in puestos:
            cambios.append(f"quita el procedimiento '{viejo}' (ya tenía '{reemplazo.id}')")
        else:
            nuevos.append({"id": reemplazo.id, "version": reemplazo.version})
            puestos.add(reemplazo.id)
            cambios.append(f"cambia el procedimiento '{viejo}' por {reemplazo}")
        if ref.get("overrides"):
            avisos.append(f"se descartaron los textos propios (overrides) de '{viejo}': el procedimiento nuevo es otro")
        cambio_de_id[viejo] = reemplazo.id
    contenido["procedures"] = nuevos

    exposes = contenido.get("exposes")
    if isinstance(exposes, dict):
        quedan = {r.get("id") for r in nuevos if isinstance(r, Mapping)}
        expuestos: list[Any] = []
        for pid in _lista(exposes.get("procedures")):
            destino = cambio_de_id.get(pid, pid) if isinstance(pid, str) else pid
            if destino is None or (isinstance(destino, str) and destino not in quedan):
                cambios.append(f"deja de exponer '{pid}'")
            elif destino not in expuestos:
                expuestos.append(destino)
        exposes["procedures"] = expuestos


def _migrar_kernel(
    contenido: dict[str, Any],
    procedimientos: Mapping[str, Retiro | None],
    sustitutos: Mapping[str, str | None],
    resultado: Migracion,
) -> None:
    routing = contenido.get("routing")
    if not isinstance(routing, dict):
        return
    reglas: list[Any] = []
    for regla in _lista(routing.get("rules")):
        if not isinstance(regla, dict):
            reglas.append(regla)
            continue
        when = regla.get("when")
        etapa = when.get("flowStage") if isinstance(when, Mapping) else None
        viejo_start = regla.get("start") if regla.get("start") in procedimientos else None
        viejo_etapa = etapa.get("procedure") if isinstance(etapa, Mapping) and etapa.get("procedure") in procedimientos else None
        if viejo_etapa is not None:
            # El nombre de la etapa es del procedimiento retirado: cambiarle el id la dejaría sin sentido.
            resultado.cambios.append(f"quita la regla '{regla.get('id')}' (depende de la etapa de '{viejo_etapa}')")
            continue
        if viejo_start is not None:
            retiro = procedimientos[viejo_start]
            reemplazo = retiro.reemplazo if retiro else None
            if reemplazo is not None and sustitutos.get(viejo_start) is None:
                regla["start"] = reemplazo.id
                resultado.cambios.append(f"la regla '{regla.get('id')}' inicia '{reemplazo.id}' en lugar de '{viejo_start}'")
            else:
                if reemplazo is not None:
                    resultado.avisos.append(f"no se agregó {reemplazo}: {sustitutos.get(viejo_start)}")
                resultado.cambios.append(f"quita la regla '{regla.get('id')}' (iniciaba '{viejo_start}')")
                continue
        reglas.append(regla)
    routing["rules"] = reglas


__all__ = [
    "AGENTE",
    "CODIGO_ACCION",
    "CODIGO_PROCEDIMIENTO",
    "KERNEL",
    "Binario",
    "Hallazgo",
    "Migracion",
    "Ofrecible",
    "binario_de",
    "hallazgos_de_contenido",
    "migrar_contenido",
    "nombres_del_binario",
]
