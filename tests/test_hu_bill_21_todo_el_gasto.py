"""HU-BILL-21 · todo el gasto en créditos con el tarifario `v2` (ADR-0208 §11-16).

Puro, sin base ni LLM: tarifario `v2`, créditos congelados por `kind`, costo con caché y la fila
`tts`. La medición contra la base (sin cobro doble, `v1`/`v2` mezclados, playground, 409 de la
ingesta) está en `tests/integration/test_hu_bill_21_todo_el_gasto.py`.
"""

from __future__ import annotations

import types
import uuid
from contextlib import asynccontextmanager
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.billing import tarifario
from core.billing.tarifario import (
    TARIFARIO_V1,
    TARIFARIO_V2,
    TARIFARIO_VIGENTE,
    creditos_congelados,
    creditos_variables,
)

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _trm(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_trm_cop", 4000.0)


# ------------------------------------------------------------------ tarifario v2


def test_v2_es_el_vigente_con_fecha_fija_y_v1_no_cambia() -> None:
    assert TARIFARIO_VIGENTE is TARIFARIO_V2
    assert TARIFARIO_V2.version == "v2"
    assert TARIFARIO_V2.vigente_desde is not None and TARIFARIO_V2.vigente_desde.tzinfo is UTC
    # v1 sigue como se publicó: desde siempre, mismos fees.
    assert TARIFARIO_V1.vigente_desde is None
    assert dict(TARIFARIO_V1.fees) == {
        "llm_turn": Decimal("0.2"), "stt": Decimal("0.5"), "vision": Decimal("1.0"), "template_sent": Decimal("0.3"),
    }
    # Mismo margen y mismos fees de turno, voz, imagen y plantilla; los gastos nuevos sin fee.
    assert TARIFARIO_V2.margen == TARIFARIO_V1.margen
    for concepto, fee in TARIFARIO_V1.fees.items():
        assert TARIFARIO_V2.fees[concepto] == fee
    assert {c: TARIFARIO_V2.fees[c] for c in ("agent_run", "embedding", "tts")} == dict.fromkeys(
        ("agent_run", "embedding", "tts"), Decimal("0.0")
    )


@pytest.mark.parametrize(
    ("concepto", "costo", "esperado"),
    [
        # ⌈0,0042 × 4000 × 3 / 10⌉ a 0,1 = 5,1, sin fee: el turno que la originó ya pagó el suyo.
        ("agent_run", Decimal("0.0042"), Decimal("5.1")),
        ("embedding", Decimal("0.0001"), Decimal("0.2")),
        ("tts", Decimal("0.009"), Decimal("10.8")),
        # Nota de voz e imagen: costo real + su fee por fila.
        ("stt", Decimal("0.0002"), Decimal("0.3") + Decimal("0.5")),
        ("vision", Decimal("0.0003"), Decimal("0.4") + Decimal("1.0")),
        ("llm_turn", Decimal("0.0069"), Decimal("8.5")),
    ],
)
def test_creditos_congelados_por_kind(concepto: str, costo: Decimal, esperado: Decimal) -> None:
    assert creditos_congelados(costo, concepto) == {  # type: ignore[arg-type]
        "credits": esperado, "key_mode": "platform", "tarifario_version": "v2", "trm_cop": Decimal("4000.00"),
    }


def test_con_llave_propia_solo_el_fee_y_sin_gasto_cero() -> None:
    assert creditos_congelados(0.5, "stt", "byok")["credits"] == Decimal("0.5")
    assert creditos_congelados(0.5, "agent_run", "byok")["credits"] == Decimal("0.0")
    assert creditos_congelados(0.5, "llm_turn", "platform", hubo_gasto=False)["credits"] == Decimal("0.0")
    # Cualquier valor que no sea `byok` cuenta como plataforma (default seguro).
    assert creditos_congelados(0.5, "tts", "raro")["key_mode"] == "platform"  # type: ignore[arg-type]


def test_tts_no_es_turno() -> None:
    from core.billing.tipos_de_uso import NO_ES_ACTIVIDAD, NO_SON_TURNOS

    assert "tts" in NO_SON_TURNOS and "tts" not in NO_ES_ACTIVIDAD


def test_la_migracion_0153_suma_tts_al_check() -> None:
    texto = (RAIZ / "alembic/versions/0153_usage_kind_tts.py").read_text()
    assert 'down_revision = "0152"' in texto and "'tts'" in texto


# ------------------------------------------------------------------ caché (§13)


def _respuesta(**uso: Any) -> Any:
    from core.llm.types import LLMResponse, LLMUsageInfo

    return LLMResponse(content="", stop_reason="end_turn", usage=LLMUsageInfo(**uso), model="m")


def test_openai_compatible_cobra_lo_cacheado_a_su_precio() -> None:
    from core.llm.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(api_key="k", model="deepseek-flash", base_url="https://x")
    sin_cache = p.estimate_cost_usd(10_000, 200)
    con_cache = p.estimate_cost_usd(10_000, 200, cached_tokens=7_500)
    # 2.500 × 0,14 + 7.500 × 0,003 + 200 × 0,28, por millón.
    assert con_cache == pytest.approx((2_500 * 0.14 + 7_500 * 0.003 + 200 * 0.28) / 1e6)
    assert con_cache < sin_cache


def test_anthropic_suma_la_cache_aparte_de_la_entrada() -> None:
    from core.llm.anthropic_provider import AnthropicProvider

    p = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
    costo = p.estimate_cost_usd(100, 50, cached_tokens=8_000, cache_write_tokens=1_000)
    assert costo == pytest.approx((100 * 1.0 + 8_000 * 0.10 + 1_000 * 1.25 + 50 * 5.0) / 1e6)
    # Antes la caché de Anthropic no se cobraba; cobrarla a precio de entrada costaría más.
    assert costo < (100 + 8_000 + 1_000) * 1.0 / 1e6 + 50 * 5.0 / 1e6


def test_un_modelo_sin_precio_de_cache_cobra_lo_cacheado_como_entrada() -> None:
    from core.llm.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(api_key="k", model="deepseek-chat", base_url="https://x")
    assert p.estimate_cost_usd(1_000, 10, cached_tokens=900) == pytest.approx(p.estimate_cost_usd(1_000, 10))


def test_un_turno_con_cached_tokens_cuesta_menos() -> None:
    from core.llm.openai_compatible import OpenAICompatibleProvider
    from core.orchestrator.loop import _TurnUsage

    p = OpenAICompatibleProvider(api_key="k", model="deepseek-flash", base_url="https://x")
    sin, con = _TurnUsage(), _TurnUsage()
    sin.add(p, _respuesta(input_tokens=9_500, output_tokens=150))
    con.add(p, _respuesta(input_tokens=9_500, output_tokens=150, cache_read_tokens=7_000))
    assert con.cost_usd < sin.cost_usd
    assert con.input_tokens == sin.input_tokens == 9_500  # el total del ledger no cambia


def test_el_proveedor_de_la_politica_reenvia_la_cache() -> None:
    from core.kernel.politica_modelo import ProveedorDeLaPolitica
    from core.llm.openai_compatible import OpenAICompatibleProvider

    base = OpenAICompatibleProvider(api_key="k", model="deepseek-flash", base_url="https://x")
    envuelto = ProveedorDeLaPolitica(base, types.SimpleNamespace())  # type: ignore[arg-type]
    assert envuelto.estimate_cost_usd(1_000, 10, cached_tokens=800) == pytest.approx(
        base.estimate_cost_usd(1_000, 10, cached_tokens=800)
    )


def test_un_estimador_de_dos_argumentos_sigue_funcionando() -> None:
    from core.llm.base import costo_de_respuesta

    llamadas: list[tuple[int, int]] = []

    def _estimar(i: int, o: int) -> float:
        llamadas.append((i, o))
        return 0.5

    proveedor = types.SimpleNamespace(estimate_cost_usd=_estimar)
    assert costo_de_respuesta(proveedor, _respuesta(input_tokens=10, output_tokens=5, cache_read_tokens=3).usage) == 0.5
    assert llamadas == [(10, 5)]


def test_replay_costea_la_cache_como_el_proveedor_real(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.llm import cassette, resolve_provider

    monkeypatch.setattr(cassette, "modo_activo", lambda: "replay")
    grabado = resolve_provider(provider_key="anthropic", api_key="", model="claude-haiku-4-5")
    assert grabado.modo == "replay"  # type: ignore[attr-defined]
    assert grabado.cache_pricing_per_mtok == (0.10, 1.25) and grabado.input_includes_cache is False
    deepseek = resolve_provider(provider_key="deepseek", api_key="", model="deepseek-flash")
    assert deepseek.cache_pricing_per_mtok == (0.003, 0.14) and deepseek.input_includes_cache is True


# ------------------------------------------------------------------ escrituras por kind


class _Sesion:
    def __init__(self, filas: list[Any]) -> None:
        self.filas = filas

    def add(self, fila: Any) -> None:
        self.filas.append(fila)


def _sesion_falsa(monkeypatch: pytest.MonkeyPatch, modulo: Any) -> list[Any]:
    filas: list[Any] = []

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield _Sesion(filas)

    monkeypatch.setattr(modulo, "get_session", _get_session)
    return filas


async def test_stt_y_vision_cobran_su_costo_real(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.media import consumo, ingesta

    filas = _sesion_falsa(monkeypatch, consumo)
    tid = uuid.uuid4()
    monkeypatch.setattr(consumo, "get_current_tenant_id", lambda: tid)
    conv = uuid.uuid4()
    for kind, costo in (("stt", 0.0002), ("vision", 0.0003)):
        await ingesta._registrar_consumo(
            kind=kind, conversation_id=conv, model="m", cost_usd=costo, key_mode="platform",
            input_tokens=1, output_tokens=1, latency_ms=5, resultado={"media_key": "k"},
        )
    assert [(f.kind, f.tenant_id, f.credits, f.tarifario_version) for f in filas] == [
        ("stt", tid, Decimal("0.8"), "v2"),
        ("vision", tid, Decimal("1.4"), "v2"),
    ]


async def test_sin_tenant_scope_no_se_escribe_media(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.media import consumo

    filas = _sesion_falsa(monkeypatch, consumo)
    monkeypatch.setattr(consumo, "get_current_tenant_id", lambda: None)
    await consumo.registrar_consumo(kind="tts", concepto="tts", conversation_id=None, model="tts-1",
                                    cost_usd=0.01, key_mode="platform")
    assert filas == []


async def test_embedding_de_ingesta_cobra_por_tarifario(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.db import session as db_session
    from core.llm import embeddings

    filas = _sesion_falsa(monkeypatch, db_session)
    tid = uuid.uuid4()
    embebidos = embeddings.Embebidos(
        vectores=[[0.0]], tokens=5_000, modelo=embeddings.MODELO_OPENAI, dimension=1,
        cost_usd=embeddings.costo_usd(embeddings.MODELO_OPENAI, 5_000),
    )
    await embeddings._registrar_ingesta(tid, embebidos, None)
    (fila,) = filas
    assert (fila.kind, fila.key_mode, fila.tarifario_version) == ("embedding", "platform", "v2")
    assert fila.credits == creditos_variables(Decimal(str(embebidos.cost_usd)), Decimal(4000)) > 0


def test_la_cota_de_la_ingesta_crece_con_el_documento() -> None:
    from core.llm.embeddings import creditos_de_ingesta

    assert creditos_de_ingesta(0) > 0  # nunca 0: sin saldo no se gasta
    assert creditos_de_ingesta(20 * 1024 * 1024) > creditos_de_ingesta(1024)


# ------------------------------------------------------------------ TTS


def test_precio_de_tts_por_modelo() -> None:
    from core.media.catalog import tts_cost_usd

    assert tts_cost_usd("tts-1", 1_000) == pytest.approx(0.015)
    assert tts_cost_usd("tts-1-hd", 1_000) == pytest.approx(0.03)
    assert tts_cost_usd("modelo-nuevo", 1_000) == pytest.approx(0.03)  # desconocido: el más caro
    assert tts_cost_usd("tts-1", 0) == 0.0


async def test_sintetizar_audio_devuelve_su_costo() -> None:
    from core.media.tts import sintetizar_audio

    transporte = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"OggS..."))
    r = await sintetizar_audio("x" * 5_000, model="tts-1", voice="alloy", api_key="k", transport=transporte)
    # El texto se corta a 4000 caracteres: eso es lo que se cobra.
    assert (r.model, r.caracteres) == ("tts-1", 4_000)
    assert r.cost_usd == pytest.approx(0.06)


async def test_la_voz_escribe_su_fila_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.media import consumo
    from core.media.tts import SpeechResult, registrar_consumo_tts

    filas = _sesion_falsa(monkeypatch, consumo)
    tid, conv = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(consumo, "get_current_tenant_id", lambda: tid)
    resultado = SpeechResult(contenido=b"", model="tts-1", caracteres=600, latency_ms=900, cost_usd=0.009)
    await registrar_consumo_tts(conv, resultado, key_mode="platform")
    await registrar_consumo_tts(conv, resultado, key_mode="byok")
    assert [(f.kind, f.conversation_id, f.key_mode, f.credits) for f in filas] == [
        ("tts", conv, "platform", Decimal("10.8")),
        ("tts", conv, "byok", Decimal("0.0")),  # llave propia: solo el fee, que en tts es 0
    ]
    assert filas[0].result == {"caracteres": 600}


async def test_pack_voice_registra_el_gasto_con_su_llave(monkeypatch: pytest.MonkeyPatch) -> None:
    from pack_voice import tts
    from pack_voice.config import VoiceConfig
    from pydantic import SecretStr

    from core.media.tts import SpeechResult

    vistos: list[str] = []

    async def _registrar(_conv: Any, _r: Any, *, key_mode: str) -> None:
        vistos.append(key_mode)

    async def _audio(*_a: Any, **_k: Any) -> SpeechResult:
        return SpeechResult(contenido=b"no-ogg", model="tts-1", caracteres=10, cost_usd=0.0001)

    monkeypatch.setattr(tts, "sintetizar_audio", _audio)
    monkeypatch.setattr(tts, "registrar_consumo_tts", _registrar)
    monkeypatch.setattr(tts, "_a_ogg_opus", lambda _d: None)  # falla después de pagar
    for llave in ("", "sk-propia"):
        config = VoiceConfig(api_key=SecretStr(llave), modo="siempre")

        async def _cargar(c: VoiceConfig = config) -> VoiceConfig:
            return c

        monkeypatch.setattr(tts, "cargar_config", _cargar)
        monkeypatch.setattr(tts, "get_settings", lambda: types.SimpleNamespace(openai_api_key="sk-plataforma"))
        assert await tts.sintetizar(uuid.uuid4(), "hola") is None
    assert vistos == ["platform", "byok"]


def test_el_tarifario_vigente_no_decide_las_filas_viejas(monkeypatch: pytest.MonkeyPatch) -> None:
    """`medir_creditos` interpreta las filas NULL con `v1` aunque el vigente cambie (§14)."""
    import inspect

    from core.billing import creditos

    fuente = inspect.getsource(creditos.medir_creditos)
    assert "_creditos_de_la_fila(trm, TARIFARIO_V1)" in fuente
    assert "TARIFARIO_VIGENTE" not in fuente
    assert tarifario.TARIFARIO_VIGENTE is TARIFARIO_V2
