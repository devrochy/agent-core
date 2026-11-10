"""Acciones que un agente de una release debe tener concedidas y no tiene (HU-ARCH-34, enmienda de
ADR-0019). Solo lectura. Se corre antes de encender el fail-closed: un faltante pierde esa acción en
vivo, y como las versiones y las releases son inmutables se corrige publicando una nueva.

Por agente de las releases activa y canary de cada conexión de un negocio v3 (el Coordinador no llama
tools):

- `core`: las del core que el turno ofrece siempre, sin las `sub_*`, que son de un enfoque y no de
  todos los especialistas (HU-AOS-51);
- `flujo`: las del flujo de la release, solo para el agente por defecto de la conexión.

Cuentan como concedidas las que tienen binding (habilitado o no: un binding deshabilitado ya es una
decisión), las declarativas de la versión y las de sus procedimientos publicados (ADR-0118 §3). Las del flujo
en un especialista no son faltantes, se cuentan aparte: el turno deja de ofrecérselas.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from core.shared.logger import get_logger

log = get_logger(__name__)

MOTIVO_CORE, MOTIVO_FLUJO = "core", "flujo"


@dataclass(frozen=True)
class Faltante:
    negocio: str
    conexion: uuid.UUID
    release: int
    agente: str
    version: int
    accion: str
    motivo: str


@dataclass
class ReporteBindings:
    faltantes: list[Faltante] = field(default_factory=list)
    agentes_revisados: int = 0
    # Acciones del flujo sin binding en especialistas: `(negocio, agente) → cuántas`.
    fuera_de_alcance: dict[tuple[str, str], int] = field(default_factory=dict)
    # Negocios o conexiones que no se pudieron revisar: un reporte parcial no vale como 0.
    errores: list[str] = field(default_factory=list)

    @property
    def limpio(self) -> bool:
        return not self.faltantes and not self.errores


def declaradas_con_binding(contenido: Mapping[str, Any]) -> set[str]:
    """Acciones con binding en la versión (habilitado o no) y sus declarativas."""
    concedidas = {str(b.get("tool")) for b in contenido.get("tools") or [] if isinstance(b, Mapping)}
    concedidas |= {str(t.get("name")) for t in contenido.get("declarativeTools") or [] if isinstance(t, Mapping)}
    return concedidas


def core_esperadas(siempre: Iterable[str]) -> set[str]:
    return {nombre for nombre in siempre if not nombre.startswith("sub_")}


def sin_binding(
    concedidas: Collection[str],
    *,
    del_core: Iterable[str],
    del_flujo: Iterable[str],
    es_por_defecto: bool,
) -> tuple[dict[str, str], set[str]]:
    """`(faltantes por acción con su motivo, fuera de alcance)`."""
    faltantes: dict[str, str] = {}
    fuera = set(del_flujo) - set(concedidas)
    if es_por_defecto:
        faltantes.update(dict.fromkeys(fuera, MOTIVO_FLUJO))
        fuera = set()
    faltantes.update(dict.fromkeys(set(del_core) - set(concedidas), MOTIVO_CORE))
    return faltantes, fuera - set(faltantes)


async def _revisar_release(
    reporte: ReporteBindings, negocio: str, release: Any, catalogo: Mapping[str, Any], siempre: set[str]
) -> None:
    from core.kernel.opciones.lint import acciones_de_procedimientos

    for agente in release.componentes:
        if agente.role == "core":
            continue
        reporte.agentes_revisados += 1
        # Los mismos que concede el turno: los publicados que la versión declara.
        propios = release.procedimientos_de(agente.instance_id).values()
        compilado = await release.compilado(agente)
        flujo = compilado.flujo if compilado is not None else None
        faltantes, fuera = sin_binding(
            declaradas_con_binding(agente.content) | acciones_de_procedimientos(propios),
            del_core=core_esperadas(siempre),
            # Una acción retirada del binario no se ofrece: no hay binding que agregarle.
            del_flujo=[t for t in flujo.tool_names if t in catalogo] if flujo is not None else (),
            es_por_defecto=agente.is_default,
        )
        for accion, motivo in sorted(faltantes.items()):
            reporte.faltantes.append(
                Faltante(negocio, release.connection_id, release.number, agente.key, agente.version, accion, motivo)
            )
        if fuera:
            clave = (negocio, agente.key)
            reporte.fuera_de_alcance[clave] = reporte.fuera_de_alcance.get(clave, 0) + len(fuera)


def _acciones_siempre_ofrecidas(catalogo: Mapping[str, Any]) -> set[str]:
    from core.kernel.procedures.catalogo import CORE_OWNER
    from core.subscriptions.tools import SOLO_PROCEDIMIENTO

    return {nombre for nombre, info in catalogo.items() if info.owner == CORE_OWNER} - set(SOLO_PROCEDIMIENTO)


async def reporte_de_bindings(
    modules: Iterable[Any], tenant_ids: Iterable[uuid.UUID] | None = None
) -> ReporteBindings:
    """Revisa las releases activa y canary de los negocios v3 (todos, o solo `tenant_ids`). Un negocio que
    falla no frena a los demás y queda en `errores`."""
    from core.db.session import get_session
    from core.kernel.banderas import negocios_v3
    from core.kernel.procedures.catalogo import catalogo_de_acciones
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    reporte = ReporteBindings()
    catalogo = catalogo_de_acciones(modules)
    siempre = _acciones_siempre_ofrecidas(catalogo)
    async with get_session() as session:
        negocios = await negocios_v3(session)
    if tenant_ids is not None:
        pedidos = set(tenant_ids)
        negocios = [t for t in negocios if t in pedidos]
    for tenant_id in negocios:
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    slug = (
                        await session.execute(text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id})
                    ).scalar_one()
                    filas = (
                        await session.execute(
                            text(
                                "SELECT active_release_id, canary_release_id FROM channel_connections "
                                "WHERE tenant_id = :t AND deleted_at IS NULL ORDER BY id"
                            ),
                            {"t": tenant_id},
                        )
                    ).all()
                    for release_id in sorted({r for fila in filas for r in fila if r is not None}):
                        release = await cargar_release(session, tenant_id, release_id)
                        if release is not None:
                            await _revisar_release(reporte, slug, release, catalogo, siempre)
        except Exception as exc:
            log.warning("reporte_bindings_negocio_fallo", tenant_id=str(tenant_id), exc_info=True)
            reporte.errores.append(f"{tenant_id}: {type(exc).__name__}")
    return reporte


__all__ = ["Faltante", "ReporteBindings", "reporte_de_bindings", "sin_binding"]
