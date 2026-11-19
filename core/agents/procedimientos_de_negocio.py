"""Procedimientos que publican las habilidades de un negocio y sus referencias (ADR-0113 §1).

Salen del lector tolerante (ADR-0191): el camino v3 los usa para armar agentes y releases."""

from __future__ import annotations

from collections.abc import Iterable

from agentcore_contracts import AgentSpec, Procedure

from core.kernel.procedures.catalogo import CORE_OWNER


def declarados(publicados: Iterable[Procedure], ya: set[str]) -> list[dict[str, str]]:
    """Refs `^M.0` de los procedimientos publicados, uno por id (la versión más alta), sin
    repetir un id abierto (el linter rechaza repetidos)."""
    from core.releases.procedimientos import parsear_version, rango_compatible

    por_id: dict[str, Procedure] = {}
    for proc in publicados:
        actual = por_id.get(proc.id)
        if actual is None or parsear_version(proc.version) > parsear_version(actual.version):
            por_id[proc.id] = proc
    return [
        {"id": pid, "version": rango_compatible(por_id[pid].version)}
        for pid in sorted(por_id)
        if pid not in ya
    ][:60]

def procedimientos_publicados(spec: AgentSpec) -> list[Procedure]:
    """Los procedimientos `procedure/1` que publican los packs habilitados del spec (de sus
    carpetas `procedures/`, el mismo cargador que los registra), solo los ofrecibles: su
    habilidad y todo su `requires` habilitados (ADR-0113 §1)."""
    from core.agents.spec import enabled_packs
    from core.kernel.procedures.de_pack import cargar
    from core.releases.procedimientos import ofrecible

    habilitados = enabled_packs(spec)
    salida: list[Procedure] = []
    for pack_id in sorted(habilitados - {CORE_OWNER}):
        salida.extend(p for p in cargar(pack_id) if ofrecible(p, habilitados))
    return salida
