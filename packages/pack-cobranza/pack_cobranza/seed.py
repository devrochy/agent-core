"""Datos de ejemplo de la cartera (`seed_data`, ADR-0083): carteras con caja, gestor
asignado y clientes con su préstamo, cuotas pagadas y cuotas en mora.

Lo usan los datos de prueba de cada tester (HU-QA-07) con el fragmento `presets/qa.json`.
Idempotente: una cartera se reconoce por su nombre y un cliente por su documento (o por
su nombre) dentro de la cartera; un cliente que ya tiene préstamo no recibe otro.

Formato de `data`:

    {"carteras": [{
        "nombre": "Cartera Centro", "tipo_interes": 20, "num_cuotas": 20, "saldo_caja": 3000000,
        "gestor": {"correo": "gestor@x.test", "permisos": "todos"},
        "clientes": [{
            "nombre": "Juana", "apellido": "Pérez", "numero_documento": "1000000001",
            "color_riesgo": "rojo",
            "prestamo": {"valor": 500000, "num_cuotas": 20, "tipo_interes": 20,
                         "dias_entre_cuotas": 1, "hace_dias": 15, "cuotas_pagadas": 5}
        }]
    }]}

`hace_dias` es cuántos días atrás se otorgó: las cuotas vencidas sin pagar quedan
atrasadas (mora) y las demás pendientes. Los clientes no traen teléfono: así ningún
recordatorio llega a un número real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.shared.tenant import tenant_scope
from pack_cobranza.models import (
    Caja,
    Cartera,
    Cliente,
    Cuota,
    Gestor,
    GestorPermiso,
    Pago,
    Prestamo,
)
from pack_cobranza.permisos import GESTOR_PERMISOS

_COLORES = {"blanco", "azul", "rojo"}


def _dec(valor: Any) -> Decimal:
    return Decimal(str(valor or 0))


Creados = list[tuple[str, uuid.UUID]]


async def sembrar_cartera(tenant_id: uuid.UUID, data: dict[str, Any], creados: Creados | None = None) -> list[str]:
    """`creados` (datos de prueba, HU-QA-10): recibe `(kind, id)` de cada cartera, cliente,
    préstamo, cuota y pago que se crea."""
    pasos: list[str] = []
    registro: Creados = creados if creados is not None else []
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for item in data.get("carteras") or []:
                pasos.extend(await _sembrar_una(session, tenant_id, item, registro))
    return pasos


async def _sembrar_una(session, tenant_id: uuid.UUID, item: dict[str, Any], creados: Creados) -> list[str]:  # type: ignore[no-untyped-def]
    nombre = str(item.get("nombre") or "").strip()
    if not nombre:
        return []
    pasos: list[str] = []
    cartera = (
        await session.execute(
            select(Cartera).where(Cartera.tenant_id == tenant_id, Cartera.nombre == nombre, Cartera.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if cartera is None:
        cartera = Cartera(
            tenant_id=tenant_id,
            nombre=nombre,
            descripcion=item.get("descripcion"),
            tipo_interes=_dec(item.get("tipo_interes")),
            num_cuotas=int(item.get("num_cuotas") or 1),
            moneda=str(item.get("moneda") or "COP"),
        )
        session.add(cartera)
        await session.flush()
        creados.append(("cartera", cartera.id))
        pasos.append(f"cartera {nombre} creada")

    caja = (
        await session.execute(select(Caja).where(Caja.tenant_id == tenant_id, Caja.cartera_id == cartera.id))
    ).scalar_one_or_none()
    if caja is None:
        saldo = _dec(item.get("saldo_caja"))
        caja = Caja(tenant_id=tenant_id, cartera_id=cartera.id, saldo_inicial=saldo, saldo_actual=saldo)
        session.add(caja)
        await session.flush()

    gestor_cfg = item.get("gestor") or {}
    if gestor_cfg.get("correo") and cartera.gestor_id is None:
        gestor = (
            await session.execute(
                select(Gestor).where(
                    Gestor.tenant_id == tenant_id,
                    Gestor.correo == str(gestor_cfg["correo"]).strip().lower(),
                    Gestor.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if gestor is not None:
            cartera.gestor_id = gestor.id
            await _dar_permisos(session, tenant_id, gestor.id, gestor_cfg.get("permisos"))
            pasos.append(f"cartera {nombre} asignada al gestor")

    for cli in item.get("clientes") or []:
        paso = await _sembrar_cliente(session, tenant_id, cartera, caja, cli, creados)
        if paso:
            pasos.append(paso)
    return pasos


async def _dar_permisos(session, tenant_id: uuid.UUID, gestor_id: uuid.UUID, permisos: Any) -> None:  # type: ignore[no-untyped-def]
    lista = list(GESTOR_PERMISOS) if permisos == "todos" else [p for p in (permisos or []) if p in GESTOR_PERMISOS]
    existentes = {
        r.permiso: r
        for r in (
            await session.execute(
                select(GestorPermiso).where(GestorPermiso.tenant_id == tenant_id, GestorPermiso.gestor_id == gestor_id)
            )
        ).scalars()
    }
    for permiso in lista:
        if permiso in existentes:
            existentes[permiso].habilitado = True
        else:
            session.add(GestorPermiso(tenant_id=tenant_id, gestor_id=gestor_id, permiso=permiso, habilitado=True))


async def _sembrar_cliente(session, tenant_id: uuid.UUID, cartera: Cartera, caja: Caja, cli: dict[str, Any], creados: Creados) -> str | None:  # type: ignore[no-untyped-def]
    nombre = str(cli.get("nombre") or "").strip()
    if not nombre:
        return None
    apellido = (str(cli.get("apellido") or "").strip()) or None
    documento = (str(cli.get("numero_documento") or "").strip()) or None
    q = select(Cliente).where(Cliente.tenant_id == tenant_id, Cliente.cartera_id == cartera.id, Cliente.deleted_at.is_(None))
    q = q.where(Cliente.numero_documento == documento) if documento else q.where(Cliente.nombre == nombre, Cliente.apellido == apellido)
    cliente = (await session.execute(q)).scalar_one_or_none()
    creado = cliente is None
    if cliente is None:
        color = str(cli.get("color_riesgo") or "blanco")
        cliente = Cliente(
            tenant_id=tenant_id,
            cartera_id=cartera.id,
            nombre=nombre,
            apellido=apellido,
            negocio=cli.get("negocio"),
            tipo_documento="CC" if documento else None,
            numero_documento=documento,
            color_riesgo=color if color in _COLORES else "blanco",
        )
        session.add(cliente)
        await session.flush()
        creados.append(("cliente", cliente.id))

    prestamo_cfg = cli.get("prestamo")
    if prestamo_cfg:
        tiene = (
            await session.execute(
                select(Prestamo.id).where(
                    Prestamo.tenant_id == tenant_id, Prestamo.cliente_id == cliente.id, Prestamo.deleted_at.is_(None)
                )
            )
        ).first()
        if tiene is None:
            await _sembrar_prestamo(session, tenant_id, cartera, caja, cliente, prestamo_cfg, creados)
    etiqueta = f"{nombre} {apellido or ''}".strip()
    return f"cliente {etiqueta} creado" if creado else None


async def _sembrar_prestamo(session, tenant_id: uuid.UUID, cartera: Cartera, caja: Caja, cliente: Cliente, cfg: dict[str, Any], creados: Creados) -> None:  # type: ignore[no-untyped-def]
    # Mismo cálculo que el alta de préstamo de la app de campo (`api_gestor.crear_prestamo`).
    valor = _dec(cfg.get("valor"))
    num_cuotas = max(1, int(cfg.get("num_cuotas") or cartera.num_cuotas or 1))
    interes = _dec(cfg.get("tipo_interes", cartera.tipo_interes))
    dias = max(1, int(cfg.get("dias_entre_cuotas") or 1))
    otorgado = date.today() - timedelta(days=max(0, int(cfg.get("hace_dias") or 0)))
    pagadas = max(0, min(num_cuotas, int(cfg.get("cuotas_pagadas") or 0)))
    total = (valor * (Decimal(1) + interes / Decimal(100))).quantize(Decimal("1"))
    cuota_valor = (total / num_cuotas).quantize(Decimal("1"))

    prestamo = Prestamo(
        tenant_id=tenant_id,
        cliente_id=cliente.id,
        cartera_id=cartera.id,
        valor=valor,
        saldo=total - cuota_valor * pagadas,
        num_cuotas=num_cuotas,
        tipo_interes=interes,
        dias_entre_cuotas=dias,
        fecha_otorgado=otorgado,
    )
    session.add(prestamo)
    await session.flush()
    creados.append(("prestamo", prestamo.id))
    hoy = date.today()
    for i in range(1, num_cuotas + 1):
        vence = otorgado + timedelta(days=i * dias)
        estatus = "pagada" if i <= pagadas else ("atrasada" if vence < hoy else "pendiente")
        cuota = Cuota(
            tenant_id=tenant_id,
            prestamo_id=prestamo.id,
            numero_cuota=i,
            valor_esperado=cuota_valor,
            fecha_vencimiento=vence,
            estatus=estatus,
        )
        session.add(cuota)
        await session.flush()
        creados.append(("cuota", cuota.id))
        if estatus == "pagada":
            pago = Pago(
                tenant_id=tenant_id,
                cliente_id=cliente.id,
                prestamo_id=prestamo.id,
                cuota_id=cuota.id,
                valor=cuota_valor,
                metodo_pago="efectivo",
                registrado_por="propietario",
                fecha_hora=datetime.combine(vence, datetime.min.time(), tzinfo=UTC) + timedelta(hours=15),
            )
            session.add(pago)
            await session.flush()
            creados.append(("pago", pago.id))
    caja.saldo_actual = caja.saldo_actual - valor + cuota_valor * pagadas
    await session.flush()
