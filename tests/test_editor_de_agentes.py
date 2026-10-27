"""HU-AOS-24 · reglas puras del editor de agentes (sin base ni red).

- Superpoderes: al menos uno, disponible y con `MIN_FRASES` frases distintas (las de la
  habilidad más las propias; repetir una de la habilidad, con otras tildes o mayúsculas, no suma).
- Modelo: uno de la tabla que el proveedor del negocio no sirve, o retirado, no pasa.
- Costo estimado por 1.000 turnos desde la tabla de precios y el consumo real; sin consumo, nada.
- Plantillas: disponibles solo con sus habilidades, con los superpoderes que publican.
- Conexiones al publicar (D-35): al menos una, sin repetir, del negocio y con las habilidades.
"""

from __future__ import annotations

import uuid

from core.agents import editor
from core.agents.editor import Destino, Superpoder


def _poder(frases: tuple[str, ...] = ("quiero comprar",)) -> Superpoder:
    return Superpoder("pack_commerce.tomar_pedido", "1.0.0", "pack_commerce", "Tomar un pedido", "", frases)


def _con(*refs: dict) -> dict:
    return {"procedures": list(refs)}


def test_sin_superpoderes_pide_marcar_uno() -> None:
    [issue] = editor.problemas_de_superpoderes({"procedures": []}, {})
    assert (issue.code, issue.path) == ("sin_superpoderes", "procedures")


def test_superpoder_no_disponible_con_las_habilidades_del_negocio() -> None:
    [issue] = editor.problemas_de_superpoderes(_con({"id": "pack_cobranza.promesa_de_pago", "version": "^1.0"}), {})
    assert (issue.code, issue.path) == ("superpoder_no_disponible", "procedures[0].id")


def test_frases_minimas_por_campo_y_las_copias_no_suman() -> None:
    poder = _poder()
    ref = {"id": poder.id, "version": "^1.0", "overrides": {"examples": ["Quiero COMPRAR", "quiéro comprar!"]}}
    [issue] = editor.problemas_de_superpoderes(_con(ref), {poder.id: poder})
    assert (issue.code, issue.path) == ("frases_minimas", "procedures[0].overrides.examples")
    assert f"al menos {editor.MIN_FRASES}" in issue.message and "agrega 2" in issue.message


def test_frases_propias_completan_el_minimo() -> None:
    poder = _poder()
    ref = {"id": poder.id, "version": "^1.0", "overrides": {"examples": ["hacer un pedido", "me lo vendes"]}}
    assert editor.problemas_de_superpoderes(_con(ref), {poder.id: poder}) == []


def test_las_frases_de_la_habilidad_cuentan_sin_overrides() -> None:
    poder = _poder(("quiero comprar", "hacer un pedido", "precio de algo"))
    assert editor.problemas_de_superpoderes(_con({"id": poder.id, "version": "^1.0"}), {poder.id: poder}) == []


def test_superpoder_de_otro_enfoque_no_se_publica() -> None:
    """Cobranza con «tomar un pedido»: existe en el negocio pero no es de su enfoque."""
    poder = Superpoder("pack_commerce.tomar_pedido", "1.1.0", "pack_commerce", "Tomar un pedido", "", ("a", "b", "c"))
    permitidos = editor.procedimientos_del_enfoque("cobranza")
    assert permitidos == {"pack_cobranza.promesa_de_pago"}
    [issue] = editor.problemas_de_superpoderes(
        _con({"id": poder.id, "version": "^1.0"}), {poder.id: poder}, permitidos
    )
    assert issue.code == "superpoder_fuera_de_enfoque"
    assert issue.path == "procedures[0].id"
    assert "Tomar un pedido" in issue.message


def test_superpoder_del_enfoque_pasa() -> None:
    poder = Superpoder("pack_commerce.tomar_pedido", "1.1.0", "pack_commerce", "Tomar un pedido", "", ("a", "b", "c"))
    permitidos = editor.procedimientos_del_enfoque("ventas")
    assert poder.id in permitidos
    assert editor.problemas_de_superpoderes(_con({"id": poder.id, "version": "^1.0"}), {poder.id: poder}, permitidos) == []


def test_key_que_no_es_de_plantilla_no_restringe() -> None:
    """`general`, `coordinador` y especialistas por API: catálogo completo, sin fuera de enfoque."""
    for key in ("general", "coordinador", "especialista_a_mano"):
        assert editor.procedimientos_del_enfoque(key) is None
    poder = _poder(("a", "b", "c"))
    assert editor.problemas_de_superpoderes(_con({"id": poder.id, "version": "^1.0"}), {poder.id: poder}, None) == []


def test_cada_plantilla_solo_reclama_sus_procedimientos() -> None:
    """Ningún procedimiento está en dos enfoques: es lo que hace que el filtro segmente."""
    vistos: dict[str, str] = {}
    for key in editor.keys_de_plantillas():
        for proc in editor.procedimientos_del_enfoque(key) or ():
            assert proc not in vistos, f"{proc} está en {vistos[proc]} y en {key}"
            vistos[proc] = key


def test_modelo_de_otro_proveedor_o_retirado() -> None:
    def problema(modelo: str, proveedor: str) -> str | None:
        issue = editor.problema_de_modelo({"model": {"primary": modelo}}, proveedor)
        return issue.code if issue else None

    assert problema("claude-haiku-4-5", "deepseek") == "modelo_de_otro_proveedor"
    assert problema("deepseek-flash", "anthropic") == "modelo_de_otro_proveedor"
    assert problema("gpt-4o-mini", "openai_compatible") is None
    assert problema("deepseek-flash", "deepseek") is None
    assert problema("deepseek-chat", "deepseek") == "modelo_retirado"
    assert problema("modelo-propio-del-negocio", "deepseek") is None  # fuera de la tabla: pasa


def test_tabla_de_precios_sale_de_las_del_core() -> None:
    from core.llm.anthropic_provider import PRICING as ANTHROPIC
    from core.llm.openai_compatible import PRICING as COMPATIBLES

    filas = {(f[1], f[2], f[3]) for f in editor.tabla_de_precios()}
    assert filas == {(m, p[0], p[1]) for m, p in (COMPATIBLES | ANTHROPIC).items()}


def test_costo_estimado_por_mil_turnos() -> None:
    assert editor.estimado(0.14, 0.28, 2000, 400) == 0.392
    assert editor.estimado(0.14, 0.28, None, None) is None


INSTALADOS = {"pack_commerce", "pack_calendar", "pack_cobranza", "pack_subscriptions"}


def test_plantillas_se_ofrecen_y_habilitan_sus_habilidades() -> None:
    """Elegir la plantilla habilita lo que pide: estar habilitada ya no es precondición."""
    vistas = {p.key: p for p in editor.plantillas({"pack_commerce"}, INSTALADOS)}
    assert set(vistas) == {"agenda", "cobranza", "suscripciones", "ventas"}
    ventas = vistas["ventas"]
    assert ventas.available and ventas.will_enable == () and ventas.missing_skills == ()
    assert [s.id for s in ventas.superpowers] == ["pack_commerce.tomar_pedido", "pack_commerce.contratar_plan"]
    assert all(len(s.phrases) >= editor.MIN_FRASES for s in ventas.superpowers)
    agenda = vistas["agenda"]
    assert agenda.available and agenda.will_enable == ("pack_calendar",) and agenda.missing_skills == ()
    assert [s.id for s in agenda.superpowers] == ["pack_calendar.agendar_cita"]


def test_plantillas_sin_ninguna_habilidad_habilitada_siguen_disponibles() -> None:
    vistas = editor.plantillas(set(), INSTALADOS)
    assert all(v.available and v.superpowers and v.missing_skills == () for v in vistas)
    assert {v.key: v.will_enable for v in vistas} == {
        "agenda": ("pack_calendar",),
        "cobranza": ("pack_cobranza",),
        "suscripciones": ("pack_subscriptions",),
        "ventas": ("pack_commerce",),
    }


def test_plantilla_no_disponible_solo_si_el_despliegue_no_trae_la_habilidad() -> None:
    vistas = {p.key: p for p in editor.plantillas(set(), INSTALADOS - {"pack_calendar"})}
    assert not vistas["agenda"].available and vistas["agenda"].missing_skills == ("pack_calendar",)
    assert vistas["ventas"].available


def test_habilidades_del_contenido() -> None:
    contenido = _con({"id": "pack_commerce.tomar_pedido"}, {"id": "pack_calendar.agendar_cita"}, {"x": 1})
    assert editor.habilidades_del_contenido(contenido) == {"pack_commerce", "pack_calendar"}


def test_conexiones_al_publicar_d35() -> None:
    ok, sin_hab = uuid.uuid4(), uuid.uuid4()
    disponibles = {
        ok: Destino(ok, "IG", "instagram", False, True, (), None),
        sin_hab: Destino(sin_hab, "WA", "whatsapp", False, False, ("pack_calendar",), None),
    }
    assert editor._validar_conexiones([], disponibles) == {
        "connection_ids": "Elige al menos una conexión donde instalar el agente."
    }
    assert editor._validar_conexiones([ok], disponibles) == {}
    campos = editor._validar_conexiones([ok, sin_hab, uuid.uuid4()], disponibles)
    assert set(campos) == {"connection_ids[1]", "connection_ids[2]"}
    assert "pack_calendar" in campos["connection_ids[1]"]
    assert "connection_ids" in editor._validar_conexiones([ok, ok], disponibles)
