"""Siembra del catálogo de planes desde `planes.json` (SPEC-43, HU-SUB-32, ADR-0177).

`{"planes": [{codigo, nombre, descripcion, plan_plataforma, periodicidad, trial_dias, entitlements,
orden, precio?}]}`. El archivo es dato del negocio (`TENANTS_ROOT/<slug>/planes.json`), no código.

- **Idempotente:** upsert por `(tenant, codigo)` entre los planes no borrados; sembrar dos veces deja los mismos planes.
- **Todo o nada:** se valida el archivo completo antes de escribir; un plan inválido (código mal formado,
  prueba en un plan de pago, plan de plataforma inexistente) aborta sin tocar ninguno.
- **El precio sigue a la plataforma:** con `plan_plataforma`, el `precio` del archivo se ignora y se usa el
  de `core/billing/plans.py` (el cron `sincronizar_precios_de_plan` lo mantiene).
- **El archivo gana:** una re-siembra reescribe los campos del plan con lo que dice el archivo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.shared.tenant import tenant_scope
from pack_subscriptions.models import Plan
from pack_subscriptions.planes import DatosDePlan, PlanError, _aplicar, validar
from pack_subscriptions.precios_de_plan import precio_de_plataforma


class SiembraInvalida(ValueError):
    """El `planes.json` tiene un plan inválido: no se escribió nada."""


def _datos(item: Any, posicion: int) -> DatosDePlan:
    if not isinstance(item, dict):
        raise SiembraInvalida(f"planes[{posicion}]: debe ser un objeto")
    plataforma = str(item.get("plan_plataforma") or "").strip().lower() or None
    if plataforma is not None:
        precio = precio_de_plataforma(plataforma)
        if precio is None:
            raise SiembraInvalida(f"planes[{posicion}]: el plan de la plataforma {plataforma!r} no existe")
    else:
        precio = item.get("precio")
        if precio is None:
            raise SiembraInvalida(f"planes[{posicion}]: falta el precio (o el plan_plataforma)")
    try:
        return validar(
            DatosDePlan(
                codigo=str(item.get("codigo") or ""),
                nombre=str(item.get("nombre") or ""),
                descripcion=str(item.get("descripcion") or ""),
                precio=Decimal(str(precio)),
                moneda=str(item.get("moneda") or "COP"),
                periodicidad=str(item.get("periodicidad") or "mensual"),
                trial_dias=int(item.get("trial_dias") or 0),
                entitlements=item.get("entitlements") if isinstance(item.get("entitlements"), dict) else None,
                plan_plataforma=plataforma,
                activo=bool(item.get("activo", True)),
                orden=int(item.get("orden") or posicion),
            )
        )
    except (PlanError, ValueError, TypeError) as exc:
        raise SiembraInvalida(f"planes[{posicion}] ({item.get('codigo')!r}): {exc}") from exc


async def seed_planes(tenant_id: uuid.UUID, data: dict[str, Any]) -> list[str]:
    """Siembra los planes de `data` en el negocio. Devuelve los pasos hechos (para el reporte del CLI)."""
    items = data.get("planes")
    if not isinstance(items, list) or not items:
        raise SiembraInvalida("el archivo no trae una lista `planes`")
    planes = [_datos(item, i) for i, item in enumerate(items)]
    codigos = [p.codigo.lower() for p in planes]
    if len(set(codigos)) != len(codigos):
        raise SiembraInvalida("hay códigos de plan repetidos en el archivo")

    pasos: list[str] = []
    async with tenant_scope(tenant_id):
        async with get_session() as session:  # una transacción: todo o nada
            for datos in planes:
                existente = (
                    await session.execute(
                        select(Plan).where(
                            Plan.tenant_id == tenant_id,
                            Plan.codigo == datos.codigo,
                            Plan.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if existente is None:
                    existente = Plan(tenant_id=tenant_id)
                    _aplicar(existente, datos)
                    session.add(existente)
                    pasos.append(f"plan {datos.codigo} creado")
                else:
                    _aplicar(existente, datos)
                    pasos.append(f"plan {datos.codigo} actualizado")
            await session.flush()
    return pasos
