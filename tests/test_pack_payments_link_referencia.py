"""`generar_link_pago` no reusa a ciegas la transacción de una referencia.

Visto en test (2026-10-10, tenant `solvershub`): el modelo llamó
`generar_link_pago({"amount": 600000, "reference": "OC-0001"})` sobre un pedido
viejo ya pagado por $200.000. La idempotencia por referencia devolvía la fila
existente tal cual: `status=aprobado`, `amount=200000` y el link vencido en
`_safe_append`, y el asistente le dijo a la clienta que ya tenía su link.

Ahora, con una transacción existente para la referencia:
- pagada -> error `ya_pagado`, sin link;
- por otro monto -> error `monto_no_coincide` con el monto real, sin link;
- pendiente con el link vencido -> link nuevo; la vieja queda `deleted_at`.

La sesión es falsa (respuestas en el orden en que la tool consulta): lo que se
prueba es la decisión, no SQLAlchemy. El índice parcial y el borrado lógico
contra Postgres están en `tests/integration/test_pack_payments_link_idempotente.py`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pack_payments.skills as skills_mod
import pytest
from pack_payments.config import PaymentsConfig
from pack_payments.providers.base import PaymentLinkResult
from pack_payments.skills import PaymentSkill, link_vencido

TENANT = uuid.UUID("10000000-0000-0000-0000-0000000000a2")
LINK_VIEJO = "https://checkout.wompi.co/l/test_viejo"


def _transaccion(**campos):
    base = {
        "id": uuid.uuid4(),
        "reference": "OC-0001",
        "provider_reference": "test_viejo",
        "status": "pendiente",
        "amount": Decimal("200000.00"),
        "currency": "COP",
        "payment_link_url": LINK_VIEJO,
        "deleted_at": None,
        "created_at": datetime.now(UTC) - timedelta(hours=1),
    }
    base.update(campos)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, valor):
        self._valor = valor
        self.rowcount = valor if isinstance(valor, int) else 0

    def scalar_one_or_none(self):
        return self._valor

    def scalars(self):
        return SimpleNamespace(all=lambda: self._valor)


class _Sesion:
    def __init__(self, entorno):
        self.entorno = entorno

    async def execute(self, stmt):
        self.entorno.sentencias.append(stmt)
        return _Result(self.entorno.respuestas.pop(0))

    def add(self, obj):
        self.entorno.agregadas.append(obj)

    async def flush(self):
        return None

    async def refresh(self, _obj):
        return None


@pytest.fixture
def entorno(monkeypatch):
    """Wompi falso y sesión con respuestas guionadas: 1) por `idempotency_key`,
    2) por `reference`, 3) el `update` que borra la vieja (si regenera)."""
    estado = SimpleNamespace(respuestas=[], sentencias=[], agregadas=[], links_creados=[])

    @asynccontextmanager
    async def _get_session():
        yield _Sesion(estado)

    async def _tenant_config():
        return PaymentsConfig(
            public_key="pub_test_x", private_key="prv_test_x", events_secret="evt_x", link_expira_horas=24
        )

    class _Proveedor:
        provider_name = "wompi"

        async def create_payment_link(self, *, reference, amount, currency, expires_in_hours):
            estado.links_creados.append({"reference": reference, "amount": amount})
            return PaymentLinkResult(
                provider_reference="test_nuevo", payment_link_url="https://checkout.wompi.co/l/test_nuevo"
            )

    monkeypatch.setattr(skills_mod, "get_session", _get_session)
    monkeypatch.setattr(skills_mod, "tenant_config", _tenant_config)
    monkeypatch.setattr(skills_mod, "build_provider", lambda _config, http=None: _Proveedor())
    monkeypatch.setattr(skills_mod, "get_current_tenant_id", lambda: TENANT)
    return estado


async def _llamar(amount=200_000, reference="OC-0001"):
    contexto = SimpleNamespace(idempotency_key=f"tc:{uuid.uuid4().hex[:8]}", http=None)
    return await PaymentSkill().generar_link_pago({"reference": reference, "amount": amount}, contexto)


@pytest.mark.asyncio
async def test_referencia_pagada_devuelve_error_sin_link(entorno):
    """El caso de test: OC-0001 pagado por $200.000 y el modelo pidió $600.000."""
    entorno.respuestas = [None, [_transaccion(status="aprobado", created_at=datetime(2026, 10, 5, tzinfo=UTC))]]

    resultado = await _llamar(amount=600_000)

    assert resultado["status"] == "ya_pagado"
    assert "error" in resultado
    assert "_safe_append" not in resultado
    assert entorno.links_creados == []


@pytest.mark.asyncio
async def test_referencia_pagada_en_una_transaccion_borrada_tambien_cuenta(entorno):
    """Un webhook tardío puede aprobar el link viejo (ya reemplazado): la referencia
    está pagada aunque la transacción viva siga pendiente."""
    entorno.respuestas = [
        None,
        [
            _transaccion(status="aprobado", deleted_at=datetime.now(UTC)),
            _transaccion(provider_reference="test_nuevo"),
        ],
    ]

    resultado = await _llamar()

    assert resultado["status"] == "ya_pagado"
    assert "_safe_append" not in resultado


@pytest.mark.asyncio
async def test_monto_distinto_al_de_la_referencia_devuelve_el_monto_real(entorno):
    entorno.respuestas = [None, [_transaccion()]]

    resultado = await _llamar(amount=600_000)

    assert resultado["status"] == "monto_no_coincide"
    assert "200,000" in resultado["error"]
    assert "_safe_append" not in resultado
    assert entorno.links_creados == []


@pytest.mark.asyncio
async def test_pago_en_revision_no_genera_otro_link(entorno):
    entorno.respuestas = [None, [_transaccion(status="monto_distinto")]]

    resultado = await _llamar()

    assert resultado["status"] == "en_revision"
    assert "_safe_append" not in resultado
    assert entorno.links_creados == []


@pytest.mark.asyncio
async def test_pago_en_revision_en_un_link_reemplazado_tambien_frena(entorno):
    entorno.respuestas = [
        None,
        [
            _transaccion(status="monto_distinto", deleted_at=datetime.now(UTC)),
            _transaccion(provider_reference="test_nuevo"),
        ],
    ]

    resultado = await _llamar()

    assert resultado["status"] == "en_revision"
    assert "_safe_append" not in resultado


@pytest.mark.asyncio
async def test_pendiente_vigente_reusa_el_link(entorno):
    entorno.respuestas = [None, [_transaccion()]]

    resultado = await _llamar()

    assert "error" not in resultado
    assert LINK_VIEJO in resultado["_safe_append"]
    assert entorno.links_creados == []


@pytest.mark.asyncio
async def test_pendiente_con_link_vencido_genera_uno_nuevo_y_borra_el_viejo(entorno):
    vieja = _transaccion(created_at=datetime.now(UTC) - timedelta(hours=30))
    entorno.respuestas = [None, [vieja], 1]

    resultado = await _llamar()

    assert "error" not in resultado
    assert resultado["_safe_append"].endswith("https://checkout.wompi.co/l/test_nuevo")
    assert entorno.links_creados == [{"reference": "OC-0001", "amount": 200_000.0}]
    # La vieja se borra (con su estado a `expirado`) en la misma sesión del insert.
    borrado = entorno.sentencias[-1]
    assert borrado.is_dml and borrado.table.name == "pack_payments_transactions"
    valores = {col.key: val.value for col, val in borrado._values.items()}
    assert valores["status"] == "expirado"
    assert valores["deleted_at"] is not None
    (nueva,) = entorno.agregadas
    assert nueva.reference == "OC-0001"
    assert nueva.provider_reference == "test_nuevo"
    assert nueva.status == "pendiente"


@pytest.mark.asyncio
async def test_si_otra_llamada_ya_regenero_devuelve_esa(entorno):
    """Dos llamadas regeneran a la vez: la que no logra borrar la vieja devuelve la
    viva en vez de insertar una segunda (chocaría con el índice único parcial)."""
    vieja = _transaccion(created_at=datetime.now(UTC) - timedelta(hours=30))
    vieja_borrada = _transaccion(id=vieja.id, status="expirado", deleted_at=datetime.now(UTC))
    ganadora = _transaccion(
        provider_reference="test_otro", payment_link_url="https://checkout.wompi.co/l/test_otro"
    )
    entorno.respuestas = [None, [vieja], 0, [vieja_borrada, ganadora]]

    resultado = await _llamar()

    assert resultado["_safe_append"].endswith("https://checkout.wompi.co/l/test_otro")
    assert entorno.agregadas == []


@pytest.mark.asyncio
async def test_si_un_webhook_la_aprobo_mientras_tanto_no_da_link(entorno):
    """El pago del link viejo se aprobó entre la lectura y el borrado: el `update` no
    la toca (cambió el estado) y la referencia ya está pagada."""
    vieja = _transaccion(created_at=datetime.now(UTC) - timedelta(hours=30))
    aprobada = _transaccion(id=vieja.id, status="aprobado", created_at=vieja.created_at)
    entorno.respuestas = [None, [vieja], 0, [aprobada]]

    resultado = await _llamar()

    assert resultado["status"] == "ya_pagado"
    assert "_safe_append" not in resultado
    assert entorno.agregadas == []


def test_link_vencido_usa_la_vigencia_y_un_margen():
    ahora = datetime(2026, 10, 10, 12, tzinfo=UTC)
    creado = ahora - timedelta(hours=23)
    assert not link_vencido(_transaccion(created_at=creado), 24, ahora=ahora)
    # A 10 minutos de vencer ya no sirve para mandárselo al cliente.
    assert link_vencido(_transaccion(created_at=creado - timedelta(minutes=50)), 24, ahora=ahora)
    assert link_vencido(_transaccion(created_at=ahora, status="expirado"), 24, ahora=ahora)
    assert link_vencido(_transaccion(created_at=ahora, payment_link_url=None), 24, ahora=ahora)
