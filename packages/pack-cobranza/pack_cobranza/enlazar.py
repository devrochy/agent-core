"""Enlace del `Cliente` de cobranza con el `Customer` del chat por teléfono (HU-CRM-15, ADR-0179 §7).

Las tools `cob_*` y el LeadProvider encuentran al deudor por `Cliente.customer_id`. Un deudor cargado
a mano (panel o app del gestor) nace sin ese enlace y no se reconoce cuando escribe por WhatsApp. Este
módulo lo enlaza comparando **solo dígitos** el `telefono_whatsapp` (texto libre) con
`Customer.contact_phone` (en WhatsApp viene del `wa_id`, sin garantía de `+`).

Tres puntos de entrada, todos dentro de `pack-cobranza` (no hay hook en la creación del `Customer`):

- `cliente_por_telefono`: camino perezoso de `skills._cliente_de_customer`, con caché negativo en Redis.
- `enlazar_cliente`: al crear un `Cliente` (panel, app del gestor) y en el backfill.
- `python -m pack_cobranza.enlazar [--dry-run]`: backfill de los clientes que ya existen.

Reglas: una sola coincidencia enlaza con `UPDATE … WHERE customer_id IS NULL` (idempotente y seguro bajo
carrera); dos o más no enlazan (`ambiguo`): se prefiere no enlazar a enlazar mal. Un `Customer` que ya
tiene otro `Cliente` enlazado no se vuelve a enlazar. El resultado se cuenta en el log
`cobranza_enlace{resultado, origen}` **sin PII** (ni teléfono, ni documento, ni nombre).

Limitaciones declaradas: `contact_phone` solo existe en WhatsApp, así que los clientes de Instagram u
otros canales no se enlazan; y un número local sin prefijo solo cruza con un `wa_id` internacional si el
prefijo es el del país por defecto (`PAIS_POR_DEFECTO`, hoy CO: no hay país por negocio).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import set_committed_value

from core.db.models import Customer, Tenant
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.telefono import PAIS_POR_DEFECTO, dividir_telefono, normalizar_telefono, pais
from core.shared.tenant import get_current_tenant_id, tenant_scope
from pack_cobranza.models import Cliente

log = get_logger(__name__)

# Debajo de esto no hay número que cruzar (un «1» o un «-» no identifican a nadie).
MIN_DIGITOS = 7
TTL_CACHE_NEGATIVO_S = 600
LOTE_POR_DEFECTO = 500
_NO_DIGITOS = re.compile(r"\D+")


class Resultado(StrEnum):
    ENLAZADO = "enlazado"
    AMBIGUO = "ambiguo"
    SIN_MATCH = "sin_match"
    YA_ENLAZADO = "ya_enlazado"


def digitos(valor: object) -> str:
    return _NO_DIGITOS.sub("", str(valor or ""))


def candidatos(telefono: object, pais_por_defecto: str = PAIS_POR_DEFECTO) -> set[str]:
    """Formas en dígitos con las que un teléfono puede estar guardado.

    Siempre la forma internacional (`573001234567`). La forma local (`3001234567`) solo si el número es
    del país por defecto: un local sin prefijo es ambiguo entre países, y un `wa_id` de otro país con los
    mismos dígitos finales no debe cruzar. Vacío si no hay dígitos suficientes.
    """
    if len(digitos(telefono)) < MIN_DIGITOS:
        return set()
    info = dividir_telefono(telefono, pais_por_defecto)
    out = {digitos(info["e164"])}
    por_defecto = pais(pais_por_defecto) or pais(PAIS_POR_DEFECTO)
    if por_defecto is not None and info["indicativo"] == por_defecto.prefijo and info["local"]:
        if len(str(info["local"])) >= MIN_DIGITOS:
            out.add(str(info["local"]))
    return out


def telefono_para_guardar(valor: object, pais_por_defecto: str = PAIS_POR_DEFECTO) -> str | None:
    """El teléfono en E.164 para guardarlo al crear un `Cliente`. Un texto sin dígitos suficientes
    (una extensión, una nota) se deja como vino: no se inventa un número."""
    texto = str(valor or "").strip()
    if len(digitos(texto)) < MIN_DIGITOS:
        return texto or None
    return normalizar_telefono(texto, pais_por_defecto)


def _tenant_actual() -> uuid.UUID:
    tid = get_current_tenant_id()
    if tid is None:
        raise RuntimeError("enlazar: fuera del tenant_scope del negocio")
    return tid


def _registrar(resultado: Resultado, origen: str) -> Resultado:
    # El backfill recorre miles de filas: su detalle va a debug y el total sale en `cobranza_enlace_backfill`.
    emitir = log.debug if origen == "backfill" else log.info
    emitir("cobranza_enlace", resultado=resultado.value, origen=origen, tenant_id=str(get_current_tenant_id()))
    return resultado


def _solo_digitos_sql(columna):
    return func.regexp_replace(columna, r"\D", "", "g")


async def _vincular(session, cliente_id: uuid.UUID, customer_id: uuid.UUID) -> bool:
    """`UPDATE … WHERE customer_id IS NULL`: de dos enlaces concurrentes gana uno solo. El `NOT EXISTS`
    evita que un `Customer` quede enlazado a dos clientes (la lectura perezosa espera uno)."""
    tid = _tenant_actual()
    otro = aliased(Cliente)
    resultado = await session.execute(
        update(Cliente)
        .where(
            Cliente.tenant_id == tid,
            Cliente.id == cliente_id,
            Cliente.customer_id.is_(None),
            Cliente.deleted_at.is_(None),
            ~exists().where(otro.tenant_id == tid, otro.customer_id == customer_id, otro.deleted_at.is_(None)),
        )
        .values(customer_id=customer_id)
    )
    return resultado.rowcount == 1


async def enlazar_customer(
    session, customer_id: uuid.UUID, *, pais_por_defecto: str = PAIS_POR_DEFECTO, origen: str = "perezoso"
) -> Resultado:
    """Del `Customer` al `Cliente`: busca el deudor sin enlazar cuyo teléfono coincide."""
    tid = _tenant_actual()
    telefono = (
        await session.execute(
            select(Customer.contact_phone).where(
                Customer.tenant_id == tid, Customer.id == customer_id, Customer.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    cands = candidatos(telefono, pais_por_defecto)
    if not cands:
        return _registrar(Resultado.SIN_MATCH, origen)
    ids = list(
        (
            await session.execute(
                select(Cliente.id)
                .where(
                    Cliente.tenant_id == tid,
                    Cliente.customer_id.is_(None),
                    Cliente.deleted_at.is_(None),
                    _solo_digitos_sql(Cliente.telefono_whatsapp).in_(cands),
                )
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return _registrar(Resultado.SIN_MATCH, origen)
    if len(ids) > 1:
        return _registrar(Resultado.AMBIGUO, origen)
    if await _vincular(session, ids[0], customer_id):
        return _registrar(Resultado.ENLAZADO, origen)
    return _registrar(Resultado.YA_ENLAZADO, origen)


async def enlazar_cliente(
    session,
    cliente: Cliente,
    *,
    pais_por_defecto: str = PAIS_POR_DEFECTO,
    origen: str = "alta",
    aplicar: bool = True,
) -> Resultado:
    """Del `Cliente` al `Customer` de WhatsApp. Con `aplicar=False` (dry-run) no escribe y devuelve lo
    que haría. El `Cliente` tiene que estar insertado (`flush`)."""
    if cliente.customer_id is not None:
        return _registrar(Resultado.YA_ENLAZADO, origen)
    tid = _tenant_actual()
    cands = candidatos(cliente.telefono_whatsapp, pais_por_defecto)
    if not cands:
        return _registrar(Resultado.SIN_MATCH, origen)
    customers = list(
        (
            await session.execute(
                select(Customer.id)
                .where(
                    Customer.tenant_id == tid,
                    Customer.channel == "whatsapp",
                    Customer.deleted_at.is_(None),
                    _solo_digitos_sql(Customer.contact_phone).in_(cands),
                )
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not customers:
        return _registrar(Resultado.SIN_MATCH, origen)
    # Dos deudores con el mismo teléfono no se enlazan (ADR-0179 §7); tampoco dos customers.
    hermano = (
        await session.execute(
            select(Cliente.id)
            .where(
                Cliente.tenant_id == tid,
                Cliente.id != cliente.id,
                Cliente.customer_id.is_(None),
                Cliente.deleted_at.is_(None),
                _solo_digitos_sql(Cliente.telefono_whatsapp).in_(cands),
            )
            .limit(1)
        )
    ).first()
    if len(customers) > 1 or hermano is not None:
        return _registrar(Resultado.AMBIGUO, origen)
    ocupado = (
        await session.execute(
            select(Cliente.id)
            .where(Cliente.tenant_id == tid, Cliente.customer_id == customers[0], Cliente.deleted_at.is_(None))
            .limit(1)
        )
    ).first()
    if ocupado is not None:
        return _registrar(Resultado.YA_ENLAZADO, origen)
    if not aplicar:
        return _registrar(Resultado.ENLAZADO, origen)
    if await _vincular(session, cliente.id, customers[0]):
        # El UPDATE fue directo a la base: se refleja en el objeto sin marcarlo sucio.
        set_committed_value(cliente, "customer_id", customers[0])
        return _registrar(Resultado.ENLAZADO, origen)
    return _registrar(Resultado.YA_ENLAZADO, origen)


# --------------------------------------------------------------------- camino perezoso
def _clave_negativa(tenant_id: uuid.UUID, customer_id: uuid.UUID) -> str:
    return f"tenant:{tenant_id}:cobr_enlace_neg:{customer_id}"


async def cliente_por_telefono(session, customer_id: uuid.UUID) -> Cliente | None:
    """Camino perezoso de `_cliente_de_customer`: si el `Customer` no tiene `Cliente` enlazado, intenta
    enlazarlo por teléfono. `sin_match` y `ambiguo` se recuerdan 10 min en Redis para no consultar en
    cada tool; si Redis falla, se intenta igual (nunca debe romper una tool)."""
    tid = _tenant_actual()
    clave = _clave_negativa(tid, customer_id)
    try:
        from core.memory.working import get_redis

        if await get_redis().get(clave):
            return None
    except Exception:
        log.warning("cobranza_enlace_redis_no_disponible", operacion="leer")
    resultado = await enlazar_customer(session, customer_id, origen="perezoso")
    if resultado in (Resultado.SIN_MATCH, Resultado.AMBIGUO):
        try:
            from core.memory.working import get_redis

            await get_redis().set(clave, resultado.value, ex=TTL_CACHE_NEGATIVO_S)
        except Exception:
            log.warning("cobranza_enlace_redis_no_disponible", operacion="escribir")
        return None
    if resultado is not Resultado.ENLAZADO:
        return None
    return (
        await session.execute(
            select(Cliente).where(
                Cliente.tenant_id == tid, Cliente.customer_id == customer_id, Cliente.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------- backfill (CLI)
@dataclass
class ReporteEnlace:
    dry_run: bool
    negocios: int = 0
    revisados: int = 0
    resultados: Counter[str] = field(default_factory=Counter)
    # Último punto procesado, para reanudar con `--tenant … --desde-id …`.
    cursor: tuple[uuid.UUID, uuid.UUID] | None = None


async def _resolver_tenant(valor: str) -> uuid.UUID:
    try:
        return uuid.UUID(valor)
    except ValueError:
        pass
    async with get_session() as session:
        tid = (
            await session.execute(select(Tenant.id).where(Tenant.slug == valor, Tenant.deleted_at.is_(None)))
        ).scalar_one_or_none()
    if tid is None:
        raise SystemExit(f"No existe el negocio '{valor}'.")
    return tid


async def _enlazar_negocio(
    tid: uuid.UUID,
    reporte: ReporteEnlace,
    *,
    desde: uuid.UUID | None,
    lote: int,
    pais_por_defecto: str,
) -> None:
    cursor = desde
    while True:
        async with tenant_scope(tid), get_session() as session:
            consulta = select(Cliente).where(
                Cliente.tenant_id == tid,
                Cliente.customer_id.is_(None),
                Cliente.deleted_at.is_(None),
                Cliente.telefono_whatsapp.is_not(None),
            )
            if cursor is not None:
                consulta = consulta.where(Cliente.id > cursor)
            filas = list((await session.execute(consulta.order_by(Cliente.id).limit(lote))).scalars().all())
            for cliente in filas:
                resultado = await enlazar_cliente(
                    session,
                    cliente,
                    pais_por_defecto=pais_por_defecto,
                    origen="backfill",
                    aplicar=not reporte.dry_run,
                )
                reporte.revisados += 1
                reporte.resultados[resultado.value] += 1
                reporte.cursor = (tid, cliente.id)
        if len(filas) < lote:
            return
        cursor = filas[-1].id


async def enlazar_existentes(
    *,
    dry_run: bool,
    tenant_id: uuid.UUID | None = None,
    desde_id: uuid.UUID | None = None,
    lote: int = LOTE_POR_DEFECTO,
    pais_por_defecto: str = PAIS_POR_DEFECTO,
) -> ReporteEnlace:
    """Backfill: enlaza los clientes existentes sin `customer_id`. Idempotente y resumible."""
    if lote < 1:
        raise ValueError("El lote tiene que ser al menos 1")
    if desde_id is not None and tenant_id is None:
        raise ValueError("--desde-id exige --tenant: el cursor es de un solo negocio")
    reporte = ReporteEnlace(dry_run=dry_run)
    if tenant_id is not None:
        negocios = [tenant_id]
    else:
        async with get_session() as session:
            negocios = list(
                (await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)).order_by(Tenant.id)))
                .scalars()
                .all()
            )
    for negocio in negocios:
        reporte.negocios += 1
        await _enlazar_negocio(negocio, reporte, desde=desde_id, lote=lote, pais_por_defecto=pais_por_defecto)
    log.info(
        "cobranza_enlace_backfill",
        dry_run=dry_run,
        negocios=reporte.negocios,
        revisados=reporte.revisados,
        resultados=dict(reporte.resultados),
    )
    return reporte


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m pack_cobranza.enlazar",
        description="Enlaza los clientes de cobranza con el Customer del chat por teléfono (HU-CRM-15).",
    )
    p.add_argument("--dry-run", action="store_true", help="No escribe: cuenta lo que haría.")
    p.add_argument("--tenant", help="Solo este negocio (slug o UUID).")
    p.add_argument("--desde-id", help="Reanuda después de este Cliente.id (exige --tenant).")
    p.add_argument("--lote", type=int, default=LOTE_POR_DEFECTO, help="Clientes por transacción.")
    p.add_argument("--pais", default=PAIS_POR_DEFECTO, help="País por defecto para números locales (ISO, ej. CO).")
    return p


async def _correr(args: argparse.Namespace) -> ReporteEnlace:
    tenant_id = await _resolver_tenant(args.tenant) if args.tenant else None
    desde = uuid.UUID(args.desde_id) if args.desde_id else None
    return await enlazar_existentes(
        dry_run=args.dry_run, tenant_id=tenant_id, desde_id=desde, lote=args.lote, pais_por_defecto=args.pais
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.desde_id and not args.tenant:
        print("--desde-id exige --tenant.", file=sys.stderr)
        return 2
    reporte = asyncio.run(_correr(args))
    print(("DRY-RUN (no se escribió nada) — " if reporte.dry_run else "") + f"negocios: {reporte.negocios}")
    print(f"clientes revisados: {reporte.revisados}")
    for clave in (r.value for r in Resultado):
        print(f"  {clave}: {reporte.resultados.get(clave, 0)}")
    if reporte.cursor is not None:
        print(f"último procesado: --tenant {reporte.cursor[0]} --desde-id {reporte.cursor[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
