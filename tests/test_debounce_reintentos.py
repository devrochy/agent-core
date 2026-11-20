"""H2 (mensajes perdidos con Twilio, 2026-11-20): un turno diferido que falla ya no se pierde.

Antes `_fire_conversation` tragaba la excepción y el `gather` descartaba un `TurnTimeout`; el
payload ya se había borrado en el claim, así que el mensaje desaparecía sin rastro. Ahora se
reencola con un contador y, agotado `MAX_DELIVERIES`, va a la DLQ del stream de entrada con la
forma que reprocesa `POST /admin/dlq/{id}/retry`.

Sin BD ni Redis: un Redis en memoria con los comandos que usa el debounce, y el turno y la
conversación parcheados. Lo que toca la BD de verdad (campos de la DLQ desde la conversación)
está en `tests/integration/test_debounce_reintentos_dlq.py`.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import debounce
from core.orchestrator.consumer import DEAD_LETTER_STREAM, MAX_DELIVERIES
from core.orchestrator.turn_gate import TurnQueueTimeout, TurnTimeout

TENANT = str(uuid.uuid5(uuid.NAMESPACE_DNS, "tenant-h2"))


class _Pipe:
    def __init__(self, redis: RedisEnMemoria) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, nombre: str) -> Any:
        def _encolar(*args: Any, **kwargs: Any) -> _Pipe:
            self._ops.append((nombre, args, kwargs))
            return self

        return _encolar

    async def execute(self) -> list[Any]:
        return [await getattr(self._redis, n)(*a, **k) for n, a, k in self._ops]


class RedisEnMemoria:
    """Lo justo de Redis para el debounce (strings, listas, sorted set y streams)."""

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}

    def pipeline(self, transaction: bool = True) -> _Pipe:
        return _Pipe(self)

    async def get(self, key: str) -> Any:
        return self.kv.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self.kv)

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.kv.pop(k, None) is not None)

    async def expire(self, key: str, _ttl: int) -> bool:
        return key in self.kv

    async def incr(self, key: str) -> int:
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def decr(self, key: str) -> int:
        self.kv[key] = int(self.kv.get(key, 0)) - 1
        return self.kv[key]

    async def rpush(self, key: str, *values: str) -> int:
        self.kv.setdefault(key, []).extend(values)
        return len(self.kv[key])

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        if key in self.kv:
            self.kv[key] = self.kv[key][start:] if end == -1 else self.kv[key][start : end + 1]
        return True

    async def lrange(self, key: str, _start: int, _end: int) -> list[str]:
        return list(self.kv.get(key, []))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrem(self, key: str, member: str) -> int:
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def zscore(self, key: str, member: str) -> float | None:
        return self.zsets.get(key, {}).get(member)

    async def zrangebyscore(self, key: str, _min: Any, maximo: float) -> list[str]:
        return [m for m, score in self.zsets.get(key, {}).items() if score <= maximo]

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        entradas = self.streams.setdefault(stream, [])
        entry_id = f"{len(entradas) + 1}-0"
        entradas.append((entry_id, dict(fields)))
        return entry_id


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> RedisEnMemoria:
    falso = RedisEnMemoria()
    monkeypatch.setattr(debounce, "get_redis", lambda: falso)
    return falso


@pytest.fixture
def avisos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str]]:
    enviados: list[tuple[str | None, str]] = []

    async def _avisar(tenant_id: str | None, entry_id: str) -> None:
        enviados.append((tenant_id, entry_id))

    monkeypatch.setattr("core.orchestrator.consumer._avisar_mensaje_fallido", _avisar)
    return enviados


@pytest.fixture
def turno(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """El turno diferido parcheado: falla con `estado["error"]` (o termina bien si es None)."""
    estado: dict[str, Any] = {"error": RuntimeError("LLM caído"), "llamadas": [], "gate": None}

    async def _run_turn(_tenant_id: Any, coro: Any) -> Any:
        if estado["gate"] is not None:  # el gate corta el turno antes o durante su ejecución
            coro.close()
            raise estado["gate"]
        return await coro

    async def _activo(_tid: Any) -> bool:
        return True

    async def _turno(conversation_id: uuid.UUID, mensaje: str, ids: list[str]) -> None:
        estado["llamadas"].append((conversation_id, mensaje, list(ids)))
        if estado["error"] is not None:
            raise estado["error"]

    async def _campos(conversation_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, str]:
        campos = debounce._campos_minimos(str(conversation_id), payload)
        campos.update(provider="twilio", channel="whatsapp", customer_external_id="whatsapp:+573001112233")
        return campos

    monkeypatch.setattr(debounce, "run_turn", _run_turn)
    monkeypatch.setattr("core.tenants.is_tenant_active", _activo)
    monkeypatch.setattr(debounce, "_turno_diferido", _turno)
    monkeypatch.setattr(debounce, "_campos_para_dlq", _campos)
    return estado


async def _agendar(conversation_id: uuid.UUID, texto: str) -> None:
    async with debounce.tenant_scope(TENANT):
        await debounce.schedule_debounce(
            conversation_id, 0, {"latest_message": texto, "inbound_message_ids": [str(uuid.uuid4())]}
        )


async def _disparar(redis: RedisEnMemoria) -> None:
    """Un ciclo del poller con todo vencido (el reintento lleva espera: se adelanta el timer)."""
    pendientes = redis.zsets.get(debounce.PENDING_KEY, {})
    for miembro in pendientes:
        pendientes[miembro] = 0
    await debounce.poll_debounce_once()


def _intentos(redis: RedisEnMemoria, conversation_id: uuid.UUID) -> Any:
    return redis.kv.get(debounce.ATTEMPTS_KEY.format(tenant_id=TENANT, conversation_id=conversation_id))


async def test_turno_que_falla_se_reencola_y_al_agotar_va_a_la_dlq(redis, avisos, turno) -> None:
    conversation_id = uuid.uuid4()
    await _agendar(conversation_id, "Domicilio, calle 68 # 29a 47")

    for intento in range(1, MAX_DELIVERIES):
        await _disparar(redis)
        # Reencolado con su texto, no perdido (antes: `debounce_fire_failed` y nada más).
        assert str(conversation_id) in redis.zsets[debounce.PENDING_KEY]
        payload = json.loads(redis.kv[debounce.PAYLOAD_KEY.format(conversation_id=conversation_id)])
        assert payload["tenant_id"] == TENANT
        assert redis.kv[debounce.MESSAGES_KEY.format(conversation_id=conversation_id)] == [
            "Domicilio, calle 68 # 29a 47"
        ]
        assert _intentos(redis, conversation_id) == intento
        assert DEAD_LETTER_STREAM not in redis.streams

    await _disparar(redis)

    assert len(turno["llamadas"]) == MAX_DELIVERIES
    assert str(conversation_id) not in redis.zsets[debounce.PENDING_KEY]
    [(entry_id, campos)] = redis.streams[DEAD_LETTER_STREAM]
    # Forma de un entrante del stream (la que reprocesa el reintento del panel) + lo de `_dead_letter`.
    assert {
        "tenant_id",
        "provider",
        "channel",
        "external_message_id",
        "customer_external_id",
        "customer_name",
        "content",
        "connection_external_id",
        "media",
        "media_context",
        "error",
        "deliveries",
        "failed_at",
    } <= set(campos)
    assert campos["tenant_id"] == TENANT
    assert campos["content"] == "Domicilio, calle 68 # 29a 47"
    assert campos["deliveries"] == str(MAX_DELIVERIES)
    assert campos["error"] == "RuntimeError: LLM caído"
    # Sintético: el original ya está en `messages` y el reintento lo descartaría como duplicado.
    assert campos["external_message_id"].startswith(f"debounce:{conversation_id}:")
    assert avisos == [(TENANT, entry_id)]
    assert _intentos(redis, conversation_id) is None
    assert not redis.kv.get(debounce.IN_FLIGHT_KEY.format(tenant_id=TENANT, conversation_id=conversation_id))


async def test_turn_timeout_se_reencola_y_cuenta_intento(redis, avisos, turno) -> None:
    turno["gate"] = TurnTimeout("turno cortado a los 180s")
    conversation_id = uuid.uuid4()
    await _agendar(conversation_id, "Pago en línea")

    await _disparar(redis)

    assert str(conversation_id) in redis.zsets[debounce.PENDING_KEY]
    assert redis.kv[debounce.MESSAGES_KEY.format(conversation_id=conversation_id)] == ["Pago en línea"]
    assert _intentos(redis, conversation_id) == 1

    for _ in range(MAX_DELIVERIES - 1):
        await _disparar(redis)
    [(_entry_id, campos)] = redis.streams[DEAD_LETTER_STREAM]
    assert campos["error"].startswith("TurnTimeout:")
    assert len(avisos) == 1


async def test_queue_timeout_se_reencola_sin_contar_intento(redis, avisos, turno) -> None:
    turno["gate"] = TurnQueueTimeout("cupo del tenant saturado")
    conversation_id = uuid.uuid4()
    await _agendar(conversation_id, "hola")

    for _ in range(MAX_DELIVERIES + 1):
        await _disparar(redis)

    assert str(conversation_id) in redis.zsets[debounce.PENDING_KEY]
    assert _intentos(redis, conversation_id) is None
    assert DEAD_LETTER_STREAM not in redis.streams
    assert avisos == []


async def test_turno_que_se_recupera_borra_el_contador(redis, avisos, turno) -> None:
    conversation_id = uuid.uuid4()
    await _agendar(conversation_id, "hola")
    await _disparar(redis)
    assert _intentos(redis, conversation_id) == 1

    turno["error"] = None
    await _disparar(redis)

    assert str(conversation_id) not in redis.zsets[debounce.PENDING_KEY]
    assert redis.kv.get(debounce.PAYLOAD_KEY.format(conversation_id=conversation_id)) is None
    assert _intentos(redis, conversation_id) is None
    assert turno["llamadas"][-1][1] == "hola"
    assert DEAD_LETTER_STREAM not in redis.streams


async def test_mensaje_durante_el_reintento_se_agrupa(redis, avisos, turno) -> None:
    """El texto restaurado y el que llega antes del reintento salen en el mismo turno."""
    conversation_id = uuid.uuid4()
    await _agendar(conversation_id, "Domicilio, calle 68 # 29a 47")
    await _disparar(redis)
    await _agendar(conversation_id, "Pago en línea")

    turno["error"] = None
    await _disparar(redis)

    assert turno["llamadas"][-1][1] == "Domicilio, calle 68 # 29a 47\nPago en línea"
    assert len(turno["llamadas"][-1][2]) == 2


async def test_barrido_periodico_salta_turno_en_curso_y_mensaje_ya_recuperado(redis) -> None:
    conversation_id, message_id = str(uuid.uuid4()), str(uuid.uuid4())

    await redis.set(debounce.IN_FLIGHT_KEY.format(tenant_id=TENANT, conversation_id=conversation_id), "1")
    assert not await debounce._reclamar_recuperacion(redis, TENANT, conversation_id, message_id)

    await redis.delete(debounce.IN_FLIGHT_KEY.format(tenant_id=TENANT, conversation_id=conversation_id))
    assert await debounce._reclamar_recuperacion(redis, TENANT, conversation_id, message_id)
    # Una sola vez por mensaje: el siguiente ciclo (u otra réplica) no lo vuelve a disparar.
    assert not await debounce._reclamar_recuperacion(redis, TENANT, conversation_id, message_id)


async def test_el_poller_barre_huerfanos_cada_n_minutos(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    reloj = {"t": 1000.0}
    barridos: list[bool] = []

    async def _poll() -> None:
        reloj["t"] += 60

    async def _recover(*, periodico: bool = False) -> None:
        barridos.append(periodico)

    vueltas = {"n": 0}

    async def _dormir(_s: float) -> None:
        vueltas["n"] += 1
        if vueltas["n"] >= 11:
            raise asyncio.CancelledError

    # Solo los nombres del módulo del debounce: el event loop sigue con su reloj y su sleep.
    monkeypatch.setattr(debounce, "time", SimpleNamespace(monotonic=lambda: reloj["t"]))
    monkeypatch.setattr(debounce, "asyncio", SimpleNamespace(sleep=_dormir))
    monkeypatch.setattr(debounce, "poll_debounce_once", _poll)
    monkeypatch.setattr(debounce, "recover_orphaned_conversations", _recover)

    with pytest.raises(asyncio.CancelledError):
        await debounce.poll_debounce_loop()

    # 11 vueltas de 60 s: barridos a los 300 s y a los 600 s, siempre en modo periódico.
    assert barridos == [True, True]
