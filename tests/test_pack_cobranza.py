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
        "cob_registrar_promesa_saldo",  # solo la ofrece el procedimiento `promesa_de_pago` (HU-AOS-08)
        "cob_opciones_promesa",  # ídem: alcance de la promesa (HU-AOS-08, decisión del 2026-10-11)
        "cob_registrar_abono",
        "cob_consultar_suscripcion",
        "cob_crear_suscripcion",
        "cob_pausar_suscripcion",
        "cob_cancelar_suscripcion",
        "cob_renovar_suscripcion",
        "cob_link_suscripcion",
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
    # Bloque E: cobros al propietario y links de pago.
    assert "pack_cobranza_cobros_propietario" in nombres
    assert "pack_cobranza_links_pago" in nombres
    # Bloque D: dispositivos y offline.
    for tabla in ("pack_cobranza_devices", "pack_cobranza_intentos_acceso", "pack_cobranza_sync_eventos"):
        assert tabla in nombres
    # Épica 9 (HU-63): conversación con el propietario.
    assert "pack_cobranza_conversaciones_propietario" in nombres
    assert "pack_cobranza_mensajes_propietario" in nombres
    columns = {c.name for c in models.Gestor.__table__.columns}
    assert "admin_id" in columns
    assert "password_hash" not in columns  # identidad unificada (ADR-0066)
    assert "usuario" not in columns


def test_jwt_del_pack() -> None:
    """ADR-0057: el pack firma/valida su propio token (HS256, sin deps)."""
    from pack_cobranza.auth import issue_token, verify_token

    token = issue_token({"tenant_id": "t1", "gestor_id": "g1"}, "secreto", ttl_hours=1)
    assert verify_token(token, "secreto") is not None
    assert verify_token(token, "otro-secreto") is None
    assert verify_token("no-es-un-token", "secreto") is None


def test_rbac_subsecciones_por_path() -> None:
    """ADR-0063: cada ruta del pack resuelve a su sub-sección RBAC."""
    from pack_cobranza.module import CobranzaModule

    from core.admin.permissions import pack_prefixes, required_section

    prefixes = pack_prefixes([CobranzaModule()])
    casos = {
        "/cobranza/": "pack_cobranza:resumen",
        "/cobranza/carteras": "pack_cobranza:carteras",
        "/cobranza/carteras/123/config": "pack_cobranza:carteras",
        "/cobranza/clientes/abc": "pack_cobranza:clientes",
        "/cobranza/reglas": "pack_cobranza:reglas",
        "/cobranza/monitoreo": "pack_cobranza:monitoreo",
        "/cobranza/reportes": "pack_cobranza:reportes",
        "/cobranza/alertas": "pack_cobranza:alertas",
        "/cobranza/promesas": "pack_cobranza:promesas",
        "/cobranza/promesas/abc/estado": "pack_cobranza:promesas",
        "/cobranza/auditoria": "pack_cobranza:auditoria",
        "/cobranza/carteras/abc/caja": "pack_cobranza:carteras",
        "/cobranza/carteras/abc/trayecto/optimizar": "pack_cobranza:carteras",
        "/cobranza/carteras/abc/cierre": "pack_cobranza:carteras",
        "/cobranza/clientes/abc/evidencia/xyz": "pack_cobranza:clientes",
        "/cobranza/dispositivos": "pack_cobranza:dispositivos",
    }
    for path, esperado in casos.items():
        req = required_section(path, "GET", prefixes)
        assert req is not None and req[0] == esperado, path


def test_cobros_conserva_su_subseccion_rbac_sin_menu() -> None:
    """Se retiró «Cobros al propietario», pero `pack_cobranza:cobros` guarda «Pagos»."""
    from pack_cobranza.module import CobranzaModule

    from core.admin.permissions import pack_section_keys

    modulo = CobranzaModule()
    assert "pack_cobranza:cobros" in pack_section_keys([modulo])
    assert all(i["section_key"] != "pack_cobranza:cobros" for i in modulo.admin_menu)


def test_cobros_al_propietario_ya_no_expone_rutas() -> None:
    from pack_cobranza.admin_api import router

    rutas = {getattr(r, "path", "") for r in router.routes}
    assert not [r for r in rutas if "owner-charges" in r]
    assert "/api/v1/pack/cobranza/payments" in rutas


def test_lead_provider_registra_columnas() -> None:
    import pack_cobranza.providers  # noqa: F401

    from core.admin.lead_registry import lead_registry

    cols = [c.key for c in lead_registry.columns_for({"pack_cobranza"})]
    assert cols == [
        "cartera",
        "saldo",
        "mora",
        "riesgo",
        "estatus",
        "cuotas_vencidas",
        "proxima_cuota",
        "promesa",
        "ultimo_pago",
    ]


def test_api_gestor_rutas_f1() -> None:
    """ADR-0065: la API del gestor expone el contrato de la APK (F1: auth + perfil)."""
    from pack_cobranza import api_gestor

    rutas = {(r.path, tuple(sorted(getattr(r, "methods", set())))) for r in api_gestor.router.routes}
    assert ("/api/pack/pack_cobranza/auth/gestor/login", ("POST",)) in rutas
    assert ("/api/pack/pack_cobranza/auth/propietario/login", ("POST",)) in rutas
    assert ("/api/pack/pack_cobranza/auth/refresh", ("POST",)) in rutas
    assert ("/api/pack/pack_cobranza/perfil", ("PATCH",)) in rutas


def test_api_gestor_rutas_f2() -> None:
    """ADR-0065: las lecturas del gestor (F2) están montadas."""
    from pack_cobranza import api_gestor

    rutas = {r.path for r in api_gestor.router.routes}
    esperadas = [
        "/api/pack/pack_cobranza/gestor/mis-carteras",
        "/api/pack/pack_cobranza/gestor/notificaciones",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/trayecto-diario",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/estadisticas",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/clientes",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/clientes/{cliente_id}/tarjeta",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/clientes/{cliente_id}/prestamos",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/prestamos/{prestamo_id}/estado-cuenta",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/prestamos/{prestamo_id}/cuotas/{cuota_id}/detalle",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/notas",
        "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/liquidaciones",
        "/api/pack/pack_cobranza/clientes/{cliente_id}/evidencia/{ev_id}",
    ]
    faltan = [p for p in esperadas if p not in rutas]
    assert not faltan, faltan


def test_api_gestor_rutas_f3() -> None:
    """ADR-0065: las escrituras del gestor (F3) están montadas."""
    from pack_cobranza import api_gestor

    rutas = {(r.path, tuple(sorted(getattr(r, "methods", set())))) for r in api_gestor.router.routes}
    base = "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}"
    esperadas = [
        (base, ("PATCH",)),
        (base + "/configuracion", ("PATCH",)),
        (base + "/notas", ("POST",)),
        (base + "/apertura", ("POST",)),
        (base + "/posicion", ("POST",)),
        (base + "/trayectoria-real", ("POST",)),
        (base + "/trayecto", ("POST",)),
        (base + "/clientes", ("POST",)),
        (base + "/clientes/{cliente_id}", ("PATCH",)),
        (base + "/clientes/{cliente_id}/evidencias", ("POST",)),
        (base + "/visitas/pago", ("POST",)),
        (base + "/visitas/no-pago", ("POST",)),
        (base + "/gastos", ("POST",)),
        (base + "/prestamos", ("POST",)),
        (base + "/cuotas/{cuota_id}", ("PATCH",)),
        (base + "/cuotas/{cuota_id}", ("DELETE",)),
        (base + "/abonos/{record_id}", ("DELETE",)),
        (base + "/pagos/{record_id}", ("DELETE",)),
        (base + "/liquidaciones", ("POST",)),
        (base + "/liquidaciones/{liquidacion_id}/export-pdf", ("GET",)),
    ]
    faltan = [p for p in esperadas if p not in rutas]
    assert not faltan, faltan


def test_api_gestor_rutas_f4() -> None:
    """ADR-0065: el sync offline del gestor (F4) está montado."""
    from pack_cobranza import api_gestor

    rutas = {(r.path, tuple(sorted(getattr(r, "methods", set())))) for r in api_gestor.router.routes}
    assert ("/api/pack/pack_cobranza/sync-offline/eventos", ("POST",)) in rutas
    assert ("/api/pack/pack_cobranza/sync-offline/trayecto-diario", ("GET",)) in rutas


def test_api_gestor_dispositivo_rutas() -> None:
    """HU-39/43: rutas de autovinculación de dispositivo."""
    from pack_cobranza import api_gestor

    rutas = {r.path for r in api_gestor.router.routes}
    assert "/api/pack/pack_cobranza/gestor/dispositivo" in rutas


def test_pago_qr_informativo_y_ruta() -> None:
    """ADR-0067: cobro con QR informativo cuando no hay pasarela."""
    from pack_cobranza import api_gestor
    from pack_cobranza.config import CobranzaConfig
    from pack_cobranza.pagos import datos_informativos

    cfg = CobranzaConfig(pago_titular="T", pago_banco="B", pago_alias="@x")
    data = datos_informativos(cfg, 1000.0)
    assert data["modo"] == "informativo" and data["titular"] == "T" and data["alias"] == "@x"
    assert data["qrPng"] is None or data["qrPng"].startswith("data:image/png;base64,")

    rutas = {r.path for r in api_gestor.router.routes}
    assert "/api/pack/pack_cobranza/gestor/carteras/{cartera_id}/cuotas/{cuota_id}/link-pago" in rutas


def test_api_gestor_par_de_tokens() -> None:
    import uuid

    from pack_cobranza.api_gestor import _par_tokens
    from pack_cobranza.auth import verify_token

    tenant_id, admin_id, gestor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pair = _par_tokens("secreto", tenant_id, admin_id, rol="gestor", gestor_id=gestor_id)
    access = verify_token(pair["accessToken"], "secreto")
    refresh = verify_token(pair["refreshToken"], "secreto")
    assert access is not None and access["typ"] == "access"
    assert access["admin_id"] == str(admin_id) and access["gestor_id"] == str(gestor_id) and access["rol"] == "gestor"
    assert refresh is not None and refresh["typ"] == "refresh"

    prop = verify_token(_par_tokens("secreto", tenant_id, admin_id, rol="propietario")["accessToken"], "secreto")
    assert prop is not None and prop["rol"] == "propietario" and "gestor_id" not in prop


def test_modulo_declara_rol_gestor() -> None:
    """ADR-0066: el pack aporta el rol de colaborador "Gestor de cobranza"."""
    from pack_cobranza.module import CobranzaModule

    roles = {r["id"]: r["label"] for r in CobranzaModule().collaborator_roles}
    assert roles.get("gestor") == "Gestor de cobranza"


def test_trayecto_vecino_mas_cercano() -> None:
    """El orden del trayecto encadena por vecino más cercano desde el inicio."""
    from pack_cobranza.admin import _haversine_km, _ordenar_trayecto

    clientes = [
        {"id": "a", "lat": 4.60, "lng": -74.08},
        {"id": "b", "lat": 4.70, "lng": -74.08},
        {"id": "c", "lat": 4.601, "lng": -74.0801},
    ]
    orden, km = _ordenar_trayecto(clientes, (4.60, -74.08))
    assert orden[0] == "a"  # el inicio coincide con "a"
    assert orden[1] == "c"  # el más cercano a "a"
    assert km > 0
    assert _haversine_km((4.60, -74.08), (4.60, -74.08)) == 0


def test_evidencias_valida_slug_y_mime() -> None:
    from pack_cobranza.evidencias import TIPOS, EvidenciaError, _slug_seguro

    assert "foto_facial" in TIPOS
    assert _slug_seguro("demo-cobranza") == "demo-cobranza"
    for malo in ("../otro", "a/b", ""):
        try:
            _slug_seguro(malo)
        except EvidenciaError:
            continue
        raise AssertionError(f"slug aceptado: {malo!r}")
