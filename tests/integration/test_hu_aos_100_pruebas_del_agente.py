"""HU-AOS-100 · modelo de datos de las pruebas del agente y su cobro, contra la base real (ADR-0212 §1 y §4).

- Aislamiento: un negocio nunca ve ni escribe escenarios, corridas o resultados de otro (RLS forzada, Gherkin 1).
- Cobro: las filas `prueba_agente` suman al cubo del playground, con una sola fórmula
  `max(0, playground vivo + pruebas − 300)` (Gherkin 2); la conversación de una corrida en curso no cuenta dos
  veces y las filas sueltas del juez (sin conversación) tampoco caen en «negocio».
- `prueba_agente` no es turno ni actividad (Gherkin 3).
- Una sola corrida activa por negocio, título único por agente y límites de `k`, desde la base.
- `purge_tenant` cubre las tablas nuevas sin cambios.

Sin LLM ni red: las filas se insertan directo.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.aislamiento,  # pruebas del agente por negocio (ADR-0212)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

V2 = {"key_mode": "platform", "tarifario_version": "v2", "trm_cop": Decimal("4000")}


@pytest.fixture
def corte(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """`vigente_desde` de `v2` hace una hora y TRM 4000."""
    from core.billing import creditos
    from core.config import get_settings

    momento = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(creditos, "TARIFARIO_V2", dataclasses.replace(creditos.TARIFARIO_V2, vigente_desde=momento))
    monkeypatch.setattr(get_settings(), "billing_trm_cop", 4000.0)
    return momento


async def _negocio() -> tuple[uuid.UUID, uuid.UUID]:
    """Negocio con un agente. Devuelve `(tenant_id, agent_id)`."""
    from _habilidades import onboard_tenant

    from core.db.models import AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("aos100")
    tid, _ = await onboard_tenant(name=f"Pruebas {slug}", slug=slug, sembrar_ejemplos=False, plan="basic")
    agente = AgentRecord(tenant_id=tid, key=f"ventas_{uuid.uuid4().hex[:6]}", kind="specialist", name="Ventas")
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(agente)
            await session.flush()
            return tid, agente.id


async def _conversacion(tid: uuid.UUID, *, canal: str = "test", estado: dict[str, Any] | None = None) -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel=canal, external_id=f"a100:{uuid.uuid4().hex[:8]}")
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel=canal, state=estado or {})
            session.add(conv)
            await session.flush()
            return conv.id


def _ledger(tid: uuid.UUID, conv: uuid.UUID | None, cuando: datetime, **campos: Any) -> Any:
    from core.db.models import UsageLedger

    base: dict[str, Any] = {"model": "m", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0, "kind": "turn"}
    base.update(campos)
    return UsageLedger(tenant_id=tid, conversation_id=conv, created_at=cuando, **base)


async def _filas(tid: uuid.UUID, *filas: Any) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add_all(filas)


async def _medir(tid: uuid.UUID, desde: datetime, *, por_agente: bool = False) -> Any:
    from core.billing.creditos import medir_creditos
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await medir_creditos(session, tid, desde=desde, por_agente=por_agente)


async def _escenario(tid: uuid.UUID, agente: uuid.UUID, titulo: str = "Pide un domicilio") -> uuid.UUID:
    from core.db.models import AgentTestCase
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    caso = AgentTestCase(
        tenant_id=tid,
        agent_id=agente,
        titulo=titulo,
        objetivo_cliente="Quiere pedir una pizza a domicilio",
        criterios=["Confirma la dirección"],
        origen="manual",
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(caso)
            await session.flush()
            return caso.id


async def _corrida(tid: uuid.UUID, agente: uuid.UUID, *, estado: str = "en_cola", k: int = 1) -> uuid.UUID:
    from core.db.models import AgentTestRun
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    run = AgentTestRun(tenant_id=tid, agent_id=agente, estado=estado, k=k)
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(run)
            await session.flush()
            return run.id


# --------------------------------------------------------------------------- Gherkin 1: aislamiento


async def test_un_negocio_no_ve_ni_escribe_las_pruebas_de_otro() -> None:
    from core.db.models import AgentTestCase, AgentTestResult, AgentTestRun
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, agente_a = await _negocio()
    b, agente_b = await _negocio()
    caso = await _escenario(a, agente_a)
    corrida = await _corrida(a, agente_a)
    async with tenant_scope(a):
        async with get_session() as session:
            session.add(
                AgentTestResult(tenant_id=a, run_id=corrida, case_id=caso, estado="falla", transcripcion=[{"x": 1}])
            )

    async with tenant_scope(b):
        async with get_session() as session:
            for modelo in (AgentTestCase, AgentTestRun, AgentTestResult):
                filas = (await session.execute(modelo.__table__.select())).all()
                assert filas == [], f"{modelo.__tablename__} se filtró a otro negocio"
            # Por id directo tampoco aparece (la ruta responde 404, no 403).
            assert (
                await session.execute(AgentTestCase.__table__.select().where(AgentTestCase.id == caso))
            ).first() is None

    # Escribir con el `tenant_id` de otro negocio lo frena el WITH CHECK de la policy.
    with pytest.raises(DBAPIError):
        async with tenant_scope(b):
            async with get_session() as session:
                session.add(
                    AgentTestCase(
                        tenant_id=a, agent_id=agente_b, titulo="Cruzado", objetivo_cliente="x", origen="manual"
                    )
                )
                await session.flush()


# --------------------------------------------------------------------------- Gherkin 2: el cobro


async def test_las_pruebas_suman_al_cubo_del_playground_con_una_sola_formula(corte: datetime) -> None:
    """280 de playground vivo + 50 de pruebas = 330 brutos: se cobran 30 (los 300 gratis son compartidos)."""
    tid, _ = await _negocio()
    vivo = await _conversacion(tid)
    ahora = corte + timedelta(minutes=10)
    await _filas(
        tid,
        _ledger(tid, vivo, ahora, credits=Decimal("280.0"), **V2),
        # Turnos de la corrida ya consolidados y juez/arreglo (ambos sin conversación): mismo cubo.
        _ledger(tid, None, ahora, kind="prueba_agente", credits=Decimal("30.0"), **V2),
        _ledger(tid, None, ahora, kind="prueba_agente", credits=Decimal("20.0"), **V2),
    )

    consumo = await _medir(tid, corte - timedelta(hours=1), por_agente=True)

    assert consumo.playground_usado == Decimal("330.0")
    assert consumo.playground_gratis_restante == Decimal("0")
    assert consumo.por_concepto["playground"] == Decimal("30.0")
    assert consumo.por_concepto["llm_turn"] == Decimal("0")  # las pruebas no caen en «negocio»
    assert consumo.total == Decimal("30.0")
    assert sum((a.used for a in consumo.por_agente), Decimal(0)) == consumo.total


async def test_la_conversacion_de_una_corrida_en_curso_no_cuenta_dos_veces(corte: datetime) -> None:
    tid, _ = await _negocio()
    de_corrida = await _conversacion(tid, estado={"panel": True, "prueba_corrida": str(uuid.uuid4())})
    ahora = corte + timedelta(minutes=10)
    await _filas(
        tid,
        # Turnos de la corrida mientras vive su conversación: no suman a «negocio» ni al playground...
        _ledger(tid, de_corrida, ahora, credits=Decimal("400.0"), **V2),
        # ...porque al terminar el escenario se consolidan en esta fila.
        _ledger(tid, None, ahora, kind="prueba_agente", credits=Decimal("400.0"), **V2),
    )

    consumo = await _medir(tid, corte - timedelta(hours=1))

    assert consumo.playground_usado == Decimal("400.0")
    assert consumo.por_concepto["playground"] == Decimal("100.0")
    assert consumo.por_concepto["llm_turn"] == Decimal("0")
    assert consumo.total == Decimal("100.0")


async def test_otro_negocio_no_hereda_las_pruebas_de_este(corte: datetime) -> None:
    gastador, _ = await _negocio()
    vecino, _ = await _negocio()
    ahora = corte + timedelta(minutes=10)
    await _filas(gastador, _ledger(gastador, None, ahora, kind="prueba_agente", credits=Decimal("500.0"), **V2))

    consumo = await _medir(vecino, corte - timedelta(hours=1))

    assert consumo.total == Decimal("0")
    assert consumo.playground_usado == Decimal("0")


# --------------------------------------------------------------------------- Gherkin 3: ni turno ni actividad


async def test_prueba_agente_no_es_turno_ni_actividad() -> None:
    from core.billing.tipos_de_uso import NO_ES_ACTIVIDAD, NO_SON_TURNOS, PRUEBA_AGENTE

    assert PRUEBA_AGENTE == "prueba_agente"
    assert PRUEBA_AGENTE in NO_ES_ACTIVIDAD
    assert PRUEBA_AGENTE in NO_SON_TURNOS


async def test_el_ledger_admite_el_kind_y_sigue_siendo_de_solo_insercion() -> None:
    tid, _ = await _negocio()
    await _filas(tid, _ledger(tid, None, datetime.now(UTC), kind="prueba_agente", credits=Decimal("1.0"), **V2))

    with pytest.raises(DBAPIError):
        await _filas(tid, _ledger(tid, None, datetime.now(UTC), kind="inventado", credits=Decimal("1.0"), **V2))


# --------------------------------------------------------------------------- restricciones de la base


async def test_una_sola_corrida_activa_por_negocio_pero_el_historial_no_se_limita() -> None:
    tid, agente = await _negocio()
    otro, agente_otro = await _negocio()

    await _corrida(tid, agente, estado="terminada")
    await _corrida(tid, agente, estado="cancelada")
    activa = await _corrida(tid, agente, estado="en_cola")
    with pytest.raises(IntegrityError):
        await _corrida(tid, agente, estado="corriendo")

    # Otro negocio sí puede tener la suya, y al terminar la primera ya se puede lanzar otra.
    await _corrida(otro, agente_otro, estado="corriendo")
    from core.db.models import AgentTestRun
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                AgentTestRun.__table__.update().where(AgentTestRun.id == activa).values(estado="terminada")
            )
    await _corrida(tid, agente, estado="en_cola")


async def test_k_y_estados_fuera_de_rango_los_rechaza_la_base() -> None:
    tid, agente = await _negocio()
    for k in (0, 11):
        with pytest.raises(IntegrityError):
            await _corrida(tid, agente, estado="terminada", k=k)
    with pytest.raises(IntegrityError):
        await _corrida(tid, agente, estado="inventado")


async def test_el_titulo_es_unico_por_agente_entre_los_vivos() -> None:
    from core.db.models import AgentTestCase
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, agente = await _negocio()
    caso = await _escenario(tid, agente, "Pide un domicilio")
    with pytest.raises(IntegrityError):
        await _escenario(tid, agente, "PIDE UN DOMICILIO")  # mayúsculas distintas, mismo título

    # Borrado lógico: el título vuelve a estar libre.
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                AgentTestCase.__table__.update().where(AgentTestCase.id == caso).values(deleted_at=datetime.now(UTC))
            )
    await _escenario(tid, agente, "Pide un domicilio")


async def test_un_intento_por_escenario_y_corrida() -> None:
    from core.db.models import AgentTestResult
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, agente = await _negocio()
    caso = await _escenario(tid, agente)
    corrida = await _corrida(tid, agente)
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(AgentTestResult(tenant_id=tid, run_id=corrida, case_id=caso, intento=1, estado="pasa"))
            session.add(AgentTestResult(tenant_id=tid, run_id=corrida, case_id=caso, intento=2, estado="falla"))
    with pytest.raises(IntegrityError):
        async with tenant_scope(tid):
            async with get_session() as session:
                session.add(AgentTestResult(tenant_id=tid, run_id=corrida, case_id=caso, intento=1, estado="pasa"))
                await session.flush()


async def test_app_runtime_no_puede_borrar_fisicamente() -> None:
    """Invariante 8: sin DELETE para el rol de la aplicación; borrar es `deleted_at`."""
    from core.db.models import AgentTestCase
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, agente = await _negocio()
    caso = await _escenario(tid, agente)
    with pytest.raises(DBAPIError):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(AgentTestCase.__table__.delete().where(AgentTestCase.id == caso))


async def test_purge_tenant_descubre_las_tablas_nuevas() -> None:
    """`purge_tenant` (0072) recorre las tablas con `tenant_id`: las tres nuevas deben estar entre ellas."""
    from core.db.session import get_session

    async with get_session() as session:
        tablas = {
            fila[0]
            for fila in (
                await session.execute(
                    text(
                        "select c.relname from pg_class c join pg_attribute a on a.attrelid = c.oid "
                        "where c.relkind = 'r' and a.attname = 'tenant_id' and not a.attisdropped "
                        "and c.relname like 'agent_test_%'"
                    )
                )
            ).all()
        }
    assert tablas == {"agent_test_cases", "agent_test_runs", "agent_test_results"}


# --------------------------------------------------------------------------- claves foráneas del mismo negocio


async def test_un_resultado_no_puede_apuntar_al_caso_o_al_agente_de_otro_negocio() -> None:
    """Las FK son compuestas `(tenant_id, …)`: la RLS oculta lo ajeno, pero solo la FK impide enlazarlo."""
    from core.db.models import AgentTestCase, AgentTestResult
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, agente_a = await _negocio()
    b, agente_b = await _negocio()
    caso_a = await _escenario(a, agente_a)
    corrida_b = await _corrida(b, agente_b)

    with pytest.raises(IntegrityError):
        async with tenant_scope(b):
            async with get_session() as session:
                session.add(AgentTestResult(tenant_id=b, run_id=corrida_b, case_id=caso_a, estado="pasa"))
                await session.flush()

    with pytest.raises(IntegrityError):
        async with tenant_scope(a):
            async with get_session() as session:
                session.add(
                    AgentTestCase(tenant_id=a, agent_id=agente_b, titulo="Ajeno", objetivo_cliente="x", origen="ia")
                )
                await session.flush()

    with pytest.raises(IntegrityError):
        await _corrida(a, agente_b)


async def test_una_corrida_activa_no_se_puede_borrar_logicamente() -> None:
    """Si no, liberaría el cupo de «una corrida activa por negocio» mientras sigue ejecutándose."""
    from core.db.models import AgentTestRun
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, agente = await _negocio()
    corrida = await _corrida(tid, agente, estado="corriendo")
    with pytest.raises(IntegrityError):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    AgentTestRun.__table__.update().where(AgentTestRun.id == corrida).values(deleted_at=datetime.now(UTC))
                )
    # Terminada sí se puede borrar y el cupo ya estaba libre.
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                AgentTestRun.__table__.update().where(AgentTestRun.id == corrida).values(estado="terminada")
            )
            await session.execute(
                AgentTestRun.__table__.update().where(AgentTestRun.id == corrida).values(deleted_at=datetime.now(UTC))
            )


async def test_tools_esperadas_y_prohibidas_no_se_cruzan_y_el_titulo_no_es_blanco() -> None:
    from core.db.models import AgentTestCase
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, agente = await _negocio()
    for campos in ({"titulo": "   "}, {"tools_esperadas": ["pagar"], "tools_prohibidas": ["pagar"]}):
        base: dict[str, Any] = {"titulo": uniq("t"), "objetivo_cliente": "x", "origen": "manual"}
        base.update(campos)
        with pytest.raises(IntegrityError):
            async with tenant_scope(tid):
                async with get_session() as session:
                    session.add(AgentTestCase(tenant_id=tid, agent_id=agente, **base))
                    await session.flush()


# --------------------------------------------------------------------------- el cleanup no se lleva una corrida


async def test_el_cleanup_del_playground_respeta_la_conversacion_de_una_corrida(monkeypatch: pytest.MonkeyPatch) -> None:
    """Purgar la conversación de una corrida antes de consolidar su gasto lo perdería (ADR-0212 §2)."""
    from core.config import get_settings
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks.playground_cleanup import purgar_pruebas_vencidas

    monkeypatch.setattr(get_settings(), "playground_retention_hours", 1)
    tid, _ = await _negocio()
    corrida = await _conversacion(tid, estado={"panel": True, "prueba_corrida": str(uuid.uuid4())})
    del_dueno = await _conversacion(tid, estado={"panel": True})
    viejo = datetime.now(UTC) - timedelta(hours=5)
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                Conversation.__table__.update().where(Conversation.id.in_([corrida, del_dueno])).values(started_at=viejo)
            )

    await purgar_pruebas_vencidas()

    async with tenant_scope(tid):
        async with get_session() as session:
            vivas = set((await session.execute(Conversation.__table__.select().with_only_columns(Conversation.id))).scalars())
    assert corrida in vivas, "el cleanup purgó la conversación de una corrida"
    assert del_dueno not in vivas, "el cleanup debe seguir purgando el playground vencido del dueño"
