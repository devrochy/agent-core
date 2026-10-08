"""HU-FWK-03 / ADR-0110: manifiesto de presets, fragmentos por pack y plan."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import core.presets as presets_mod
from core.presets import PresetError, PresetManifest, list_presets, load_fragment, load_preset
from core.presets.apply import _validar_plan


def test_catalogo_inicial_comercio_restaurante_y_cobranza() -> None:
    """Gherkin «Catálogo inicial»."""
    assert {p.id for p in list_presets()} >= {"comercio", "restaurante", "cobranza"}
    restaurante = load_preset("restaurante")
    assert restaurante.packs == ["pack_commerce", "pack_payments"]
    assert restaurante.plan_minimo == "basic"


def test_manifiesto_rechaza_campos_desconocidos_como_un_prompt() -> None:
    base = json.loads((presets_mod.PRESETS_DIR / "comercio.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        PresetManifest.model_validate({**base, "prompt": "Eres un vendedor agresivo"})
    with pytest.raises(ValidationError):
        PresetManifest.model_validate({**base, "persona": {"tone": "cercano", "instrucciones": "x"}})


def test_preset_inexistente_o_id_inseguro() -> None:
    with pytest.raises(PresetError):
        load_preset("no-existe")
    with pytest.raises(PresetError):
        load_preset("../../etc/passwd")


def _pack_falso(tmp_path: Path, monkeypatch, fragmento: dict) -> None:
    carpeta = tmp_path / "pack-falso"
    (carpeta / "presets").mkdir(parents=True)
    (carpeta / "presets" / "comercio.json").write_text(json.dumps(fragmento), encoding="utf-8")
    monkeypatch.setattr(presets_mod, "_pack_dir", lambda pack_id: carpeta)


def test_fragmento_no_puede_traer_secretos(tmp_path: Path, monkeypatch) -> None:
    from pack_payments.config import PaymentsConfig

    _pack_falso(tmp_path, monkeypatch, {"config": {"private_key": "prv_test_x"}})
    with pytest.raises(PresetError, match="secretos"):
        load_fragment("pack_payments", "comercio", PaymentsConfig)


def test_fragmento_rechaza_claves_desconocidas(tmp_path: Path, monkeypatch) -> None:
    from pack_commerce.config import CRMConfig

    _pack_falso(tmp_path, monkeypatch, {"config": {"no_existe": 1}})
    with pytest.raises(PresetError, match="desconocidas"):
        load_fragment("pack_commerce", "comercio", CRMConfig)


def test_sin_fragmento_se_usan_los_defaults_del_pack() -> None:
    fragmento = load_fragment("pack_payments", "restaurante")
    assert fragmento.config == {} and fragmento.seed == {}


def test_fragmentos_reales_de_commerce_son_validos() -> None:
    from pack_commerce.config import CRMConfig

    for vertical in ("comercio", "restaurante", "suscripciones"):
        fragmento = load_fragment("pack_commerce", vertical, CRMConfig)
        CRMConfig.model_validate(fragmento.config)
        assert fragmento.seed.get("products")


def _plan(pid: str, orden: int, plugins: int | None) -> SimpleNamespace:
    return SimpleNamespace(id=pid, sort_order=orden, limits={"plugins": plugins})


def test_plan_insuficiente_se_rechaza() -> None:
    manifest = load_preset("cobranza")  # premium
    modulos = {"pack_cobranza": SimpleNamespace(min_plan="premium")}
    with pytest.raises(PresetError, match="plan más alto"):
        _validar_plan(manifest, _plan("basic", 1, 3), modulos)
    _validar_plan(manifest, _plan("premium", 2, 8), modulos)


def test_limite_de_habilidades_del_plan() -> None:
    manifest = load_preset("restaurante")  # 2 packs
    modulos = {p: SimpleNamespace(min_plan="basic") for p in manifest.packs}
    with pytest.raises(PresetError, match="habilidades"):
        _validar_plan(manifest, _plan("basic", 1, 1), modulos)


def test_el_cli_de_tenant_no_importa_packs() -> None:
    """DoD de HU-FWK-03: `cli/commands/tenant.py` sin imports de packs (invariante 5)."""
    arbol = ast.parse(Path("cli/commands/tenant.py").read_text(encoding="utf-8"))
    modulos = [
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(arbol)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    ]
    assert not [m for m in modulos if m and m.split(".")[0].startswith("pack_")]


def test_preset_suscripciones_fija_su_proveedor_y_agenda_interna() -> None:
    """HU-SH-01: preset genérico de planes recurrentes (base del asistente de SolversHub)."""
    from pack_calendar.config import CalendarConfig

    preset = load_preset("suscripciones")
    assert set(preset.packs) == {"pack_commerce", "pack_calendar", "pack_payments", "pack_subscriptions"}
    assert preset.proveedor_suscripciones == "pack_subscriptions"
    assert preset.proveedor_suscripciones in preset.packs
    assert preset.plan_minimo == "premium"  # 4 habilidades: basic permite 3
    agenda = load_fragment("pack_calendar", "suscripciones", CalendarConfig)
    assert CalendarConfig.model_validate(agenda.config).source == "interno"


def test_seed_de_suscripciones_respeta_d16() -> None:
    """Solo el plan de $0 trae prueba en los datos de ejemplo (D-16)."""
    from pack_commerce.config import CRMConfig

    fragmento = load_fragment("pack_commerce", "suscripciones", CRMConfig)
    for producto in fragmento.seed["products"]:
        for variante in producto["variants"]:
            prueba = variante["attributes"].get("trial_dias", 0)
            assert prueba == 0 or variante["price"] == 0, variante["sku"]
