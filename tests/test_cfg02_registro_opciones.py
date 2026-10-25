"""HU-CFG-02 (SPEC-37, ADR-0166): registro de opciones de configuración del negocio.

Unitarias, sin base de datos: forma de cada opción, valores derivados del runtime, filtrado por
rol, opciones por negocio, que el runtime de turnos no importe el registro, y la prueba de
consistencia (`ast`): toda clave literal que el código lee de `behavior_config` o
`escalation_config` está registrada, y toda clave registrada tiene lector.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
from agentcore_contracts import Persona
from pydantic import ValidationError

from core.config import DEBOUNCE_DELAY_MS
from core.config_registro import OPCIONES, POR_CLAVE, Dependencia, Opcion, catalogo_para
from core.gateway.message_splitter import CAPS
from core.orchestrator.verificador import TOGGLES
from core.waweb import policy

RAIZ = Path(__file__).resolve().parent.parent

CLAVES_ESPERADAS = {
    "assistant_name", "business_description", "brand_voice", "language", "currency", "timezone",
    "bot_paused", "debounce_mode", "split_style", "guardrail_fail_action", "guardrail_max_retries",
    "disabled_guardrails", "difficulty_long_message_threshold",
    "difficulty_turnos_sin_resolver_threshold", "difficulty_score_threshold", "palabras_de_baja",
    "waweb_mode", "waweb_send_min_delay_ms", "waweb_send_max_delay_ms", "waweb_daily_outbound_cap",
    "waweb_warmup_enabled", "escalation_keywords", "auto_escalate_after_turns", "business_tz",
    "business_ranges", "out_of_hours_message", "persona_tone", "persona_warmth", "persona_emojis",
    "persona_max_length", "persona_forbidden",
}  # fmt: skip


# ------------------------------------------------------------------ forma
def test_son_las_31_opciones_del_primer_recorte_sin_repetidas() -> None:
    claves = [o.clave for o in OPCIONES]
    assert len(claves) == len(set(claves)) == 31
    assert set(claves) == CLAVES_ESPERADAS


def test_cada_opcion_tiene_etiqueta_y_explicacion_en_espanol() -> None:
    for o in OPCIONES:
        assert o.etiqueta.strip(), o.clave
        assert len(o.explicacion.strip()) >= 20, o.clave


def test_las_de_elegir_traen_valores_con_etiqueta_salvo_las_calculadas() -> None:
    for o in OPCIONES:
        if o.tipo in ("elegir_una", "elegir_varias") and not o.valores_calculados:
            assert o.valores, o.clave
            assert all(v.etiqueta.strip() for v in o.valores), o.clave
            assert len({v.valor for v in o.valores}) == len(o.valores), o.clave


def test_los_numeros_llevan_rango_y_el_defecto_cabe_en_el_rango() -> None:
    for o in OPCIONES:
        if o.tipo == "entero":
            assert o.minimo is not None and o.maximo is not None and o.minimo <= o.maximo, o.clave
            assert o.minimo <= o.por_defecto <= o.maximo, o.clave


def test_el_defecto_de_una_opcion_de_elegir_es_uno_de_sus_valores() -> None:
    for o in OPCIONES:
        # `business_tz` no tiene defecto propio: hereda la zona del negocio.
        if o.tipo == "elegir_una" and o.valores and o.por_defecto is not None:
            assert o.por_defecto in {v.valor for v in o.valores}, o.clave


def test_opcion_rechaza_campos_desconocidos_y_formas_incoherentes() -> None:
    base = dict(clave="x", grupo="identidad", tipo="texto", etiqueta="X", explicacion="x" * 30, almacen="columna:x")
    Opcion(**base)
    with pytest.raises(ValidationError):
        Opcion(**base, inventado=1)
    with pytest.raises(ValidationError):
        Opcion(**{**base, "tipo": "elegir_una"})  # sin valores
    with pytest.raises(ValidationError):
        Opcion(**{**base, "tipo": "entero"})  # sin rango
    with pytest.raises(ValidationError):
        Opcion(**{**base, "minimo": 1, "maximo": 2})  # texto con rango
    with pytest.raises(ValidationError):
        Opcion(**{**base, "almacen": "nube:x"})
    with pytest.raises(ValidationError):
        Opcion(**base, depende_de=Dependencia(clave="y", condicion="igual", motivo="m"))  # `igual` sin valor


# ------------------------------------------------------------------ valores del runtime
def test_velocidad_sale_de_debounce_delay_ms() -> None:
    o = POR_CLAVE["debounce_mode"]
    assert [v.valor for v in o.valores] == list(DEBOUNCE_DELAY_MS)
    assert "corto" in {v.valor for v in o.valores}
    notas = {v.valor: v.nota for v in o.valores}
    for modo, ms in DEBOUNCE_DELAY_MS.items():
        if ms:
            assert notas[modo] == f"espera {ms // 1000} s"
    assert notas["inmediato"] == "responde sin esperar"


def test_estilo_de_envio_sale_de_caps() -> None:
    o = POR_CLAVE["split_style"]
    assert [v.valor for v in o.valores] == list(CAPS)
    assert {v.valor: v.nota for v in o.valores}["varios_cortos"] == f"hasta {CAPS['varios_cortos']} por respuesta"


def test_tono_y_emojis_salen_del_contrato_de_persona() -> None:
    assert [v.valor for v in POR_CLAVE["persona_tone"].valores] == list(get_args(Persona.model_fields["tone"].annotation))
    assert [v.valor for v in POR_CLAVE["persona_emojis"].valores] == list(get_args(Persona.model_fields["emojis"].annotation))


def test_los_rangos_de_whatsapp_web_son_los_de_la_politica() -> None:
    for clave, limites, defecto in (
        ("waweb_send_min_delay_ms", policy._MIN_DELAY_BOUNDS, policy.DEFAULT_MIN_DELAY_MS),
        ("waweb_send_max_delay_ms", policy._MAX_DELAY_BOUNDS, policy.DEFAULT_MAX_DELAY_MS),
        ("waweb_daily_outbound_cap", policy._CAP_BOUNDS, policy.DEFAULT_DAILY_OUTBOUND_CAP),
    ):
        o = POR_CLAVE[clave]
        assert (o.minimo, o.maximo, o.por_defecto) == (*limites, defecto), clave


def test_los_rangos_coinciden_con_los_que_valida_la_api() -> None:
    """`IdentityUpdate` responde 422 fuera de rango: el registro tiene que anunciar los mismos límites."""
    from core.api.routers.settings import IdentityUpdate

    comprobadas = 0
    for clave, campo in IdentityUpdate.model_fields.items():
        if clave not in POR_CLAVE or POR_CLAVE[clave].tipo != "entero":
            continue
        limites = {type(m).__name__: m for m in campo.metadata}
        lo = getattr(limites.get("Ge"), "ge", None)
        hi = getattr(limites.get("Le"), "le", None)
        assert (POR_CLAVE[clave].minimo, POR_CLAVE[clave].maximo) == (lo, hi), clave
        comprobadas += 1
    assert comprobadas >= 7


# ------------------------------------------------------------------ rol y negocio
def test_el_duenio_no_ve_las_waweb_y_el_master_si() -> None:
    dueno = catalogo_para(es_master=False, guardrails_disponibles=[])
    master = catalogo_para(es_master=True, guardrails_disponibles=[])
    assert not [o for o in dueno.opciones if o.clave.startswith("waweb_")]
    assert len(dueno.opciones) == 26
    assert len(master.opciones) == 31
    assert {o.clave for o in master.opciones if o.clave.startswith("waweb_")} == {
        o.clave for o in OPCIONES if o.grupo == "whatsapp_web"
    }
    assert all(o.editable_por == "dueno" for o in dueno.opciones)


def test_los_guardrails_apagables_son_los_del_negocio_mas_los_interruptores() -> None:
    sin_pack = catalogo_para(es_master=False, guardrails_disponibles=sorted(TOGGLES))
    con_pack = catalogo_para(es_master=False, guardrails_disponibles=sorted({*TOGGLES, "crm_amount_check"}))

    def valores(c) -> list[str]:
        return [v.valor for o in c.opciones if o.clave == "disabled_guardrails" for v in o.valores]

    assert valores(sin_pack) == sorted(TOGGLES)
    assert "crm_amount_check" in valores(con_pack)
    assert sin_pack.version != con_pack.version
    # El registro estático no se contamina con la lista de un negocio.
    assert POR_CLAVE["disabled_guardrails"].valores == []


def test_la_version_es_estable_para_el_mismo_contenido() -> None:
    a = catalogo_para(es_master=False, guardrails_disponibles=["b", "a"])
    b = catalogo_para(es_master=False, guardrails_disponibles=["a", "b", "a"])
    assert a.version == b.version
    assert a.version != catalogo_para(es_master=True, guardrails_disponibles=["a", "b"]).version


# ------------------------------------------------------------------ sin prompts (invariante 3)
def _importa_el_registro(arbol: ast.AST) -> bool:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            modulo = nodo.module or ""
            if modulo == "core.config_registro" or modulo.startswith("core.config_registro."):
                return True
            if modulo == "core" and any(a.name == "config_registro" for a in nodo.names):
                return True
        elif isinstance(nodo, ast.Import):
            if any(a.name == "core.config_registro" or a.name.startswith("core.config_registro.") for a in nodo.names):
                return True
    return False


def test_solo_el_router_de_configuracion_importa_el_registro() -> None:
    """Las etiquetas y explicaciones son texto para personas: nunca llegan al contexto del LLM.

    Lista blanca: en `core/`, `packages/` y `plugins/` solo el router de configuración (y el propio
    paquete) pueden importar el registro.
    """
    permitidos = {"core/api/routers/settings.py"}
    ofensores: list[str] = []
    for carpeta in ("core", "packages", "plugins"):
        for ruta in (RAIZ / carpeta).rglob("*.py"):
            rel = ruta.relative_to(RAIZ).as_posix()
            if rel.startswith("core/config_registro/") or rel in permitidos:
                continue
            if _importa_el_registro(ast.parse(ruta.read_text())):
                ofensores.append(rel)
    assert not ofensores, f"importan el registro de opciones: {ofensores}"


def test_cargar_el_runtime_de_turnos_no_trae_el_registro() -> None:
    """Cubre lo transitivo: ni el loop, ni el consumer, ni el sender, ni el ingress cargan el registro."""
    codigo = (
        "import sys\n"
        "import core.orchestrator.loop, core.orchestrator.consumer, core.gateway.sender, core.kernel.ingress\n"
        "print('core.config_registro' in sys.modules)\n"
    )
    salida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ, check=True)
    assert salida.stdout.strip().splitlines()[-1] == "False"


# ------------------------------------------------------------------ consistencia con ast
# Módulos que LEEN las dos configuraciones y los receptores que usan para hacerlo.
LECTORES: dict[str, set[str]] = {
    "core/orchestrator/consumer.py": {"behavior"},
    "core/orchestrator/loop.py": {"behavior"},
    "core/gateway/sender.py": set(),
    "core/kernel/ingress.py": {"escalation_cfg"},
    "core/waweb/policy.py": {"b"},
}
RECORTADORES = {"_clamped_int", "_clamped_bool", "leer_entero"}


def _claves_leidas(ruta: str, receptores: set[str]) -> dict[str, set[str]]:
    """Claves literales leídas de `behavior_config` y `escalation_config` en un módulo."""
    fuente = (RAIZ / ruta).read_text()
    leidas: dict[str, set[str]] = {"behavior_config": set(), "escalation_config": set()}
    for nodo in ast.walk(ast.parse(fuente)):
        if not isinstance(nodo, ast.Call):
            continue
        clave = None
        config = "behavior_config"
        if isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "get" and nodo.args:
            texto = ast.unparse(nodo.func.value)
            es_escalamiento = "escalation_config" in texto or texto in {"escalation_cfg"}
            es_comportamiento = (
                nodo.func.value.id in receptores if isinstance(nodo.func.value, ast.Name) else False
            ) or "_behavior_config" in texto or "behavior_config" in texto
            if (es_escalamiento or es_comportamiento) and isinstance(nodo.args[0], ast.Constant):
                clave = nodo.args[0].value
                config = "escalation_config" if es_escalamiento else "behavior_config"
        elif isinstance(nodo.func, ast.Name) and nodo.func.id in RECORTADORES:
            literal = next((a.value for a in nodo.args[:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)), None)
            clave = literal
            # `leer_entero(<config>, "clave", defecto)`: la configuración se deduce del primer argumento.
            if nodo.func.id == "leer_entero" and nodo.args and "escalation" in ast.unparse(nodo.args[0]):
                config = "escalation_config"
        if isinstance(clave, str):
            leidas[config].add(clave)
    return leidas


def _registradas() -> dict[str, set[str]]:
    """Claves de almacenamiento (primer nivel) que el registro declara por configuración."""
    registradas: dict[str, set[str]] = {"behavior_config": set(), "escalation_config": set()}
    for o in OPCIONES:
        prefijo, _, resto = o.almacen.partition(":")
        if prefijo in registradas:
            registradas[prefijo].add(resto.split(".", 1)[0])
    return registradas


# Módulos que mencionan las dos configuraciones pero no leen claves literales con `.get`: escriben,
# definen el modelo o pasan el diccionario entero. Un módulo nuevo que las nombre tiene que
# añadirse a `LECTORES` o aquí, a propósito.
NO_LECTORES = {
    "core/admin/settings_service.py", "core/admin/views.py", "core/agents/spec.py", "core/alta_negocio.py",
    "core/api/routers/settings.py", "core/db/models.py", "core/llm/difficulty_router.py", "core/mcp/host.py",
    "core/orchestrator/guardrails.py", "core/orchestrator/verificador/verificador.py", "core/presets/apply.py",
}  # fmt: skip


def test_ningun_modulo_nuevo_lee_las_configuraciones_sin_declararse() -> None:
    conocidos = set(LECTORES) | NO_LECTORES
    nuevos = []
    for ruta in (RAIZ / "core").rglob("*.py"):
        rel = ruta.relative_to(RAIZ).as_posix()
        if rel.startswith("core/config_registro/") or rel in conocidos:
            continue
        texto = ruta.read_text()
        if "behavior_config" in texto or "escalation_config" in texto:
            nuevos.append(rel)
    assert not nuevos, f"módulos que nombran behavior_config/escalation_config sin declararse: {nuevos}"


def test_toda_clave_leida_esta_registrada_y_toda_registrada_tiene_lector() -> None:
    leidas: dict[str, dict[str, set[str]]] = {"behavior_config": {}, "escalation_config": {}}
    for ruta, receptores in LECTORES.items():
        for config, claves in _claves_leidas(ruta, receptores).items():
            for clave in claves:
                leidas[config].setdefault(clave, set()).add(ruta)

    registradas = _registradas()
    for config in ("behavior_config", "escalation_config"):
        sin_registrar = {c: sorted(r) for c, r in leidas[config].items() if c not in registradas[config]}
        assert not sin_registrar, f"{config}: claves leídas y no registradas {sin_registrar}"
        sin_lector = sorted(registradas[config] - set(leidas[config]))
        assert not sin_lector, f"{config}: claves registradas sin lector {sin_lector}"


def test_la_consistencia_detecta_una_clave_nueva_sin_registrar(tmp_path, monkeypatch) -> None:
    """El detector funciona: una lectura literal nueva aparece en `_claves_leidas`."""
    modulo = tmp_path / "lector.py"
    modulo.write_text('def f(behavior):\n    return behavior.get("clave_nueva_sin_registro", 1)\n')
    monkeypatch.setattr(sys.modules[__name__], "RAIZ", tmp_path)
    assert "clave_nueva_sin_registro" in _claves_leidas("lector.py", {"behavior"})["behavior_config"]
