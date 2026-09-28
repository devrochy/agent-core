"""Drenaje de la cola de salida del connector, con política anti-baneo (ADR-0074).

El `connector` toma un job de `waweb:outbox:<tenant>`, espera un delay humano
(aleatorio entre el mínimo y el máximo de la política del tenant), lo envía por
el socket y publica la respuesta en `waweb:reply:<request_id>` para que el worker
Celery que espera (`core.waweb.handoff.request_send`) reciba el wamid o el error.

El delay y el tope son la mitigación anti-baneo; no son una garantía.
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from typing import Any, Protocol

from core.waweb import handoff
from core.waweb.handoff import outbox_key, reply_key
from core.waweb.policy import WaWebPolicy


class SendBackend(Protocol):
    """Lo mínimo que el drenaje necesita del socket. Lo implementa el backend
    Neonize (o un fake de test)."""

    async def send_text(
        self, *, connection_id: str, to: str, text: str, image_url: str | None = None
    ) -> str: ...


async def process_job(
    job: dict[str, Any],
    backend: SendBackend,
    policy: WaWebPolicy,
    *,
    sleep: Any = asyncio.sleep,
    allow: Any = None,
    on_sent: Any = None,
) -> None:
    """Procesa UN job y publica la respuesta. Nunca levanta: el error viaja en el reply.

    `allow` (opcional) se consulta ANTES de enviar: si devuelve False, no se envía
    (kill-switch o tope diario) y se responde el motivo. `on_sent` (opcional) se
    llama tras un envío exitoso para contabilizarlo.
    """
    request_id = str(job.get("request_id", ""))
    try:
        if allow is not None and not await allow():
            reply: dict[str, Any] = {"ok": False, "error": "envíos pausados o tope diario alcanzado"}
        else:
            delay_ms = random.randint(policy.send_min_delay_ms, policy.send_max_delay_ms)
            if delay_ms > 0:
                await sleep(delay_ms / 1000)
            wamid = await backend.send_text(
                connection_id=str(job.get("connection_id", "")),
                to=str(job.get("to", "")),
                text=str(job.get("text", "")),
                image_url=str(job.get("image_url") or "") or None,
            )
            if on_sent is not None:
                await on_sent()
            reply = {"ok": True, "wamid": wamid}
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)[:200]}
    if request_id:
        await handoff.get_redis().rpush(reply_key(request_id), json.dumps(reply))


async def drain_once(
    tenant_id: uuid.UUID,
    backend: SendBackend,
    policy: WaWebPolicy,
    *,
    sleep: Any = asyncio.sleep,
    timeout: int = 2,
    allow: Any = None,
    on_sent: Any = None,
) -> bool:
    """Procesa a lo sumo un job pendiente. `True` si procesó algo."""
    popped = await handoff.get_redis().blpop(outbox_key(str(tenant_id)), timeout=timeout)
    if popped is None:
        return False
    _key, raw = popped
    try:
        job = json.loads(raw)
    except (TypeError, ValueError):
        return True
    if isinstance(job, dict):
        await process_job(job, backend, policy, sleep=sleep, allow=allow, on_sent=on_sent)
    return True


async def purge_outbox(tenant_id: uuid.UUID, reason: str) -> int:
    """Vacía la cola de salida y responde error a cada worker que espera.

    Se usa al cerrar la sesión (p. ej. `logged_out`): evita que un envío
    encolado se despache más tarde contra una sesión muerta. Devuelve cuántos
    jobs descartó.
    """
    r = handoff.get_redis()
    key = outbox_key(str(tenant_id))
    purged = 0
    while True:
        raw = await r.lpop(key)
        if raw is None:
            break
        purged += 1
        try:
            job = json.loads(raw)
        except (TypeError, ValueError):
            continue
        request_id = str(job.get("request_id", "")) if isinstance(job, dict) else ""
        if request_id:
            await r.rpush(reply_key(request_id), json.dumps({"ok": False, "error": reason}))
    return purged
