"""HU-CFG-03 (SPEC-37): una sola validación de la configuración.

Unitarias: el rango de cada opción numérica vive en `core/config_limites.py` y de ahí lo leen el
registro, la API, el panel SSR y el runtime; la lectura del runtime es tolerante; las listas de
valores se validan contra el registro; los presets rechazan claves desconocidas.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from core import config_limites
from core.config_limites import RANGOS, Rango, leer_entero
from core.config_registro import OPCIONES, POR_CLAVE
from core.config_registro.validar import validar_valor

RAIZ = Path(__file__).resolve().parent.parent


def test_toda_opcion_numerica_con_rango_sale_de_config_limites() -> None:
    for o in OPCIONES:
        # Las del Coordinador (`ambito="conexion"`) toman su rango del contrato `kernelspec/1`.
        if o.tipo == "entero" and o.ambito == "negocio":
            assert o.clave in RANGOS, f"{o.clave}: opción numérica sin rango en config_limites"
            assert (o.minimo, o.maximo) == tuple(RANGOS[o.clave]), o.clave
    assert set(RANGOS) <= {o.clave for o in OPCIONES}


def test_los_limites_no_estan_escritos_a_mano_en_el_loop_ni_en_la_api_ni_en_el_panel_ssr() -> None:
    """Gherkin «Rango único»: cambiar `RANGOS` basta; los tres sitios ya no repiten 50/5000, 1/20 ni 1/10."""
    viejos = re.compile(r"\b(?:5000|min\(20,|min\(10,|min\(5,)")
    for ruta in ("core/orchestrator/loop.py", "core/api/routers/settings.py", "core/admin/views.py"):
        for n, linea in enumerate((RAIZ / ruta).read_text().splitlines(), 1):
            if linea.lstrip().startswith("#"):
                continue
            assert not viejos.search(linea), f"{ruta}:{n} repite un rango a mano: {linea.strip()}"


def test_cambiar_un_rango_cambia_la_lectura_del_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"difficulty_score_threshold": 9}
    assert leer_entero(config, "difficulty_score_threshold", 2) == 9
    monkeypatch.setitem(config_limites.RANGOS, "difficulty_score_threshold", Rango(1, 8))
    assert leer_entero(config, "difficulty_score_threshold", 2) == 8


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [(999, 10), (-3, 1), ("7", 7), ("abc", 2), (None, 2), (3.9, 3), ([], 2)],
)
def test_lectura_tolerante_del_runtime(crudo, esperado) -> None:
    """Gherkin «Lectura tolerante»: una fila vieja o escrita por un script nunca tumba el turno."""
    assert leer_entero({"difficulty_score_threshold": crudo}, "difficulty_score_threshold", 2) == esperado
    assert leer_entero(None, "difficulty_score_threshold", 2) == 2
    assert leer_entero({}, "difficulty_score_threshold", 2) == 2


def test_validar_valor_de_una_lista_cerrada() -> None:
    assert validar_valor("debounce_mode", "corto") == "corto"
    with pytest.raises(ValueError, match="inmediato, corto, rapido, natural, pausado"):
        validar_valor("debounce_mode", "xyz")
    with pytest.raises(ValueError, match="un_mensaje"):
        validar_valor("split_style", "inventado")
    with pytest.raises(ValueError, match="formal, neutral, cercano, informal"):
        validar_valor("persona_tone", "divertido")
    with pytest.raises(ValueError, match="none, sparse, free"):
        validar_valor("persona_emojis", "muchos")


def test_validar_valor_de_un_numero_con_rango() -> None:
    assert validar_valor("difficulty_score_threshold", 10) == 10
    for malo in (0, 11, "x"):
        with pytest.raises(ValueError, match="Puntaje para razonamiento"):
            validar_valor("difficulty_score_threshold", malo)


def test_identity_update_rechaza_lo_que_el_registro_no_acepta() -> None:
    from core.api.routers.settings import IdentityUpdate

    base = {"assistant_name": "Ana"}
    IdentityUpdate(**base, debounce_mode="corto", split_style="pocos_cortos", persona_tone="formal")
    for campo, valor in (
        ("debounce_mode", "xyz"),
        ("split_style", "inventado"),
        ("persona_tone", "divertido"),
        ("persona_emojis", "muchos"),
        ("difficulty_score_threshold", 11),
        ("guardrail_max_retries", 6),
    ):
        with pytest.raises(ValidationError) as exc:
            IdentityUpdate(**base, **{campo: valor})
        assert campo in str(exc.value)
    # Vacío en la voz = no cambiar (compatibilidad con clientes que no la envían).
    IdentityUpdate(**base, persona_tone="", persona_emojis="")


def test_un_preset_con_clave_de_app_settings_desconocida_falla_nombrandola() -> None:
    from core.presets import PresetAppSettings

    PresetAppSettings(split_style="un_mensaje", currency="COP")
    with pytest.raises(ValidationError, match="debounce_mode"):
        PresetAppSettings(debounce_mode="rapido")  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="split_style"):
        PresetAppSettings(split_style="inventado")


def test_el_registro_y_la_politica_de_waweb_comparten_los_limites() -> None:
    from core.waweb import policy

    assert tuple(policy._MIN_DELAY_BOUNDS) == tuple(RANGOS["waweb_send_min_delay_ms"])
    assert POR_CLAVE["waweb_daily_outbound_cap"].maximo == RANGOS["waweb_daily_outbound_cap"].maximo
