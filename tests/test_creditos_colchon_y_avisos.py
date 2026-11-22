"""HU-BILL-20 · el crédito bloquea los turnos, con colchón y avisos (ADR-0208 §2-7). Sin base de datos.

- la decisión del turno: normal, colchón y agotado (con o sin conversación abierta), y la bandera apagada;
- el colchón es el 5 % del crédito mensual y se deriva de lo consumido menos el saldo;
- una conversación nueva sin saldo recibe `quota_exhausted` una sola vez por episodio;
- con el colchón agotado, la conversación abierta pasa a una persona con `creditos_agotados`, sin LLM;
- el desbloqueo es solo: con un ciclo nuevo el turno vuelve a `normal`;
- los avisos al 80 %, al 100 % y al entrar al colchón salen una vez por ciclo y nunca rompen el turno.

El camino completo contra la base (episodio releído, filas reales del ledger) está en
`tests/integration/test_creditos_colchon_turno.py`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.creditos import COLCHON_FRACCION, DecisionDelTurno, SaldoDeCreditos

LIMITE_BASIC = 12_000  # crédito mensual de Basic: colchón de 600
INICIO_CICLO = datetime(2026, 11, 1, tzinfo=UTC)


def _overview(plan_id: str | None = "basic", period_start: datetime | None = None):  # type: ignore[no-untyped-def]
    from core.billing.enforcement import SubscriptionOverview

    inicio = period_start or datetime.now(UTC) - timedelta(days=3)
    return SubscriptionOverview(
        plan_id=plan_id, plan_name=plan_id or "—", status="active", operational=True, trial_ends_at=None,
        period_start=inicio, period_end=inicio + timedelta(days=30), days_left=27, limits={}, dia_ancla=None,
    )


@pytest.fixture()
def saldo(monkeypatch):  # type: ignore[no-untyped-def]
    """Suscripción, consumo y conversación falsos. `consumo` es [(instante, créditos)]: `medir_creditos`
    suma lo que cae en la ventana pedida, así un ciclo nuevo mide 0."""
    from core.billing import creditos, enforcement

    estado: dict[str, Any] = {
        "overview": _overview(),
        "consumo": [],
        "abierta": (False, None),
        "consultas_conversacion": 0,
        "sql": [],
        "abonos": [],  # credit_grants del ciclo (HU-BILL-22)
    }

    async def _sub(_tid):  # type: ignore[no-untyped-def]
        return estado["overview"]

    async def _medir(_session, _tid, *, desde=None, hasta=None, **_k):  # type: ignore[no-untyped-def]
        total = sum((c for t, c in estado["consumo"] if desde <= t < hasta), Decimal(0))
        return creditos.ConsumoDeCreditos(total=total, por_concepto={})

    async def _abierta(_session, _tid, _cid):  # type: ignore[no-untyped-def]
        estado["consultas_conversacion"] += 1
        return estado["abierta"]

    class _Sesion:
        async def execute(self, stmt, *_a, **_k):  # type: ignore[no-untyped-def]
            estado["sql"].append(str(stmt))

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield _Sesion()

    monkeypatch.setattr(enforcement, "subscription_overview", _sub)
    monkeypatch.setattr(creditos, "medir_creditos", _medir)
    monkeypatch.setattr(creditos, "conversacion_abierta", _abierta)

    async def _abonos(_session, _tid, _ciclo):  # type: ignore[no-untyped-def]
        return list(estado["abonos"])

    monkeypatch.setattr(creditos, "leer_abonos", _abonos)
    monkeypatch.setattr("core.db.session.get_session", _get_session)
    return estado


def _consumido(estado: dict[str, Any], creditos: float | int) -> None:
    estado["consumo"] = [(datetime.now(UTC) - timedelta(minutes=5), Decimal(str(creditos)))]


async def _decidir(**kw: Any) -> DecisionDelTurno:
    from core.billing.enforcement import turn_quota_metric

    return await turn_quota_metric(uuid.uuid4(), uuid.uuid4(), **kw)


# ------------------------------------------------------------------ la decisión del turno
def test_el_colchon_es_el_5_por_ciento_del_credito_mensual() -> None:
    from core.billing.creditos import tamano_del_colchon

    assert COLCHON_FRACCION == Decimal("0.05")
    assert tamano_del_colchon(LIMITE_BASIC) == Decimal(600)
    assert tamano_del_colchon(None) == 0 and tamano_del_colchon(0) == 0


@pytest.mark.parametrize(
    ("usado", "abierta", "salida", "es_abierta"),
    [
        (11_999.9, False, "normal", False),  # con saldo
        (11_999.9, True, "normal", False),
        (12_000, False, "agotado", False),  # conversación nueva sin saldo
        (12_000, True, "colchon", True),  # abierta: entra al colchón
        (12_599.9, True, "colchon", True),
        (12_600, True, "agotado", True),  # colchón agotado
        (20_000, True, "agotado", True),
    ],
)
async def test_normal_colchon_y_agotado(saldo, usado, abierta, salida, es_abierta) -> None:
    _consumido(saldo, usado)
    saldo["abierta"] = (abierta, datetime.now(UTC) - timedelta(minutes=10) if abierta else None)
    decision = await _decidir()
    assert (decision.salida, decision.abierta) == (salida, es_abierta)
    assert decision.colchon_limite == Decimal(600)
    assert decision.colchon_usado == max(Decimal(0), Decimal(str(usado)) - LIMITE_BASIC)
    # Lectura del turno: sin lock consultivo (ADR-0208 §2).
    assert not any("advisory" in sql for sql in saldo["sql"])


async def test_con_saldo_no_consulta_la_conversacion(saldo) -> None:
    _consumido(saldo, 100)
    assert (await _decidir()).salida == "normal"
    assert saldo["consultas_conversacion"] == 0


async def test_con_la_bandera_apagada_siempre_es_normal(saldo, monkeypatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_bloquear_por_creditos", False)
    saldo["abierta"] = (True, datetime.now(UTC))
    for usado in (12_000, 12_300, 50_000):
        _consumido(saldo, usado)
        assert (await _decidir()).salida == "normal"
    assert saldo["consultas_conversacion"] == 0


async def test_sin_suscripcion_no_limita_ni_tiene_colchon(saldo) -> None:
    saldo["overview"] = _overview(None)
    _consumido(saldo, 10**9)
    decision = await _decidir()
    assert decision.salida == "normal" and decision.saldo.limite is None and decision.colchon_limite == 0


async def test_desbloqueo_con_un_ciclo_nuevo(saldo) -> None:
    """Nada del corte queda guardado: en el ciclo siguiente lo consumido del anterior no cuenta."""
    from core.billing.creditos import decidir_turno
    from core.shared.tenant import tenant_scope

    tid = uuid.uuid4()
    saldo["overview"] = _overview(period_start=INICIO_CICLO)
    saldo["consumo"] = [(INICIO_CICLO + timedelta(days=10), Decimal(20_000))]
    saldo["abierta"] = (True, INICIO_CICLO + timedelta(days=12))

    class _Sesion:
        async def execute(self, *_a, **_k):  # type: ignore[no-untyped-def]
            return None

    async with tenant_scope(tid):
        agotado = await decidir_turno(_Sesion(), tid, uuid.uuid4(), now=INICIO_CICLO + timedelta(days=12))
        nuevo = await decidir_turno(_Sesion(), tid, uuid.uuid4(), now=INICIO_CICLO + timedelta(days=31))
    assert agotado.salida == "agotado" and agotado.saldo.inicio == INICIO_CICLO
    assert nuevo.salida == "normal" and nuevo.saldo.usado == 0 and nuevo.saldo.inicio > INICIO_CICLO


# ------------------------------------------------------------------ el ingreso del turno
def _decision(salida: str, *, abierta: bool = False, episodio: datetime | None = None, usado: int = 12_000):
    return DecisionDelTurno(
        salida,  # type: ignore[arg-type]
        SaldoDeCreditos(usado=Decimal(usado), limite=LIMITE_BASIC, inicio=INICIO_CICLO),
        abierta=abierta,
        episodio=episodio,
        colchon_limite=Decimal(600),
    )


class _Redis:
    def __init__(self) -> None:
        self.llaves: dict[str, str] = {}

    async def set(self, llave, valor, *, nx=False, ex=None):  # type: ignore[no-untyped-def]
        if nx and llave in self.llaves:
            return None
        self.llaves[llave] = valor
        return True

    async def delete(self, llave):  # type: ignore[no-untyped-def]
        self.llaves.pop(llave, None)


@pytest.fixture()
def turno(monkeypatch):  # type: ignore[no-untyped-def]
    """`politicas_del_turno` con la decisión, los bloqueos, la escalación y los avisos registrados."""
    from core.kernel import ingress
    from core.shared.tenant import tenant_scope

    estado: dict[str, Any] = {"decision": _decision("normal"), "vistos": [], "avisos": [], "redis": _Redis()}

    async def _turn_quota_metric(_tid, _cid):  # type: ignore[no-untyped-def]
        return estado["decision"]

    async def _bloquear(_conv, motivo, *, status=None):  # type: ignore[no-untyped-def]
        estado["vistos"].append(("bloquear", motivo, status))

    async def _escalar(_cid, reason, summary=""):  # type: ignore[no-untyped-def]
        estado["vistos"].append(("escalar", reason, summary))

    def _marcar():  # type: ignore[no-untyped-def]
        estado["vistos"].append(("marcar_escalado",))

    async def _avisar(_tid, decision):  # type: ignore[no-untyped-def]
        estado["avisos"].append(decision.salida)

    async def _respuesta(_spec, clave):  # type: ignore[no-untyped-def]
        return None  # el playbook no carga: va el fallback

    monkeypatch.setattr("core.billing.enforcement.turn_quota_metric", _turn_quota_metric)
    monkeypatch.setattr("core.billing.avisos_de_consumo.avisar_consumo", _avisar)
    monkeypatch.setattr("core.orchestrator.escalation.escalate", _escalar)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: estado["redis"])
    monkeypatch.setattr(ingress, "bloquear", _bloquear)
    monkeypatch.setattr(ingress, "marcar_escalado", _marcar)
    monkeypatch.setattr(ingress, "load_response", _respuesta)

    tid = uuid.uuid4()
    estado["conv"] = SimpleNamespace(id=uuid.uuid4(), tenant_id=tid, channel="whatsapp", state={})

    async def _correr():  # type: ignore[no-untyped-def]
        async with tenant_scope(tid):
            return await ingress.politicas_del_turno(estado["conv"], spec=object())

    estado["correr"] = _correr
    return estado


async def test_normal_y_colchon_siguen_al_llm_y_avisan(turno) -> None:
    for salida in ("normal", "colchon"):
        turno["decision"] = _decision(salida, abierta=salida == "colchon")
        assert await turno["correr"]() is None
    assert turno["vistos"] == []
    assert turno["avisos"] == ["normal", "colchon"]


async def test_conversacion_nueva_sin_saldo_responde_una_vez_por_episodio(turno) -> None:
    from core.kernel.ingress import QUOTA_EXHAUSTED_REPLY

    episodio = datetime.now(UTC) - timedelta(minutes=3)
    turno["decision"] = _decision("agotado", episodio=episodio)

    primero = await turno["correr"]()
    segundo = await turno["correr"]()
    tercero = await turno["correr"]()
    assert (primero.motivo, primero.texto, primero.escalado) == ("quota", QUOTA_EXHAUSTED_REPLY, False)
    assert segundo.texto is None and tercero.texto is None
    assert turno["vistos"] == [
        ("bloquear", "quota", None),
        ("bloquear", "quota", "silent"),
        ("bloquear", "quota", "silent"),
    ]

    # Un episodio nuevo vuelve a avisar una vez.
    turno["decision"] = _decision("agotado", episodio=episodio + timedelta(hours=2))
    assert (await turno["correr"]()).texto == QUOTA_EXHAUSTED_REPLY


async def test_otra_conversacion_tiene_su_propio_aviso(turno) -> None:
    episodio = datetime.now(UTC)
    turno["decision"] = _decision("agotado", episodio=episodio)
    assert (await turno["correr"]()).texto is not None
    turno["conv"] = SimpleNamespace(id=uuid.uuid4(), tenant_id=turno["conv"].tenant_id, channel="whatsapp", state={})
    assert (await turno["correr"]()).texto is not None


async def test_sin_redis_responde_igual(turno, monkeypatch) -> None:
    class _Caido:
        async def set(self, *_a, **_k):  # type: ignore[no-untyped-def]
            raise ConnectionError("redis caído")

    monkeypatch.setattr("core.memory.working.get_redis", lambda: _Caido())
    turno["decision"] = _decision("agotado", episodio=datetime.now(UTC))
    assert (await turno["correr"]()).texto is not None


async def test_colchon_agotado_escala_con_creditos_agotados_sin_llm(turno) -> None:
    from core.kernel.ingress import ESCALATION_REPLY, RESUMEN_CREDITOS_AGOTADOS

    turno["decision"] = _decision("agotado", abierta=True, episodio=datetime.now(UTC), usado=12_600)
    bloqueo = await turno["correr"]()
    assert (bloqueo.motivo, bloqueo.texto, bloqueo.escalado) == ("quota", ESCALATION_REPLY, True)
    # La decisión antes que los efectos; luego la escalación y el status del turno.
    assert turno["vistos"] == [
        ("bloquear", "quota", "escalated"),
        ("escalar", "creditos_agotados", RESUMEN_CREDITOS_AGOTADOS),
        ("marcar_escalado",),
    ]
    assert turno["redis"].llaves == {}


async def test_en_el_playground_el_colchon_agotado_no_escala(turno) -> None:
    """En el canal `test`, `escalate` no escala: se responde una vez como una conversación nueva."""
    turno["conv"] = SimpleNamespace(id=uuid.uuid4(), tenant_id=turno["conv"].tenant_id, channel="test", state={})
    turno["decision"] = _decision("agotado", abierta=True, episodio=datetime.now(UTC), usado=12_600)
    bloqueo = await turno["correr"]()
    assert not bloqueo.escalado and bloqueo.texto is not None
    assert all(v[0] != "escalar" for v in turno["vistos"])


async def test_un_aviso_que_falla_no_rompe_el_turno(turno, monkeypatch) -> None:
    from core.billing.avisos_de_consumo import avisar_consumo

    async def _explota(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("centro de notificaciones caído")

    monkeypatch.setattr("core.billing.avisos_de_consumo.avisar_consumo", avisar_consumo)
    monkeypatch.setattr("core.db.session.get_session", _explota)
    turno["decision"] = _decision("colchon", abierta=True, usado=12_100)
    assert await turno["correr"]() is None


async def test_el_loop_marca_el_resultado_como_escalado() -> None:
    """`Bloqueo.escalado` llega al `TurnResult`: egress no vuelve a escalar si el envío falla."""
    import inspect

    from core.orchestrator import loop

    fuente = inspect.getsource(loop.process_conversation)
    assert "escalated=bloqueo.escalado" in fuente


# ------------------------------------------------------------------ avisos al dueño
@pytest.mark.parametrize(
    ("usado", "salida", "esperados"),
    [
        (9_599, "normal", []),
        (9_600, "normal", ["80"]),
        (12_000, "agotado", ["80", "100"]),
        (12_100, "colchon", ["80", "100", "colchon"]),
    ],
)
def test_umbrales_cruzados(usado, salida, esperados) -> None:
    from core.billing.avisos_de_consumo import umbrales_cruzados

    assert umbrales_cruzados(_decision(salida, usado=usado)) == esperados


def test_sin_credito_en_el_plan_no_hay_avisos() -> None:
    from core.billing.avisos_de_consumo import umbrales_cruzados

    sin_limite = DecisionDelTurno("normal", SaldoDeCreditos(usado=Decimal(10**6), limite=None))
    assert umbrales_cruzados(sin_limite) == []


@pytest.fixture()
def centro(monkeypatch):  # type: ignore[no-untyped-def]
    """Centro de notificaciones falso: `existentes` son las claves que el negocio ya tiene."""
    estado: dict[str, Any] = {"existentes": set(), "emitidas": [], "lecturas": 0}

    class _Resultado:
        def __init__(self, claves):  # type: ignore[no-untyped-def]
            self._claves = claves

        def scalars(self):  # type: ignore[no-untyped-def]
            return iter(self._claves)

    class _Sesion:
        async def execute(self, stmt, *_a, **_k):  # type: ignore[no-untyped-def]
            estado["lecturas"] += 1
            pedidas = set(stmt.compile().params["dedupe_key_1"])
            return _Resultado(pedidas & estado["existentes"])

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield _Sesion()

    async def _notify(tid, spec):  # type: ignore[no-untyped-def]
        spec.validate()
        estado["emitidas"].append(spec)
        estado["existentes"].add(spec.dedupe_key)
        return uuid.uuid4()

    monkeypatch.setattr("core.db.session.get_session", _get_session)
    monkeypatch.setattr("core.notifications.service.notify_standalone", _notify)
    return estado


async def test_avisos_al_80_al_100_y_al_colchon_sin_duplicados(centro) -> None:
    from core.billing.avisos_de_consumo import avisar_consumo

    tid = uuid.uuid4()
    ts = int(INICIO_CICLO.timestamp())
    await avisar_consumo(tid, _decision("normal", usado=9_700))
    await avisar_consumo(tid, _decision("normal", usado=9_800))
    await avisar_consumo(tid, _decision("agotado", usado=12_000))
    await avisar_consumo(tid, _decision("colchon", abierta=True, usado=12_050))
    await avisar_consumo(tid, _decision("colchon", abierta=True, usado=12_400))

    assert [s.dedupe_key for s in centro["emitidas"]] == [
        f"creditos:80:{ts}",
        f"creditos:100:{ts}",
        f"creditos:colchon:{ts}",
    ]
    assert [s.event_key for s in centro["emitidas"]] == ["core.credit_80", "core.credit_100", "core.credit_buffer"]
    assert all(s.section == "plan" and s.category == "sistema" for s in centro["emitidas"])


async def test_debajo_del_80_no_lee_nada(centro) -> None:
    from core.billing.avisos_de_consumo import avisar_consumo

    await avisar_consumo(uuid.uuid4(), _decision("normal", usado=100))
    assert centro["lecturas"] == 0 and centro["emitidas"] == []


async def test_un_ciclo_nuevo_vuelve_a_avisar(centro) -> None:
    from core.billing.avisos_de_consumo import avisar_consumo

    tid = uuid.uuid4()
    await avisar_consumo(tid, _decision("normal", usado=9_700))
    siguiente = DecisionDelTurno(
        "normal",
        SaldoDeCreditos(usado=Decimal(9_700), limite=LIMITE_BASIC, inicio=INICIO_CICLO + timedelta(days=30)),
    )
    await avisar_consumo(tid, siguiente)
    assert len({s.dedupe_key for s in centro["emitidas"]}) == 2


async def test_los_avisos_tambien_salen_con_la_bandera_apagada(saldo, centro, monkeypatch) -> None:
    """Medir no depende de bloquear: con la bandera apagada el turno es normal pero el aviso sale."""
    from core.billing.avisos_de_consumo import avisar_consumo
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_bloquear_por_creditos", False)
    _consumido(saldo, 13_000)
    decision = await _decidir()
    assert decision.salida == "normal"
    await avisar_consumo(uuid.uuid4(), decision)
    assert [s.event_key for s in centro["emitidas"]] == ["core.credit_80", "core.credit_100"]


# ------------------------------------------------------------------ motivo y texto de configuración
def test_motivo_creditos_agotados() -> None:
    from core.orchestrator.motivos_escalacion import MOTIVOS, codigo, etiqueta

    assert "creditos_agotados" in MOTIVOS
    assert codigo("creditos_agotados") == "creditos_agotados"
    assert etiqueta("creditos_agotados") == "Se agotó el crédito del plan"


async def test_colchon_agotado_usa_el_texto_de_escalacion_del_playbook() -> None:
    """Sin clave propia en `respuestas.md`: el playbook entra completo en el prompt del sistema."""
    from test_core_tools import make_spec

    from core.agents.responses import load_response

    spec = make_spec([{"name": "identidad", "description": "I"}, {"name": "respuestas", "description": "R"}])
    assert await load_response(spec, "creditos_agotados") is None
    texto = await load_response(spec, "escalation")
    assert texto and "persona" in texto
