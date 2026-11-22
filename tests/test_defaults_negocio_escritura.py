"""Escritura de `app_settings.agent_defaults` (ADR-0206 §6, HU-AOS-91 parte C1).

Un guardado parcial hace merge sobre lo guardado: solo cambian las claves que vienen (aunque traigan
`None`), nunca se pisa con los por defecto lo que no vino, y un valor fuera de rango no se guarda.
Sin base de datos: la fila es un `AppSettings` en memoria.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from core.admin.settings_service import read_memory
from core.agents import defaults_negocio
from core.db.models import AppSettings


def _fila(guardados: dict | None = None) -> AppSettings:
    fila = AppSettings(tenant_id=uuid.uuid4())
    fila.agent_defaults = guardados
    return fila


def test_un_cambio_parcial_no_pisa_lo_que_no_vino() -> None:
    fila = _fila({"persona": {"tone": "formal", "warmth": 1, "forbiddenPhrases": ["gratis"]}})

    defaults_negocio.escribir(fila, {"persona": {"tone": "informal"}})

    persona = fila.agent_defaults["persona"]
    assert persona["tone"] == "informal"
    assert persona["warmth"] == 1
    assert persona["forbiddenPhrases"] == ["gratis"]


def test_none_es_un_valor_y_la_seccion_ausente_no_cambia() -> None:
    fila = _fila({"memory": {"shortTermTurns": 12, "rag": {"topK": 4, "minScore": 0.7}}, "model": {"primary": "m1"}})

    defaults_negocio.escribir(fila, {"memory": {"rag": None}})

    assert fila.agent_defaults["memory"] == {"shortTermTurns": 12, "rag": None}
    assert fila.agent_defaults["model"]["primary"] == "m1"
    assert read_memory(defaults_negocio.desde_dict(fila.agent_defaults)) == {
        "short_term_turns": 12,
        "rag_top_k": 0,
        "rag_min_score": 0.6,
    }


def test_sin_valores_guardados_parte_de_los_por_defecto() -> None:
    fila = _fila(None)

    defaults_negocio.escribir(fila, {"policies": {"maxToolCallsPerTurn": 7}})

    assert fila.agent_defaults["policies"] == {"maxToolCallsPerTurn": 7, "budgetUsdPerConversation": 0.5}
    assert fila.agent_defaults["persona"] == defaults_negocio.a_guardar(defaults_negocio.POR_DEFECTO)["persona"]


def test_un_valor_invalido_no_se_guarda() -> None:
    guardados = {"persona": {"tone": "formal"}}
    fila = _fila(dict(guardados))

    with pytest.raises(ValidationError):
        defaults_negocio.escribir(fila, {"persona": {"tone": "gritón"}})

    assert fila.agent_defaults == guardados


def test_escribir_reasigna_la_columna() -> None:
    """SQLAlchemy no ve una mutación en sitio del JSONB: la columna se reasigna con un dict nuevo."""
    original = {"persona": {"tone": "formal"}}
    fila = _fila(original)

    defaults_negocio.escribir(fila, {"persona": {"warmth": 2}})

    assert fila.agent_defaults is not original
    assert original == {"persona": {"tone": "formal"}}


def test_un_guardado_invalido_no_se_cambia_por_los_por_defecto() -> None:
    """El backfill copia el v2 sin topes: cambiar el tono no puede llevarse el modelo del negocio."""
    guardados = {"persona": {"maxLength": 20_000}, "model": {"primary": "m1"}}
    fila = _fila(guardados)

    with pytest.raises(ValidationError):
        defaults_negocio.escribir(fila, {"persona": {"tone": "formal"}})
    assert fila.agent_defaults["model"]["primary"] == "m1"

    defaults_negocio.escribir(fila, {"persona": {"maxLength": 900}})
    assert fila.agent_defaults["persona"]["maxLength"] == 900
    assert fila.agent_defaults["model"]["primary"] == "m1"
