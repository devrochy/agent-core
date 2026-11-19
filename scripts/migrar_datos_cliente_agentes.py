"""Migra la política de datos del cliente del negocio a cada agente (HU-CRM-28).

La política global vivía en `app_settings.behavior_config["datos_cliente"]` (`politica` y
`herramientas`). Ahora cada especialista tiene la suya en `agents.data_policy`. Este script:

1. Lee la política global del negocio.
2. Para cada especialista activo, copia solo los ajustes de las operaciones que ese agente
   puede ejecutar (sus acciones habilitadas o concedidas por un superpoder).
3. Quita `politica` y `herramientas` del `behavior_config`, conservando `aviso`.
4. Deja auditoría con el resumen por agente.

Uso:
  python scripts/migrar_datos_cliente_agentes.py            # dry-run: no escribe nada
  python scripts/migrar_datos_cliente_agentes.py --apply    # aplica los cambios
  python scripts/migrar_datos_cliente_agentes.py --tenant <uuid> [--apply]

Es idempotente: si ya no hay política global, no hace nada.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))


def _tools_de_sus_procedimientos(contenido: dict[str, Any], packs: set[str]) -> set[str]:
    """Acciones que conceden los superpoderes declarados (etapas), como hace la API del agente."""
    from core.kernel.procedures.de_pack import cargar
    from core.releases.procedimientos import ofrecible, parsear_version

    por_id = {}
    for pack_id in sorted(packs):
        try:
            procedimientos = cargar(pack_id)
        except Exception:
            continue
        for proc in procedimientos:
            if not ofrecible(proc, packs):
                continue
            actual = por_id.get(proc.id)
            if actual is None or parsear_version(proc.version) > parsear_version(actual.version):
                por_id[proc.id] = proc
    refs = {
        str(p.get("id"))
        for p in contenido.get("procedures") or []
        if isinstance(p, dict) and p.get("id")
    }
    tools: set[str] = set()
    for pid in refs:
        proc = por_id.get(pid)
        if proc is None:
            continue
        for stage in proc.stages:
            tools.update(stage.actions)
    return tools


async def _migrar_negocio(tenant_id: uuid.UUID, *, aplicar: bool) -> dict[str, Any]:
    from sqlalchemy import select

    from core.agents import versiones
    from core.agents.editor import habilitados_del_negocio
    from core.agents.habilidades_del_agente import packs_del_agente
    from core.datos_cliente.ajustes import con_herramientas, con_politica, limpiar
    from core.datos_cliente.registro import registro_datos
    from core.db.models import AgentRecord, AppSettings, AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    resumen: dict[str, Any] = {"tenant_id": str(tenant_id), "agentes": [], "politica_global": 0}

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            settings = (
                await session.execute(
                    select(AppSettings).where(
                        AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if settings is None:
                return resumen
            behavior = dict(settings.behavior_config or {})
            bloque = behavior.get("datos_cliente")
            if not isinstance(bloque, dict):
                return resumen
            politica = limpiar(bloque.get("politica"))
            herramientas = bool(bloque.get("herramientas"))
            if not politica and not herramientas:
                return resumen
            resumen["politica_global"] = len(politica)
            resumen["herramientas_global"] = herramientas

            agentes = (
                await session.execute(
                    select(AgentRecord).where(
                        AgentRecord.tenant_id == tenant_id,
                        AgentRecord.kind == "specialist",
                        AgentRecord.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            habilitados = await habilitados_del_negocio(tenant_id)

            for agente in agentes:
                try:
                    borrador = await versiones.obtener_borrador(tenant_id, agente.id)
                    contenido = borrador.content if isinstance(borrador.content, dict) else {}
                except Exception:
                    contenido = {}
                packs = await packs_del_agente(tenant_id, agente.id, habilitados)
                tools = {
                    str(b.get("tool"))
                    for b in contenido.get("tools") or []
                    if isinstance(b, dict) and b.get("tool") and b.get("enabled", True)
                }
                tools |= _tools_de_sus_procedimientos(contenido, packs)
                acciones = [a for a in registro_datos.de_packs(packs) if a.aplica_a(tools)]
                ids = {a.id for a in acciones}
                filtrada = {k: v for k, v in politica.items() if k in ids}
                if not filtrada and not herramientas:
                    continue
                nueva = con_herramientas(con_politica(agente.data_policy, filtrada), herramientas)
                if nueva == (agente.data_policy or {}):
                    continue
                resumen["agentes"].append(
                    {"agente_id": str(agente.id), "key": agente.key, "acciones": sorted(filtrada)}
                )
                if aplicar:
                    antes = dict(agente.data_policy or {})
                    agente.data_policy = nueva
                    session.add(
                        AuditLog(
                            tenant_id=tenant_id,
                            actor="script:migrar_datos_cliente_agentes",
                            action="agent.datos_cliente_migrado",
                            input={
                                "agente_id": str(agente.id),
                                "antes": antes,
                                "despues": nueva,
                            },
                        )
                    )

            if aplicar:
                nuevo_behavior = dict(behavior)
                bloque_nuevo = dict(bloque)
                bloque_nuevo.pop("politica", None)
                bloque_nuevo.pop("herramientas", None)
                if bloque_nuevo:
                    nuevo_behavior["datos_cliente"] = bloque_nuevo
                else:
                    nuevo_behavior.pop("datos_cliente", None)
                settings.behavior_config = nuevo_behavior
                session.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        actor="script:migrar_datos_cliente_agentes",
                        action="settings.datos_cliente_migrado",
                        input={
                            "politica": politica,
                            "herramientas": herramientas,
                            "agentes": resumen["agentes"],
                        },
                    )
                )

    return resumen


def _preparar_registro() -> None:
    """Carga los `datos.py` de los packs: el registro vive en el proceso y no siempre se importa solo."""
    import importlib

    for modulo in ("pack_commerce.datos", "pack_calendar.datos", "pack_subscriptions.datos"):
        try:
            importlib.import_module(modulo)
        except Exception:
            continue


async def _ejecutar(tenant: uuid.UUID | None, *, aplicar: bool) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    _preparar_registro()
    async with get_session() as session:
        consulta = select(Tenant.id).where(Tenant.deleted_at.is_(None))
        if tenant is not None:
            consulta = consulta.where(Tenant.id == tenant)
        ids = list((await session.execute(consulta)).scalars().all())

    salida: list[dict[str, Any]] = []
    for tenant_id in ids:
        resumen = await _migrar_negocio(tenant_id, aplicar=aplicar)
        if resumen["politica_global"] or resumen["agentes"]:
            salida.append(resumen)
    return salida


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=uuid.UUID, default=None, help="Solo este negocio.")
    parser.add_argument("--apply", action="store_true", help="Escribe los cambios (sin esto es dry-run).")
    args = parser.parse_args()

    resumen = asyncio.run(_ejecutar(args.tenant, aplicar=args.apply))
    modo = "APLICADO" if args.apply else "DRY-RUN"
    print(f"[{modo}] negocios con política global: {len(resumen)}")
    for fila in resumen:
        print(
            f"- {fila['tenant_id']}: {fila['politica_global']} acciones globales, "
            f"{len(fila['agentes'])} agentes con cambios"
        )
        for agente in fila["agentes"]:
            print(f"  · {agente['key']} ({agente['agente_id']}): {', '.join(agente['acciones']) or 'solo herramientas'}")
    if not args.apply and resumen:
        print("\nNada se escribió. Vuelve a ejecutarlo con --apply para aplicar.")


if __name__ == "__main__":
    main()
