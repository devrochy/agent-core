"""Escenario «Cobranza completa» de los datos de prueba (HU-QA-13, adenda de ADR-0135 §8).

Sobre una cartera nueva (la misma de «Preparar mis datos», sembrada con `sembrar_datos_de_prueba`
y el sufijo del caso): promesas pendiente, cumplida e incumplida; un cobro al propietario; un
gestor de campo con un dispositivo por aprobar y dos intentos de acceso (uno denegado); las reglas
de negociación del negocio; y el cierre de caja del día con su liquidación.

Determinista y aditivo. Ningún cron de cobranza envía mensajes y los clientes de la cartera no
tienen teléfono. El dispositivo pendiente es de un gestor aparte (sin cuenta), para no bloquear la
app de campo del gestor de prueba.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from core.db.session import get_session
from core.qa.contrato_datos import (
    ContextoDeNegocio,
    EntradaDeEscenario,
    EscenarioNoDisponible,
    ResumenDeEscenario,
)
from core.shared.tenant import get_current_tenant_id
from pack_cobranza.models import (
    Cartera,
    CobroPropietario,
    Device,
    Gestor,
    IntentoAcceso,
    Liquidacion,
    Prestamo,
    Promesa,
    ReglaNegociacion,
)

# (días hasta la fecha prometida, estado, tipo, quién la creó)
PROMESAS: tuple[tuple[int, str, str, str], ...] = (
    (3, "pendiente", "promesa", "ia"),
    (-5, "cumplida", "abono_parcial", "gestor"),
    (-2, "incumplida", "promesa", "agente"),
)


async def _cartera(tenant_id: uuid.UUID, entrada: EntradaDeEscenario) -> tuple[uuid.UUID, list[uuid.UUID]]:
    from pack_cobranza.datos_de_prueba import CobranzaDePrueba, formas, sembrar

    forma = next(f for f in formas() if f.modo == "cartera")
    datos = CobranzaDePrueba.model_validate(dict(forma.respaldo))
    contexto = ContextoDeNegocio(moneda=entrada.moneda, zona_horaria=entrada.zona_horaria, sufijo=entrada.sufijo)
    registros = await sembrar(tenant_id, "cartera", datos, contexto)
    carteras = [r.ref for r in registros if r.kind == "cartera"]
    if not carteras:
        raise EscenarioNoDisponible("No se pudo crear la cartera del escenario.")
    return carteras[0], [r.ref for r in registros if r.kind == "prestamo"]


async def sembrar(tenant_id: uuid.UUID, entrada: EntradaDeEscenario) -> ResumenDeEscenario:
    from core.admin.auth import hash_password
    from pack_cobranza.jobs import snapshot_cartera

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("escenario fuera del tenant_scope del negocio")
    cartera_id, prestamos_ids = await _cartera(tenant_id, entrada)
    hoy = date.today()
    ahora = datetime.now(UTC)
    suf = entrada.sufijo
    async with get_session() as session:
        cartera = (
            await session.execute(select(Cartera).where(Cartera.tenant_id == tenant_id, Cartera.id == cartera_id))
        ).scalar_one()
        prestamos = (
            await session.execute(
                select(Prestamo)
                .where(Prestamo.tenant_id == tenant_id, Prestamo.id.in_(prestamos_ids), Prestamo.estatus == "vigente")
                .order_by(Prestamo.created_at, Prestamo.id)
            )
        ).scalars().all()
        if not prestamos:
            raise EscenarioNoDisponible("La cartera del escenario no tiene préstamos vigentes.")
        for n, (dias, estado, tipo, quien) in enumerate(PROMESAS):
            prestamo = prestamos[n % len(prestamos)]
            session.add(
                Promesa(
                    prestamo_id=prestamo.id,
                    cliente_id=prestamo.cliente_id,
                    fecha_prometida=hoy + timedelta(days=dias),
                    valor_prometido=Decimal(str(prestamo.saldo or prestamo.valor)) / 4,
                    estado=estado,
                    tipo=tipo,
                    creado_por=quien,
                    idempotency_key=f"qa-escenario:{suf}:{n}",
                )
            )
        session.add(
            CobroPropietario(
                periodo=f"{hoy:%Y-%m}-{suf}",
                monto_calculado=Decimal("85000"),
                fecha_vencimiento=hoy + timedelta(days=5),
                estado="pendiente",
                registrado_por="escenario de prueba",
            )
        )
        gestor = Gestor(nombre=f"Gestor de campo {suf}", apellido="Escenario", estatus="activo")
        session.add(gestor)
        await session.flush()
        imei = "35" + "".join(secrets.choice("0123456789") for _ in range(13))
        dispositivo = Device(
            codigo=str(uuid.uuid4()),
            api_key_hash=hash_password(secrets.token_urlsafe(24)),
            gestor_id=gestor.id,
            cartera_id=cartera.id,
            imei=imei,
            estado="pendiente",
        )
        session.add(dispositivo)
        await session.flush()
        session.add_all(
            [
                IntentoAcceso(
                    gestor_id=gestor.id, device_id=dispositivo.id, imei=imei, resultado="denegado",
                    motivo="dispositivo pendiente de aprobación", ip="203.0.113.10",
                ),
                IntentoAcceso(
                    gestor_id=gestor.id, device_id=dispositivo.id, imei="35" + "0" * 13, resultado="denegado",
                    motivo="IMEI no registrado", ip="203.0.113.11",
                ),
                IntentoAcceso(gestor_id=gestor.id, imei=imei, resultado="ok", motivo=None, ip="203.0.113.10"),
            ]
        )
        await session.execute(
            insert(ReglaNegociacion)
            .values(
                tenant_id=tenant_id, max_dias_prorroga=10, min_abono_pct=Decimal("20"), max_reprogramaciones=2,
                umbral_saldo_autonomo=Decimal("300000"), alcance_promesa="ambos",
            )
            .on_conflict_do_update(
                index_elements=[ReglaNegociacion.tenant_id],
                set_={
                    "max_dias_prorroga": 10, "min_abono_pct": Decimal("20"), "max_reprogramaciones": 2,
                    "umbral_saldo_autonomo": Decimal("300000"), "alcance_promesa": "ambos", "updated_at": ahora,
                },
            )
        )
        await snapshot_cartera(session, tenant_id, cartera, hoy)
        suma = (
            await session.execute(
                select(func.coalesce(func.sum(Prestamo.saldo), 0)).where(
                    Prestamo.tenant_id == tenant_id, Prestamo.cartera_id == cartera.id, Prestamo.deleted_at.is_(None)
                )
            )
        ).scalar_one()
        session.add(
            Liquidacion(
                cartera_id=cartera.id,
                fecha=hoy,
                periodo="diario",
                caja_anterior=Decimal("500000"),
                caja_actual=Decimal("620000"),
                total_cobrado=Decimal("140000"),
                total_prestado=Decimal("0"),
                total_gastos=Decimal("20000"),
                comision_valor=Decimal("7000"),
                comision_porcentaje=Decimal("5"),
                estimado_acobrar=Decimal("180000"),
                total_inyeccion=Decimal("0"),
                suma_cartera=Decimal(str(suma)),
                detalle={"escenario": "cobranza-completa"},
                comentario="Cierre del escenario de prueba.",
            )
        )
    return ResumenDeEscenario(
        conteos={"carteras": 1, "promesas": len(PROMESAS), "dispositivos": 1, "intentos": 3, "liquidaciones": 1},
        detalle=[
            f"Cartera «{cartera.nombre}» con sus clientes y préstamos",
            "Promesas pendiente, cumplida e incumplida",
            "Un cobro al propietario pendiente",
            f"Un dispositivo por aprobar del «Gestor de campo {suf}» y tres intentos de acceso (dos denegados)",
            "Reglas de negociación y el cierre de caja del día con su liquidación",
        ],
    )
