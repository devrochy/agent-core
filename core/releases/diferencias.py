"""Qué procedimientos tocó una release candidata frente a la activa (HU-AOS-21, adenda de ADR-0117). Puro.

El gate de pass^k mide solo lo que cambió. Se comparan las dos releases cargadas con el mismo
binario (`ReleaseCongelada`):

- **contenido**: un procedimiento cuyo contenido canónico cambió, o que es nuevo;
- **agente**: `content_hash` distinto (modelo, persona, tools), alta del agente o cambio de
  `is_default` tocan todos sus procedimientos; la baja de un especialista toca los que tenía y
  siguen en la candidata;
- **coordinador**: un `content_hash` distinto del componente `core` (voz, reglas, `routing`) toca
  todos los procedimientos;
- **playbook**: un override que declara un agente toca los de ese agente; uno que no declara ninguno
  (allowlist v2) toca todos;
- **permisos** (adenda HU-AOS-27): un enlace A2A añadido, quitado o cambiado (modos, intenciones o
  límites) toca los procedimientos de sus agentes `from` y `to` que están en la conexión; los enlaces
  con un agente de fuera son inertes;
- **app_settings**, **plugins del negocio** y **habilidad** (`manifest_hash` distinto) tocan todos.
  Para la habilidad es una aproximación conservadora: no hay un mapa fiable de qué agente usa cada
  pack.

Sin activa, o con una activa `release/1` (sin procedimientos por agente), todos cuentan como
tocados y no hay línea base. Un procedimiento que desaparece no se mide. Un `content_hash` que
cambia solo por campos que no entran al prompt también cuenta: es más simple y más seguro.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from core.releases.cargar import ReleaseCongelada

SIN_LINEA_BASE = "sin_linea_base"


def _canonico(proc: Any) -> str:
    return json.dumps(proc.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def _hashes_por_instancia(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(c.get("instance_id")): str(c.get("content_hash"))
        for c in manifest.get("components") or []
        if isinstance(c, Mapping)
    }


def _skills(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(s.get("pack_id")): str(s.get("manifest_hash"))
        for s in manifest.get("skills") or []
        if isinstance(s, Mapping)
    }


def _playbooks_declarados(agente: Any) -> set[str]:
    return {
        str(ref["name"])
        for ref in agente.content.get("playbooks") or []
        if isinstance(ref, Mapping) and isinstance(ref.get("name"), str)
    }


def procedimientos_tocados(candidata: ReleaseCongelada, activa: ReleaseCongelada | None) -> dict[str, str]:
    """`{procedure_id: motivo}` de los procedimientos de la candidata que hay que medir.

    El motivo es el primero que se detectó (para mostrarlo); vacío = no tocó nada."""
    todos = candidata.procedimientos()
    if activa is None or not activa.con_procedimientos_por_agente:
        return {pid: SIN_LINEA_BASE for pid in sorted(todos)}

    tocados: dict[str, str] = {}

    def marcar(pids: Any, motivo: str) -> None:
        for pid in sorted(pids):
            if pid in todos:
                tocados.setdefault(pid, motivo)

    # Globales: tocan todo.
    if candidata.app_settings != activa.app_settings:
        marcar(todos, "app_settings")
    if sorted(candidata.tenant_plugins) != sorted(activa.tenant_plugins):
        marcar(todos, "plugins_del_negocio")
    antes, ahora = _skills(activa.manifest), _skills(candidata.manifest)
    for pack in sorted(set(antes) | set(ahora)):
        if antes.get(pack) != ahora.get(pack):
            marcar(todos, f"habilidad:{pack}")

    # Contenido de cada procedimiento.
    previos = activa.procedimientos()
    for pid, proc in sorted(todos.items()):
        anterior = previos.get(pid)
        if anterior is None:
            marcar([pid], "nuevo")
        elif _canonico(proc) != _canonico(anterior):
            marcar([pid], "contenido")

    # Agentes.
    hash_antes = _hashes_por_instancia(activa.manifest)
    hash_ahora = _hashes_por_instancia(candidata.manifest)
    por_instancia = {c.instance_id: c for c in activa.componentes}
    for agente in candidata.componentes:
        previo = por_instancia.get(agente.instance_id)
        cambio = hash_antes.get(str(agente.instance_id)) != hash_ahora.get(str(agente.instance_id))
        if agente.role == "core":
            if cambio:
                marcar(todos, "coordinador")
            continue
        propios = candidata.procedimientos_de(agente.instance_id)
        if previo is None:
            marcar(propios, f"agente_nuevo:{agente.key}")
        elif cambio:
            marcar(propios, f"agente:{agente.key}")
        elif previo.is_default != agente.is_default:
            marcar(propios, f"agente_por_defecto:{agente.key}")
    vigentes: set[uuid.UUID] = {c.instance_id for c in candidata.componentes}
    for previo in activa.componentes:
        if previo.instance_id not in vigentes and previo.role != "core":
            marcar(activa.procedimientos_de(previo.instance_id), f"agente_retirado:{previo.key}")

    # Permisos entre agentes (HU-AOS-18/19): mismo `content_hash` = sin cambio.
    grafo_antes, grafo_ahora = activa.a2a or {}, candidata.a2a or {}
    if grafo_antes.get("content_hash") != grafo_ahora.get("content_hash"):
        def enlaces(grafo: Mapping[str, Any]) -> dict[tuple[str, str], str]:
            return {
                (str(e.get("from_agent_id")), str(e.get("to_agent_id"))): json.dumps(e, sort_keys=True, default=str)
                for e in grafo.get("links") or []
                if isinstance(e, Mapping)
            }

        e_antes, e_ahora = enlaces(grafo_antes), enlaces(grafo_ahora)
        por_agente = {str(c.agent_id): c for c in candidata.componentes}
        for par in sorted(set(e_antes) | set(e_ahora)):
            if e_antes.get(par) == e_ahora.get(par):
                continue
            desde, hacia = por_agente.get(par[0]), por_agente.get(par[1])
            if desde is None or hacia is None:
                continue  # inerte: un agente que no está en la conexión
            motivo = f"permiso:{desde.key}>{hacia.key}"
            marcar(candidata.procedimientos_de(desde.instance_id), motivo)
            marcar(candidata.procedimientos_de(hacia.instance_id), motivo)

    # Overrides de playbooks.
    pb_antes, pb_ahora = activa.playbooks, candidata.playbooks
    cambiados = {n for n in set(pb_antes) | set(pb_ahora) if pb_antes.get(n) != pb_ahora.get(n)}
    if cambiados:
        declarados_por_alguno: set[str] = set()
        for agente in candidata.componentes:
            propios_pb = _playbooks_declarados(agente)
            declarados_por_alguno |= propios_pb
            for nombre in sorted(cambiados & propios_pb):
                marcar(candidata.procedimientos_de(agente.instance_id), f"playbook:{nombre}")
        for nombre in sorted(cambiados - declarados_por_alguno):
            marcar(todos, f"playbook:{nombre}")
    return tocados
