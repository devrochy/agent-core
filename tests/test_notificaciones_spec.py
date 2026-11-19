"""Contrato de `NotificationSpec` y visibilidad pura del feed (ADR-0167). Sin base ni red."""

from __future__ import annotations

import pytest

from core.notifications.feed import ve_notificacion
from core.notifications.spec import (
    CATEGORIAS,
    ENTIDADES,
    SEVERIDADES,
    NotificationSpec,
    NotificationSpecError,
)


def _spec(**kw) -> NotificationSpec:
    base = {
        "event_key": "core.escalation",
        "category": "conversaciones",
        "severity": "critical",
        "title": "Nueva escalación",
        "dedupe_key": "esc:1",
    }
    return NotificationSpec(**{**base, **kw})


def test_spec_valida_pasa() -> None:
    _spec(target={"entity": "conversacion", "id": "abc", "section": "conversations"}).validate()


@pytest.mark.parametrize(
    "cambio",
    [
        {"category": "otra"},
        {"severity": "grave"},
        {"title": "  "},
        {"dedupe_key": ""},
        {"event_key": ""},
        {"target": {"entity": "inventada"}},
    ],
)
def test_spec_invalida_lanza(cambio) -> None:
    with pytest.raises(NotificationSpecError):
        _spec(**cambio).validate()


def test_enums_cubren_el_contrato_del_producto() -> None:
    assert set(CATEGORIAS) == {"conversaciones", "ventas", "cobros", "agenda", "sistema"}
    assert set(SEVERIDADES) == {"info", "success", "warning", "critical"}
    assert "conversacion" in ENTIDADES and "cita" in ENTIDADES


def test_dueno_y_master_ven_todo() -> None:
    for kw in ({"actua_master": True, "tenant_role": None}, {"actua_master": False, "tenant_role": "owner"}):
        assert ve_notificacion(section=None, explicitos={}, **kw)
        assert ve_notificacion(section="pack_calendar:notificaciones", explicitos={}, **kw)


def test_colaborador_sin_fila_explicita_no_ve() -> None:
    # Heredar `read` por rol NO da avisos (mismo criterio que notification_recipients).
    assert not ve_notificacion(
        actua_master=False, tenant_role="colaborador", section="conversations", explicitos={}
    )


def test_colaborador_con_acceso_explicito_ve_y_none_deniega() -> None:
    kw = {"actua_master": False, "tenant_role": "colaborador"}
    assert ve_notificacion(section="conversations", explicitos={"conversations": ["read"]}, **kw)
    assert ve_notificacion(
        section="pack_calendar:notificaciones", explicitos={"pack_calendar": ["write"]}, **kw
    )
    assert not ve_notificacion(section="conversations", explicitos={"conversations": ["none"]}, **kw)
    # La sub-sección gana al pack: `none` en la sub-sección niega aunque el pack tenga acceso.
    assert not ve_notificacion(
        section="pack_calendar:notificaciones",
        explicitos={"pack_calendar": ["write"], "pack_calendar:notificaciones": ["none"]},
        **kw,
    )


def test_evento_sin_seccion_es_solo_de_duenos_y_masters() -> None:
    assert not ve_notificacion(
        actua_master=False, tenant_role="colaborador", section=None, explicitos={"conversations": ["read"]}
    )


def test_quien_no_pertenece_al_negocio_no_ve() -> None:
    assert not ve_notificacion(
        actua_master=False, tenant_role=None, section="conversations", explicitos={"conversations": ["read"]}
    )
