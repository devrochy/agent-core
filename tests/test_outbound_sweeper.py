"""Barrido de salientes atascadas (H3): reencola la cabeza `queued` huérfana de una conversación,
respeta las que tienen tarea viva, marca `failed` (y encadena) la que no avanza, y sin Redis no hace
nada."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

import core.tasks.outbound_sweeper as mod

AHORA = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(self, vivas: set[str] | None = None, caido: bool = False):
        self.vivas = vivas or set()
        self.caido = caido
        self.contadores: dict[str, int] = {}

    async def ping(self):
        if self.caido:
            raise ConnectionError("redis caído")
        return True

    async def exists(self, *claves):
        return sum(1 for c in claves if c in self.vivas)

    async def incr(self, clave):
        self.contadores[clave] = self.contadores.get(clave, 0) + 1
        return self.contadores[clave]

    async def decr(self, clave):
        self.contadores[clave] -= 1
        return self.contadores[clave]

    async def expire(self, clave, ttl):
        return True


class FakeTenantsSession:
    def __init__(self, tenant_ids):
        self.tenant_ids = tenant_ids

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        ids = self.tenant_ids

        class R:
            def scalars(self):
                return self

            def all(self):
                return ids

        return R()


@pytest.fixture
def entorno(monkeypatch):
    tenant_id, conv_id, msg_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    creado = AHORA - timedelta(minutes=30)
    estado = {"cabezas": [(msg_id, conv_id, creado)], "delay": [], "fallidos": [], "next": []}
    redis = FakeRedis()

    async def cabezas(tid, limite):
        assert tid == tenant_id
        assert limite == AHORA - timedelta(minutes=10)
        return estado["cabezas"]

    class FakeTask:
        @staticmethod
        def delay(*args):
            estado["delay"].append(args)

    async def mark_failed(mid, tid, exc, *, dlq):
        estado["fallidos"].append((mid, tid, exc, dlq))
        return conv_id, creado, mid

    async def enqueue_next(*a):
        estado["next"].append(a)

    import core.memory.working as working

    monkeypatch.setattr(working, "get_redis", lambda: redis)
    monkeypatch.setattr(mod, "get_session", lambda: FakeTenantsSession([tenant_id]))
    monkeypatch.setattr(mod, "_cabezas_atascadas", cabezas)
    monkeypatch.setattr(mod, "outbound_send_task", FakeTask)
    monkeypatch.setattr(mod, "_mark_failed", mark_failed)
    monkeypatch.setattr(mod, "_enqueue_next", enqueue_next)
    return tenant_id, conv_id, msg_id, redis, estado


@pytest.mark.asyncio
async def test_queued_huerfana_se_reencola(entorno):
    tenant_id, _, msg_id, _, estado = entorno
    conteo = await mod.barrer_salientes_atascadas(AHORA)
    assert estado["delay"] == [(str(msg_id), str(tenant_id))]
    assert estado["fallidos"] == []
    assert conteo["reencoladas"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("marca", ["claim", "programado"])
async def test_con_tarea_viva_no_se_reencola(entorno, marca):
    from core.tasks.outbound_send import claim_key, programado_key

    tenant_id, conv_id, msg_id, redis, estado = entorno
    redis.vivas.add(
        claim_key(tenant_id, conv_id, msg_id) if marca == "claim" else programado_key(tenant_id, msg_id)
    )
    conteo = await mod.barrer_salientes_atascadas(AHORA)
    assert estado["delay"] == []
    assert conteo["con_tarea"] == 1


@pytest.mark.asyncio
async def test_tras_el_tope_de_reencolados_falla_escala_y_encadena(entorno, monkeypatch):
    tenant_id, conv_id, msg_id, _, estado = entorno
    monkeypatch.setattr(mod.get_settings(), "outbound_sweep_max_reencolados", 2)
    for _ in range(2):
        await mod.barrer_salientes_atascadas(AHORA)
    assert len(estado["delay"]) == 2
    conteo = await mod.barrer_salientes_atascadas(AHORA)
    assert len(estado["delay"]) == 2  # la tercera vez ya no reencola
    assert conteo["fallidas"] == 1
    (mid, tid, exc, dlq), = estado["fallidos"]
    assert (mid, tid, dlq) == (msg_id, tenant_id, True)
    assert exc.message == mod.MOTIVO_AGOTADO
    assert estado["next"] == [(conv_id, tenant_id, AHORA - timedelta(minutes=30), msg_id)]


@pytest.mark.asyncio
async def test_sin_redis_no_reencola_nada(entorno):
    _, _, _, redis, estado = entorno
    redis.caido = True
    conteo = await mod.barrer_salientes_atascadas(AHORA)
    assert estado["delay"] == [] and estado["fallidos"] == []
    assert conteo["errores"] == 1


@pytest.mark.asyncio
async def test_un_mensaje_que_revienta_no_frena_a_los_demas(entorno, monkeypatch):
    tenant_id, _, msg_id, redis, estado = entorno
    otro = uuid.uuid4()
    estado["cabezas"] = [(otro, uuid.uuid4(), AHORA - timedelta(hours=1)), *estado["cabezas"]]
    original = redis.exists

    async def exists(*claves):
        if any(str(otro) in c for c in claves):
            raise RuntimeError("fallo puntual")
        return await original(*claves)

    monkeypatch.setattr(redis, "exists", exists)
    conteo = await mod.barrer_salientes_atascadas(AHORA)
    assert estado["delay"] == [(str(msg_id), str(tenant_id))]
    assert conteo["errores"] == 1


def test_el_cron_esta_en_el_beat_y_el_visibility_timeout_supera_el_time_limit():
    from core.tasks.celery_app import celery

    assert celery.conf.beat_schedule["core.outbound_sweeper"]["task"] == "core.outbound_sweeper"
    vt = celery.conf.broker_transport_options["visibility_timeout"]
    assert vt > celery.conf.task_time_limit
    assert vt > 3600  # el countdown más largo de la salida (QUOTA)


@pytest.mark.asyncio
async def test_si_el_broker_falla_el_intento_no_cuenta(entorno, monkeypatch):
    _, _, _, redis, _ = entorno

    class BrokerCaido:
        @staticmethod
        def delay(*a):
            raise ConnectionError("broker caído")

    monkeypatch.setattr(mod, "outbound_send_task", BrokerCaido)
    conteo = await mod.barrer_salientes_atascadas(AHORA)
    assert conteo["errores"] == 1 and conteo["reencoladas"] == 0
    assert set(redis.contadores.values()) == {0}
