"""Retención de los streams de entrada (ADR-0053: no retener dato personal sin necesidad).

`agent_core:inbound` y su DLQ llevan el texto y el teléfono de los clientes en los
campos. Sin recorte crecían para siempre (`XADD` sin límite, `XACK` sin `XDEL`).

- **Lo procesado** se borra al ackear (`XDEL` en el consumer). Como red (un `XDEL`
  fallido, o lo ackeado antes de este cambio), `recortar_inbound` quita con
  `XTRIM MINID` lo más viejo que la retención, pero el corte nunca pasa del pendiente
  más viejo (PEL) ni del último entregado de ningún grupo: lo pendiente o no leído
  no se toca. No se usa `XADD MAXLEN`: en Redis 7 recorta sin mirar el PEL y podría
  botar mensajes sin procesar si hay atraso.
- **La DLQ** no tiene grupos: se recorta por antigüedad (`recortar_dlq`), dejando
  tiempo para revisar y reintentar desde el panel.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.shared.logger import get_logger

log = get_logger(__name__)

_LOTE_DLQ = 500
_HORA_MS = 3_600_000

StreamId = tuple[int, int]


def _parse_id(entry_id: str) -> StreamId:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


def _fmt_id(sid: StreamId) -> str:
    return f"{sid[0]}-{sid[1]}"


async def _ahora_ms(r: Any) -> int:
    """Hora de Redis (la que genera los IDs), no la de la réplica: un reloj adelantado
    no debe recortar antes de tiempo."""
    segundos, micros = await r.time()
    return int(segundos) * 1000 + int(micros) // 1000


def _corte_por_antiguedad(horas: int, ahora_ms: int) -> StreamId:
    return max(ahora_ms - horas * _HORA_MS, 0), 0


async def corte_seguro_inbound(r: Any, stream: str, horas: int) -> StreamId | None:
    """ID mínimo a conservar en `stream` (todo lo anterior se puede borrar), o `None`.

    Es el menor de: el corte por antigüedad, y por cada grupo, su pendiente más viejo
    (PEL) y el siguiente a su último entregado. Así lo que queda antes del corte está
    ackeado por TODOS los grupos. Sin stream o sin grupos devuelve `None`: nada consta
    como procesado y no se recorta.
    """
    try:
        grupos = await r.xinfo_groups(stream)
    except Exception as exc:
        if "no such key" in str(exc).lower():
            return None
        raise
    if not grupos:
        return None
    ahora = await _ahora_ms(r)
    corte = _corte_por_antiguedad(horas, ahora)
    for g in grupos:
        ms, seq = _parse_id(str(g["last-delivered-id"]))
        corte = min(corte, (ms, seq + 1))
        if int(g.get("pending") or 0):
            resumen = await r.xpending(stream, g["name"])
            if resumen and resumen.get("min"):
                corte = min(corte, _parse_id(str(resumen["min"])))
    if corte[0] < ahora - 2 * horas * _HORA_MS:
        # Un pendiente atascado o un atraso de lectura frena la retención: lo ackeado
        # posterior se queda. Se avisa (solo IDs, sin campos) para que no pase en silencio.
        log.warning(
            "inbound_trim_blocked_by_pending",
            stream=stream,
            corte_id=_fmt_id(corte),
            horas_atras=(ahora - corte[0]) // _HORA_MS,
        )
    return corte


async def recortar_inbound(r: Any, stream: str, horas: int) -> int:
    """Borra lo ya procesado por todos los grupos y más viejo que `horas`. Devuelve cuántas."""
    corte = await corte_seguro_inbound(r, stream, horas)
    if corte is None:
        return 0
    # Exacto (sin `~`): la retención es una promesa de privacidad; con el `XDEL` al
    # ackear, lo que queda por recortar es poco.
    return int(await r.xtrim(stream, minid=_fmt_id(corte), approximate=False))


async def recortar_dlq(r: Any, stream: str, horas: int) -> int:
    """Borra de la DLQ lo que lleva más de `horas` ahí. Devuelve cuántas."""
    corte = _corte_por_antiguedad(horas, await _ahora_ms(r))
    return int(await r.xtrim(stream, minid=_fmt_id(corte), approximate=False))


def _es_del_tenant(campos: Any, tenant_id: uuid.UUID) -> bool:
    """Compara el campo `tenant_id` como UUID (mayúsculas/sin guiones); inválido = ajeno."""
    try:
        return uuid.UUID(str(dict(campos).get("tenant_id", ""))) == tenant_id
    except ValueError:
        return False


async def borrar_dlq_del_tenant(r: Any, stream: str, tenant_id: str) -> int:
    """`XDEL` de las entradas de la DLQ de un tenant (purga). Devuelve cuántas.

    Compara el campo `tenant_id` como UUID; recorre la DLQ por lotes (acotada por su
    retención). No toca `agent_core:inbound`: lo pendiente de un negocio purgado lo
    descarta el consumer (y con el `XDEL` al ackear, se borra).
    """
    tid = uuid.UUID(tenant_id)
    borradas = 0
    desde = "-"
    while True:
        filas = await r.xrange(stream, min=desde, count=_LOTE_DLQ)
        if not filas:
            return borradas
        suyas = [eid for eid, campos in filas if _es_del_tenant(campos, tid)]
        if suyas:
            borradas += int(await r.xdel(stream, *suyas))
        if len(filas) < _LOTE_DLQ:
            return borradas
        desde = "(" + str(filas[-1][0])
