"""HU-AOS-08 · procedimientos de agenda, cambio de plan y promesa de pago publicados por los packs.

Unitarios, sin BD ni red:
- los JSON de `packages/pack-*/procedures/` pasan el validador y el linter contra el binario
  (lo mismo que hace el motor en runtime; el CI los corre aquí);
- las acciones nuevas son "solo de procedimiento": ningún `flow.json` las nombra, no son
  `always_available` y no entran en los bindings del General v3 (sin cambio con banderas apagadas);
- las reglas puras: horario ofrecido, cambio de plan (ADR-0111) y fecha de la promesa.
"""

from __future__ import annotations

import json
import types
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from core.kernel.procedures import ActionInfo, catalogo_de_acciones, de_pack, lint
from core.plugin_manager import plugin_manager

ROOT = Path(__file__).resolve().parents[1]
PUBLICADOS = {
    "pack_calendar": ["pack_calendar.agendar_cita"],
    "pack_subscriptions": ["pack_subscriptions.cambiar_plan", "pack_subscriptions.contratar_plan"],
    "pack_cobranza": ["pack_cobranza.promesa_de_pago"],
}
SOLO_PROCEDIMIENTO = {
    "sub_cotizar_cambio_plan", "sub_aplicar_cambio_plan", "cob_registrar_promesa_saldo", "cob_opciones_promesa",
    "sub_listar_planes", "sub_cotizar_inscripcion", "sub_confirmar_inscripcion", "sub_inscribir",
}


@pytest.fixture(scope="module")
def catalogo() -> Mapping[str, ActionInfo]:
    plugin_manager.scan()
    return catalogo_de_acciones(plugin_manager.list_modules())


# ------------------------------------------------------------------ publicación


@pytest.mark.parametrize("pack_id", sorted(PUBLICADOS))
def test_el_pack_publica_su_procedimiento_y_pasa_el_linter(pack_id: str, catalogo) -> None:
    procs = de_pack.cargar(pack_id)
    assert sorted(p.id for p in procs) == sorted(PUBLICADOS[pack_id])
    for proc in procs:
        assert lint(proc, catalogo) == []


def test_todos_los_json_de_packs_pasan_el_linter(catalogo) -> None:
    """Cualquier procedimiento que un pack agregue a `procedures/` pasa el linter (los de AOS-07 también)."""
    rutas = sorted(ROOT.glob("packages/pack-*/procedures/*.json"))
    assert len(rutas) >= len(PUBLICADOS)
    for ruta in rutas:
        pack_id = ruta.parent.parent.name.replace("-", "_")
        procs = {p.id: p for p in de_pack.cargar(pack_id)}
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
        assert crudo["id"] in procs, ruta
        assert lint(procs[crudo["id"]], catalogo) == [], ruta


def test_un_json_roto_se_salta_y_el_pack_sigue(tmp_path, monkeypatch) -> None:
    carpeta = tmp_path / "packages" / "pack-falso" / "procedures"
    carpeta.mkdir(parents=True)
    (carpeta / "a_roto.json").write_text("{no es json", encoding="utf-8")
    (carpeta / "b_invalido.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
    (carpeta / "c_bueno.json").write_text(
        json.dumps({"id": "pack_falso.ok", "version": "1.0.0", "title": "Ok", "stages": [{"id": "uno"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(de_pack, "REPO_ROOT", tmp_path)
    assert [p.id for p in de_pack.cargar("pack_falso")] == ["pack_falso.ok"]
    assert de_pack.cargar("pack_sin_carpeta") == []


def test_cada_pack_registra_procedimiento_y_resolvers() -> None:
    from pack_calendar.procedimientos import registrar as reg_calendar
    from pack_cobranza.procedimientos import registrar as reg_cobranza
    from pack_cobranza.skills import CobranzaSkill
    from pack_subscriptions.procedimientos import registrar as reg_subs

    from core.mcp.host import MCPHost
    from core.tools.core_skill import register_core_skills

    host = MCPHost()
    register_core_skills(host)  # como en producción: las `sub_*` del core se registran antes que los packs
    for pack_id, registrar in (
        ("pack_calendar", reg_calendar), ("pack_subscriptions", reg_subs), ("pack_cobranza", reg_cobranza),
    ):
        host.begin_activation(pack_id)
        if pack_id == "pack_cobranza":
            host.register_skill(CobranzaSkill())  # el resumen de la confirmación es de una tool suya
        registrar(host, pack_id)
        host.end_activation()
    assert host.get_resumen("cob_registrar_promesa_saldo") is not None
    assert host.get_resumen("sub_confirmar_inscripcion") is not None
    assert {p.id for lista in host.get_procedures().values() for p in lista} == {
        i for ids in PUBLICADOS.values() for i in ids
    }
    for nombre in (
        "pack_calendar.horario_ofrecido", "pack_calendar.tipo_de_cita",
        "pack_subscriptions.plan_destino", "pack_subscriptions.plan_a_contratar",
        "pack_subscriptions.codigo_de_promocion", "pack_subscriptions.metodo_de_pago",
        "pack_subscriptions.tipo_de_negocio", "pack_subscriptions.nombre_de_negocio", "pack_cobranza.fecha_de_promesa",
    ):
        assert host.get_resolver(nombre) is not None, nombre


# ------------------------------------------------------------------ sin cambio con banderas apagadas


def test_las_acciones_nuevas_no_estan_en_ningun_flujo() -> None:
    nombradas = set()
    for ruta in [ROOT / "core" / "flow.json", *ROOT.glob("packages/pack-*/flow.json")]:
        for seccion in json.loads(ruta.read_text(encoding="utf-8")).get("sections", []):
            nombradas |= set(seccion.get("tools") or [])
    assert not (SOLO_PROCEDIMIENTO & nombradas)


def test_las_tools_del_core_del_procedimiento_no_son_always_available() -> None:
    from core.subscriptions.tools import SOLO_PROCEDIMIENTO as DEL_CORE
    from core.subscriptions.tools import SubscriptionSkill

    herramientas = {t.name: t for t in SubscriptionSkill().get_tools()}
    assert set(DEL_CORE) <= set(herramientas)
    assert {n for n, t in herramientas.items() if not t.always_available} == set(DEL_CORE)
    # Las de siempre siguen igual.
    for nombre in ("sub_consultar", "sub_link_pago", "sub_pausar", "sub_cancelar", "sub_cambiar_plan"):
        assert herramientas[nombre].always_available


def test_los_bindings_del_general_no_cambian(catalogo, monkeypatch) -> None:
    from core.agents.tolerante import candidatas_del_spec

    monkeypatch.setattr("core.mcp.declarative.tools_for_spec", lambda spec: [])
    spec = types.SimpleNamespace(packs=["pack_subscriptions", "pack_cobranza", "pack_calendar"])
    _candidatas, siempre = candidatas_del_spec(spec, catalogo)  # type: ignore[arg-type]
    assert not (SOLO_PROCEDIMIENTO & siempre)
    assert {"sub_consultar", "sub_cambiar_plan", "escalate_to_human"} <= siempre


async def test_oferta_segun_el_motor(monkeypatch) -> None:
    """B2 de la revisión: con el motor activo el modelo no puede saltarse el botón con la tool de
    siempre, y las del procedimiento no se ofrecen sin la bandera (aunque un flujo las nombre).
    Si la bandera no se lee, lo de siempre se ofrece y lo nuevo no."""
    from core.kernel import oferta

    estado: dict[str, bool | None] = {"activo": False}

    async def motor() -> bool | None:
        return estado["activo"]

    async def no(_tipos: set[str]) -> bool:
        return False

    monkeypatch.setattr(oferta, "motor_activo", motor)
    viejo, nuevo = oferta.sin_motor_activo(), oferta.solo_con_motor_activo()
    for activo, esperado_viejo, esperado_nuevo in ((False, True, False), (None, True, False), (True, False, True)):
        estado["activo"] = activo
        assert (await viejo(set()), await nuevo(set())) == (esperado_viejo, esperado_nuevo), activo
    assert await oferta.sin_motor_activo(no)(set()) is False
    assert await oferta.solo_con_motor_activo(no)(set()) is False


def test_las_tools_reemplazadas_y_las_nuevas_usan_el_predicado_del_motor() -> None:
    from pack_cobranza.skills import CobranzaSkill

    from core.subscriptions.tools import SubscriptionSkill

    herramientas = {t.name: t for t in [*SubscriptionSkill().get_tools(), *CobranzaSkill().get_tools()]}
    for nombre in ("sub_cambiar_plan", "cob_registrar_promesa_pago", *SOLO_PROCEDIMIENTO):
        assert herramientas[nombre].connection_predicate is not None, nombre


# ------------------------------------------------------------------ agenda


def test_horario_ofrecido_solo_si_esta_en_los_libres_del_servidor() -> None:
    from pack_calendar.procedimientos import hora_ofrecida, mensaje_no_ofrecido
    from pack_calendar.skills import _free_slots

    tz = ZoneInfo("America/Bogota")
    dia = "2027-03-18"
    ocupado = (datetime(2027, 3, 18, 10, tzinfo=tz), datetime(2027, 3, 18, 11, tzinfo=tz))
    libres = _free_slots("09:00-12:00", [ocupado], tz, dia, slot_minutes=30, duration_minutes=60, buffer_minutes=0, limit=96)
    assert libres == ["09:00", "11:00"]
    assert hora_ofrecida(datetime(2027, 3, 18, 11, tzinfo=tz), libres)
    assert not hora_ofrecida(datetime(2027, 3, 18, 10, tzinfo=tz), libres)       # ocupado
    assert not hora_ofrecida(datetime(2027, 3, 18, 9, 15, tzinfo=tz), libres)    # fuera de la cuadrícula
    assert not hora_ofrecida(datetime(2027, 3, 18, 15, tzinfo=tz), libres)       # fuera de horario
    texto = mensaje_no_ofrecido("jueves 18 de marzo de 2027", libres)
    assert "09:00, 11:00" in texto
    assert "find_next_available" in mensaje_no_ofrecido("domingo 21 de marzo de 2027", [])


# ------------------------------------------------------------------ cambio de plan (ADR-0111)


def _sub(**campos: Any) -> Any:
    ahora = datetime.now(UTC)
    base = {
        "variante_id": uuid.uuid4(), "estado": "activa", "valor_periodo": Decimal("200000"), "periodicidad": "mensual",
        "periodo_actual_inicio": ahora - timedelta(days=15), "proximo_cargo": ahora + timedelta(days=15),
        "dia_ancla": 1, "moneda": "COP",
    }
    return types.SimpleNamespace(**{**base, **campos})


PREMIUM = {"plan_code": "PLAN-PREMIUM", "nombre": "Premium", "precio": 600000.0, "moneda": "COP", "periodicidad": "mensual"}


def test_evaluar_cambio_upgrade_prorrateado_downgrade_programado_y_rechazos() -> None:
    from pack_subscriptions.capability import evaluar_cambio

    from core.subscriptions import SubscriptionError

    ahora = datetime.now(UTC)
    sub = _sub()
    up = evaluar_cambio(sub, variant_id=str(uuid.uuid4()), plan=PREMIUM, ahora=ahora)
    assert (up.cambio, up.monto) == ("pendiente_pago", Decimal("200000"))  # 400.000 × 15/30
    down = evaluar_cambio(_sub(valor_periodo=Decimal("600000")), variant_id=str(uuid.uuid4()),
                          plan={**PREMIUM, "precio": 200000.0}, ahora=ahora)
    assert (down.cambio, down.monto) == ("programado", Decimal("0"))
    igual = evaluar_cambio(sub, variant_id=str(uuid.uuid4()), plan={**PREMIUM, "precio": 200000.0}, ahora=ahora)
    assert igual.cambio == "inmediato"
    with pytest.raises(SubscriptionError) as exc:
        evaluar_cambio(sub, variant_id=str(sub.variante_id), plan=PREMIUM, ahora=ahora)
    assert exc.value.code == "mismo_plan"
    for sub_mala, plan, codigo in (
        (_sub(estado="pausada"), PREMIUM, "estado_no_permite_cambio"),
        (sub, {**PREMIUM, "periodicidad": "anual"}, "periodicidad_distinta"),
    ):
        with pytest.raises(SubscriptionError) as exc:
            evaluar_cambio(sub_mala, variant_id=str(uuid.uuid4()), plan=plan, ahora=ahora)
        assert exc.value.code == codigo


def test_mismo_monto_con_el_redondeo_del_prorrateo() -> None:
    from pack_subscriptions.capability import mismo_monto

    assert mismo_monto(Decimal("200000"), 200000, "COP")
    assert mismo_monto(Decimal("200000"), "200000.4", "COP")
    assert not mismo_monto(Decimal("200000"), 199000, "COP")
    assert not mismo_monto(Decimal("10.50"), 10.49, "USD")
    assert not mismo_monto(Decimal("1"), "no es número", "COP")


# ------------------------------------------------------------------ promesa de pago


def test_reglas_de_la_fecha_de_promesa() -> None:
    from pack_cobranza.procedimientos import ReglasDePromesa, error_de_fecha, mismo_saldo

    hoy = date(2026, 10, 12)  # lunes
    reglas = ReglasDePromesa(hoy=hoy, max_dias=15, sin_domingos=True, pendientes=0, max_promesas=2)
    assert error_de_fecha(date(2026, 10, 16), reglas) is None                    # el viernes
    assert "pasada" in (error_de_fecha(date(2026, 10, 11), reglas) or "")
    assert "2026-10-27" in (error_de_fecha(date(2026, 10, 28), reglas) or "")   # pasa de 15 días
    assert "domingo" in (error_de_fecha(date(2026, 10, 18), reglas) or "")
    llenas = ReglasDePromesa(hoy=hoy, max_dias=15, sin_domingos=True, pendientes=2, max_promesas=2)
    assert "asesor" in (error_de_fecha(date(2026, 10, 16), llenas) or "")
    assert mismo_saldo(Decimal("850000"), 850000.0, "COP")
    assert not mismo_saldo(Decimal("850000"), 800000, "COP")
    assert not mismo_saldo(Decimal("850000"), True, "COP")


def test_opciones_de_promesa_segun_lo_vencido_y_lo_que_permite_el_negocio() -> None:
    """Decisión del dueño (2026-10-11): por lo vencido o por el saldo total; una sola opción si lo
    vencido es 0 o igual al saldo; el negocio puede restringir los alcances."""
    from pack_cobranza.procedimientos import opciones_de_promesa

    def ver(saldo: int, vencido: int, permitido: str | None) -> list[tuple[str, Decimal]]:
        return [(o.alcance, o.monto) for o in opciones_de_promesa(Decimal(saldo), Decimal(vencido), permitido)]

    assert ver(600, 300, "ambos") == [("vencido", Decimal(300)), ("saldo_total", Decimal(600))]
    assert ver(600, 0, "ambos") == [("saldo_total", Decimal(600))]             # nada vencido
    assert ver(600, 600, "ambos") == [("saldo_total", Decimal(600))]           # todo vencido
    assert ver(600, 300, "saldo_total") == [("saldo_total", Decimal(600))]
    assert ver(600, 300, "vencido") == [("vencido", Decimal(300))]
    assert ver(600, 600, "vencido") == [("vencido", Decimal(600))]
    assert ver(600, 0, "vencido") == []                                         # nada que prometer
    assert ver(0, 0, "ambos") == []
    assert ver(600, 300, "otro") == [("saldo_total", Decimal(600))]            # desconocido: lo de antes
    assert ver(600, 900, "ambos") == [("saldo_total", Decimal(600))]           # nunca más que el saldo
