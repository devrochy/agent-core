"""Resolución pura de permisos (ADR-0136): `none` deniega, la sub-sección manda sobre el pack
y el default del rol solo aplica sin fila explícita."""

from __future__ import annotations

import uuid

from core.admin.permissions import (
    ACCESS_NONE,
    acceso_por_rol,
    resolver_explicito,
    resolver_seccion,
    seccion_padre,
)

TID = uuid.uuid4()


def test_seccion_padre() -> None:
    assert seccion_padre("pack_cobranza:reportes") == "pack_cobranza"
    assert seccion_padre("pack_cobranza") is None
    assert seccion_padre("costs") is None


def test_sin_filas_cae_al_default_del_rol() -> None:
    assert resolver_seccion("pack_cobranza:reportes", "colaborador", TID, {}) == "read"
    assert resolver_seccion("pack_cobranza:reportes", "owner", TID, {}) == "write"
    assert resolver_seccion("pack_cobranza:reportes", None, TID, {}) is None
    assert resolver_seccion("tenants", "owner", TID, {}) is None
    assert acceso_por_rol("costs", "colaborador", None) is None


def test_none_deniega_aunque_el_rol_herede() -> None:
    filas = {"pack_cobranza:reportes": [ACCESS_NONE]}
    assert resolver_seccion("pack_cobranza:reportes", "colaborador", TID, filas) is None
    assert resolver_seccion("pack_cobranza:reportes", "owner", TID, filas) is None
    # Las otras sub-secciones del pack siguen heredando.
    assert resolver_seccion("pack_cobranza:carteras", "colaborador", TID, filas) == "read"


def test_denegar_el_pack_completo_cubre_sus_subsecciones() -> None:
    filas = {"pack_cobranza": [ACCESS_NONE]}
    for sub in ("reportes", "carteras", "gestores", "cobros"):
        assert resolver_seccion(f"pack_cobranza:{sub}", "colaborador", TID, filas) is None
    # Otro pack no se ve afectado.
    assert resolver_seccion("pack_commerce", "colaborador", TID, filas) == "read"


def test_la_subseccion_manda_sobre_el_pack() -> None:
    filas = {"pack_cobranza": [ACCESS_NONE], "pack_cobranza:reportes": ["read"]}
    assert resolver_seccion("pack_cobranza:reportes", "colaborador", TID, filas) == "read"
    assert resolver_seccion("pack_cobranza:carteras", "colaborador", TID, filas) is None
    filas2 = {"pack_cobranza": ["write"], "pack_cobranza:cobros": [ACCESS_NONE]}
    assert resolver_seccion("pack_cobranza:cobros", "colaborador", TID, filas2) is None
    assert resolver_seccion("pack_cobranza:carteras", "colaborador", TID, filas2) == "write"


def test_dentro_de_un_nivel_none_gana_y_si_no_el_maximo() -> None:
    # Fila global `read` + fila del negocio `none` (misma sección): deniega.
    assert resolver_explicito({"costs": ["read", ACCESS_NONE]}, "costs") == (True, None)
    assert resolver_explicito({"costs": ["read", "write"]}, "costs") == (True, "write")
    assert resolver_explicito({}, "costs") == (False, None)
    assert resolver_explicito({"costs": ["raro"]}, "costs") == (False, None)


def test_colaboradores_no_se_deniega_por_fila() -> None:
    filas = {"collaborators": [ACCESS_NONE]}
    assert resolver_seccion("collaborators", "owner", TID, filas) == "write"
    assert resolver_seccion("collaborators", "colaborador", TID, {"collaborators": ["write"]}) is None


def test_observabilidad_el_colaborador_no_la_hereda() -> None:
    """ADR-0169 §1: el dueño la hereda; el colaborador solo con permiso explícito; `none` deniega."""
    assert resolver_seccion("observability", "owner", TID, {}) == "write"
    assert resolver_seccion("observability", "colaborador", TID, {}) is None
    assert resolver_seccion("observability", "colaborador", TID, {"observability": ["read"]}) == "read"
    assert resolver_seccion("observability", "owner", TID, {"observability": [ACCESS_NONE]}) is None
    # Las demás secciones del colaborador siguen heredando lectura.
    assert resolver_seccion("conversations", "colaborador", TID, {}) == "read"
