"""Tests unitarios de pack-cobranza (sin BD/red).

Cubren el motor determinista de negociación (la parte crítica: "la IA propone,
las reglas deciden"), el contrato de la skill y el registro del LeadProvider.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pack_cobranza.models import ReglaNegociacion
from pack_cobranza.skills import CobranzaSkill, evaluar_negociacion


def _reglas(**kw) -> ReglaNegociacion:
    base = {
        "max_dias_prorroga": 15,
        "min_abono_pct": Decimal(10),
        "max_reprogramaciones": 2,
        "umbral_saldo_autonomo": Decimal(0),
    }
    base.update(kw)
    return ReglaNegociacion(tenant_id=uuid.uuid4(), **base)


def test_saldo_cero_no_acepta() -> None:
    r = evaluar_negociacion(Decimal(0), 0, Decimal(100), "abono_parcial", _reglas())
    assert r["aceptada"] is False
    assert r["motivo"] == "sin_saldo"


def test_abono_bajo_minimo() -> None:
    r = evaluar_negociacion(Decimal(1000), 0, Decimal(50), "abono_parcial", _reglas())
    assert r["aceptada"] is False
    assert r["motivo"] == "abono_bajo_minimo"
    assert r["minimo"] == 100.0


def test_abono_ok() -> None:
    r = evaluar_negociacion(Decimal(1000), 0, Decimal(200), "abono_parcial", _reglas())
    assert r["aceptada"] is True


def test_refinanciacion_mora_alta_deriva() -> None:
    r = evaluar_negociacion(Decimal(1000), 30, Decimal(500), "refinanciacion", _reglas())
    assert r["aceptada"] is False
    assert r.get("derivar") is True


def test_valor_mayor_al_saldo() -> None:
    r = evaluar_negociacion(Decimal(1000), 0, Decimal(5000), "abono_parcial", _reglas())
    assert r["aceptada"] is False
    assert r["motivo"] == "valor_mayor_al_saldo"


def test_umbral_autonomo_deriva() -> None:
    r = evaluar_negociacion(
        Decimal(100000), 0, Decimal(5000), "abono_parcial", _reglas(umbral_saldo_autonomo=Decimal(50000))
    )
    assert r["aceptada"] is False
    assert r.get("derivar") is True


def test_skill_expone_tools_de_cliente() -> None:
    """Solo las tools orientadas al cliente (auditoría M3: las de administración
    viven en el panel, no en el asistente del deudor)."""
    names = {t.name for t in CobranzaSkill().get_tools()}
    assert names == {
        "cob_consultar_saldo",
        "cob_estado_cuenta",
        "cob_evaluar_negociacion",
        "cob_registrar_promesa_pago",
        "cob_registrar_abono",
    }
    # Las operaciones de administración NO deben estar en el asistente.
    assert "cob_registrar_gasto" not in names
    assert "cob_registrar_inyeccion" not in names
    assert "cob_generar_liquidacion" not in names
    assert "cob_registrar_visita" not in names


def test_permisos_gestor() -> None:
    from pack_cobranza.permisos import GESTOR_PERMISOS

    assert len(GESTOR_PERMISOS) == 14
    assert "registrar_pago" in GESTOR_PERMISOS


def test_modelos_alineados_a_develop() -> None:
    from pack_cobranza import models

    nombres = set(models.Base.metadata.tables)
    # Naming de develop (ADR-0060): carteras, no rutas.
    assert "pack_cobranza_carteras" in nombres
    assert "pack_cobranza_cartera_config" in nombres
    assert "pack_cobranza_gastos" in nombres
    assert "pack_cobranza_abonos" in nombres
    assert "pack_cobranza_gestores" in nombres
    assert "pack_cobranza_socios" not in nombres
    assert "pack_cobranza_rutas" not in nombres
    # El propietario es el tenant (ADR-0062): no hay entidad propia.
    assert "pack_cobranza_propietarios" not in nombres
    assert "pack_cobranza_propietario_permisos" not in nombres
    assert "propietario_id" not in {c.name for c in models.Cartera.__table__.columns}
    assert "propietario_id" not in {c.name for c in models.Gestor.__table__.columns}
    # Bloque A: auditoría, cambios de cliente y evidencias.
    assert "pack_cobranza_auditoria" in nombres
    assert "pack_cobranza_cambios_cliente" in nombres
    assert "pack_cobranza_cliente_evidencias" in nombres
    # Bloque B: operación de cartera.
    for tabla in (
        "pack_cobranza_cartera_notas",
        "pack_cobranza_carteras_aperturas",
        "pack_cobranza_posicion_gestor",
        "pack_cobranza_reportes_diarios",
        "pack_cobranza_cartera_estadisticas",
        "pack_cobranza_cartera_optimizada",
    ):
        assert tabla in nombres


def test_lead_provider_registra_columnas() -> None:
    import pack_cobranza.providers  # noqa: F401

    from core.admin.lead_registry import lead_registry

    cols = [c.key for c in lead_registry.columns_for({"pack_cobranza"})]
    assert cols == ["saldo", "mora", "riesgo"]
