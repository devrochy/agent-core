"""Qué claves Redis barre la purga de un negocio (`core.tenants._es_del_tenant`).

Convención: el estado de un tenant lleva su UUID como primer UUID del nombre. Lo demás
(familias solo por conversación, claves de varios negocios, ids hijos de otro negocio)
no lo toca el barrido.
"""

from __future__ import annotations

import uuid

import pytest

from core.tenants import _es_del_tenant

T = str(uuid.uuid4())
OTRO = str(uuid.uuid4())
CONV = str(uuid.uuid4())


@pytest.mark.parametrize(
    "clave",
    [
        f"wm:{T}:{CONV}",
        f"turns:{T}",
        f"inbound:{T}:meta:wamid.abc",
        f"alta:terminos:{T}:{CONV}:v1",
        f"waweb:outbox:{T}",
        f"waweb:cap:{T}:2026-10-10",
        f"meta:paused:{T}:{CONV}",
        f"outbound:claim:{T}:{CONV}:{OTRO}",
    ],
)
def test_claves_del_tenant(clave: str) -> None:
    assert _es_del_tenant(clave, T) is True


@pytest.mark.parametrize(
    "clave", [f"debounce:payload:{T}", f"debounce:messages:{T}", f"wm:{T}"]
)
def test_familias_solo_por_conversacion_no_se_barren(clave: str) -> None:
    assert _es_del_tenant(clave, T) is False


@pytest.mark.parametrize(
    "clave",
    [
        f"wm:{OTRO}:{T}",  # T es una conversación de OTRO
        f"webhook_firma_ajena:{OTRO}:{T}",  # clave de varios negocios
        f"outbound:claim:{OTRO}:{CONV}:{T}",
    ],
)
def test_uuid_en_otra_posicion_no_es_suyo(clave: str) -> None:
    assert _es_del_tenant(clave, T) is None
