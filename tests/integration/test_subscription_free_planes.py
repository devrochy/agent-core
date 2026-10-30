"""HU-SUB-06 · Free con prueba y planes pagos sin prueba (D-16), contra la BD real.

Gherkin: Free sin cobro y fin de la prueba (oferta y vencida). Con SPEC-43 el plan es del catálogo de
`pack_subscriptions`: «plan pago sin prueba» lo valida el catálogo (`tests/test_planes_d16.py`), el
cambio desde Free está en `test_subscription_inscripcion.py` y el pedido contra entrega que no nace
pagado, en `tests/test_planes_d16.py`.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _alta_helpers import plan_del_catalogo
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AHORA = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _negocio(packs: list[str] | None = None) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("free")
    tid, _ = await onboard_tenant(
        name=f"Negocio {slug}", slug=slug, packs=packs or ["pack_subscriptions"]
    )
    return tid


async def _eventos(tid, topic: str) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(
                        select(DomainEvent).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic)
                    )
                ).scalars().all()
            )


async def _sub_free(tid, *, trial_termina: datetime) -> uuid.UUID:
    from pack_subscriptions.inscripcion import _siguiente_numero
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    plan_id = await plan_del_catalogo(tid, plan="free", precio=0)
    async with tenant_scope(tid):
        async with get_session() as s:
            sub = Subscription(
                tenant_id=tid, customer_id=uuid.uuid4(), plan_id=plan_id,
                numero_publico=await _siguiente_numero(s, "SUB"), concepto="Free", estado="trialing",
                valor_periodo=Decimal("0"), moneda="COP", periodicidad="mensual", dia_ancla=21,
                trial_termina=trial_termina, proximo_cargo=trial_termina,
                fecha_alta=trial_termina - timedelta(days=30),
            )
            s.add(sub)
            await s.flush()
            return sub.id


async def _leer(tid, id_):
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await s.get(Subscription, id_)


async def test_free_nace_en_prueba_y_emite_activated() -> None:
    """Gherkin «Free sin cobro»: 30 días de prueba, sin cargo, `subscription.activated`."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    plan_id = await plan_del_catalogo(tid, plan="free", precio=0)
    antes = datetime.now(UTC)
    async with tenant_scope(tid):
        from pack_subscriptions.planes import obtener

        codigo = (await obtener(plan_id)).codigo
        vista = await SubscriptionCapabilityImpl().inscribir(
            customer_id=uuid.uuid4(), plan_code=codigo, datos_alta={"email": "ana@ejemplo.co"}, origen="panel"
        )
        async with get_session() as s:
            sub = await s.get(Subscription, uuid.UUID(vista["subscription_id"]))
    assert sub.estado == "trialing"
    assert Decimal(sub.valor_periodo) == 0
    assert sub.trial_termina is not None
    assert timedelta(days=29, hours=23) < sub.trial_termina - antes < timedelta(days=30, minutes=5)
    eventos = await _eventos(tid, "subscription.activated")
    assert [e.payload["subscription_id"] for e in eventos] == [str(sub.id)]
    assert eventos[0].payload["estado"] == "trialing"


async def test_oferta_antes_del_fin_de_la_prueba_una_sola_vez() -> None:
    """Gherkin «Fin de la prueba»: oferta 3 días antes, sin repetir."""
    from pack_subscriptions.jobs import _avisos_de_cobro

    tid = await _negocio()
    cerca = await _sub_free(tid, trial_termina=AHORA + timedelta(days=2))
    await _sub_free(tid, trial_termina=AHORA + timedelta(days=10))

    await _avisos_de_cobro(AHORA)
    await _avisos_de_cobro(AHORA + timedelta(days=1))
    eventos = await _eventos(tid, "subscription.trial_ending")
    assert [e.payload["subscription_id"] for e in eventos] == [str(cerca)]
    assert eventos[0].dedupe_key == f"sub_trial_ending:{cerca}"
    assert "{prueba.termina}" in eventos[0].payload["texto"]


async def test_free_vence_al_terminar_la_prueba_sin_cargo() -> None:
    """Gherkin «Fin de la prueba»: al día 30 sin pasar a un plan pago → `vencida`."""
    from pack_subscriptions.jobs import _generar_cargos, _marcar_vencidas
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    terminada = await _sub_free(tid, trial_termina=AHORA - timedelta(hours=1))
    vigente = await _sub_free(tid, trial_termina=AHORA + timedelta(days=5))

    await _generar_cargos(AHORA)
    await _marcar_vencidas(AHORA)
    sub = await _leer(tid, terminada)
    assert sub.estado == "vencida"
    assert sub.proximo_cargo is None
    assert (await _leer(tid, vigente)).estado == "trialing"
    async with tenant_scope(tid):
        async with get_session() as s:
            cargos = (await s.execute(select(SubscriptionCharge))).scalars().all()
    assert cargos == []
    eventos = await _eventos(tid, "subscription.past_due")
    assert [e.dedupe_key for e in eventos] == [f"sub_trial_expired:{terminada}"]
    assert eventos[0].payload["motivo"] == "fin_de_prueba"
