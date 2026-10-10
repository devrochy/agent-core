"""Los requisitos de Embedded Signup solo quedan "listos" con una revisión de "App de Meta" vigente
(30 días, decisión del dueño del 2026-10-10). Sin base: la última corrida es falsa."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.admin.platform_meta_es as es
import core.admin.production_readiness_service as readiness
from core.gateway.meta_production_readiness import PLATFORM_CHECK_LABELS

MASTER = SimpleNamespace(id=None, role="master")


def _corrida(hace: timedelta | None, **oks):
    fin = datetime.now(UTC) - hace if hace is not None else None
    items = dict.fromkeys(PLATFORM_CHECK_LABELS, True) | {"rls_tests_pass": None} | oks
    return SimpleNamespace(
        status="done", requested_at=datetime.now(UTC), finished_at=fin, error=None,
        checks=[{"key": k, "label": PLATFORM_CHECK_LABELS[k], "ok": ok} for k, ok in items.items()],
    )


def _revision(monkeypatch, corrida) -> es.RevisionPlataforma:
    async def _ultima(_admin):
        return corrida

    monkeypatch.setattr(readiness, "latest_platform_run", _ultima)
    return asyncio.run(es._revision_de_plataforma(MASTER))


def test_la_vigencia_es_de_30_dias():
    assert es.VIGENCIA_REVISION_PLATAFORMA == timedelta(days=30)


def test_una_revision_de_hace_29_dias_sigue_valiendo(monkeypatch):
    revision = _revision(monkeypatch, _corrida(timedelta(days=29)))

    assert revision.ok is True
    assert revision.expired is False
    assert revision.expires_at is not None and revision.expires_at > datetime.now(UTC)


def test_una_revision_de_hace_31_dias_vence_aunque_este_en_verde(monkeypatch):
    revision = _revision(monkeypatch, _corrida(timedelta(days=31)))

    assert revision.ok is False
    assert revision.expired is True
    assert revision.pending == []  # nada en rojo: lo único que falta es revisar de nuevo
    assert revision.expires_at is not None and revision.expires_at < datetime.now(UTC)


def test_sin_fecha_de_fin_cuenta_como_vencida(monkeypatch):
    revision = _revision(monkeypatch, _corrida(None))

    assert revision.ok is False and revision.expired is True


def test_vigente_con_un_rojo_no_esta_lista(monkeypatch):
    revision = _revision(monkeypatch, _corrida(timedelta(days=1), worker_heartbeat_and_dlq=False))

    assert revision.ok is False and revision.expired is False
    assert revision.pending == [PLATFORM_CHECK_LABELS["worker_heartbeat_and_dlq"]]


@pytest.mark.parametrize("dias, lista", [(29, True), (31, False)])
def test_estado_app_meta_lista_segun_la_vigencia(monkeypatch, dias, lista):
    """`ready` con todos los pasos manuales confirmados: depende solo de la vigencia."""

    async def _ultima(_admin):
        return _corrida(timedelta(days=dias))

    class _Sesion:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            filas = [SimpleNamespace(key=m.key, done=True, notes=None, updated_at=None) for m in es.MANUALES]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: filas))

    monkeypatch.setattr(readiness, "latest_platform_run", _ultima)
    monkeypatch.setattr(es, "get_session", _Sesion)

    estado = asyncio.run(es.estado_app_meta(MASTER))

    assert estado.ready is lista
    assert estado.platform_check.expired is (not lista)
