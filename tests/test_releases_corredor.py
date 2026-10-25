"""HU-AOS-21 parte 3b · piezas puras del corredor del gate (sin base ni LLM)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core.releases import corredor, escenarios


def _gate():
    evals = str(Path(__file__).resolve().parents[1] / "evals")
    if evals not in sys.path:
        sys.path.insert(0, evals)
    import gate_release

    return gate_release


# ------------------------------------------------------------------ entorno del subproceso
BASE = {
    "PATH": "/usr/bin",
    "APP_ENV": "development",
    "LLM_API_KEY": "k",
    "DATABASE_URL": "postgresql+asyncpg://app:x@negocio/agent_core",
    "WOMPI_PRIVATE_KEY": "secreto",
    "META_APP_SECRET": "secreto",
    "WHATSAPP_APP_SECRET": "secreto",
    "SMTP_PASSWORD": "secreto",
    "FCM_SERVICE_ACCOUNT": "secreto",
    "REDIS_URL": "redis://negocio:6379/0",
    "EVAL_GATE_EJECUTOR": "m:f",
}


def _entorno(base: dict[str, str]) -> dict[str, str]:
    return corredor.entorno_del_subproceso(
        base,
        url_eval="postgresql+asyncpg://app:x@h/agentcore_eval",
        url_eval_dueno="postgresql+asyncpg://dueno:x@h/agentcore_eval",
        redis_url="redis://:pw@h:6379/0",
        redis_db=15,
        tenants_root="/tmp/t",
    )


def test_el_entorno_no_lleva_secretos_de_canales_ni_cobro() -> None:
    e = _entorno(BASE)
    for k in ("WOMPI_PRIVATE_KEY", "META_APP_SECRET", "WHATSAPP_APP_SECRET", "SMTP_PASSWORD", "FCM_SERVICE_ACCOUNT"):
        assert k not in e
    assert e["LLM_API_KEY"] == "k"


def test_el_entorno_apunta_a_la_base_de_eval_y_a_otro_indice_de_redis() -> None:
    e = _entorno(BASE)
    assert e["DATABASE_URL"].endswith("/agentcore_eval")
    assert e["MIGRATIONS_DATABASE_URL"].endswith("/agentcore_eval")
    assert e["REDIS_URL"] == "redis://:pw@h:6379/15"
    assert e["AOS_CONNECTION_ROUTING"] == "true"


def test_el_entorno_no_lleva_las_claves_de_la_plataforma() -> None:
    base = {**BASE, "APP_SECRET_KEY": "real", "MASTER_ENCRYPTION_KEY": "real", "TOKEN_MASTER_KEYS": "{real}", "GROQ_API_KEY": "g"}
    e = _entorno(base)
    assert e["APP_SECRET_KEY"] != "real" and len(e["APP_SECRET_KEY"]) == 64
    assert e["APP_SECRET_KEY"] != _entorno(base)["APP_SECRET_KEY"]  # desechable por corrida
    assert "MASTER_ENCRYPTION_KEY" not in e and "GROQ_API_KEY" not in e
    assert e["TOKEN_MASTER_KEYS"] != "{real}" and e["TOKEN_ACTIVE_KEY_ID"] == "gate"


def test_el_indice_de_redis_de_eval_no_puede_ser_el_del_negocio() -> None:
    with pytest.raises(corredor.MedicionInvalida):
        corredor.entorno_del_subproceso(
            BASE,
            url_eval="postgresql+asyncpg://a:x@h/agentcore_eval",
            url_eval_dueno="postgresql+asyncpg://d:x@h/agentcore_eval",
            redis_url="redis://h:6379/15",
            redis_db=15,
            tenants_root="/tmp/t",
        )


def test_el_ejecutor_de_prueba_solo_viaja_en_entornos_de_prueba() -> None:
    assert "EVAL_GATE_EJECUTOR" in _entorno(BASE)
    assert "EVAL_GATE_EJECUTOR" not in _entorno({**BASE, "APP_ENV": "production"})


# ------------------------------------------------------------------ validar_salida
PEDIDOS = {("procedimientos", "a"): ["p1"], ("procedimientos", "b"): ["p1", "p2"]}


def _salida(**cambios):
    intento = {"ok": True, "detalle": ""}
    s = {
        "ok": True,
        "resultados": [
            {"set": "procedimientos", "nombre": "a", "intentos": [intento] * 4},
            {"set": "procedimientos", "nombre": "b", "intentos": [intento] * 3 + [{"ok": False, "detalle": "x"}]},
        ],
        "costo_usd": 0.5,
        "input_tokens": 10,
        "output_tokens": 5,
        "modelo": "m",
    }
    s.update(cambios)
    return s


def test_una_salida_valida_reparte_los_escenarios_por_procedimiento() -> None:
    med, costo, modelo, tin, tout = corredor.validar_salida(_salida(), PEDIDOS, presupuesto_usd=2.0)
    assert (costo, modelo, tin, tout) == (0.5, "m", 10, 5)
    assert len(med["p1"].escenarios) == 2 and len(med["p2"].escenarios) == 1
    fallido = [e for e in med["p2"].escenarios if not e.ok]
    assert fallido and fallido[0].pass_k == 0.0 and fallido[0].pass_1 == 0.75


@pytest.mark.parametrize(
    "cambio",
    [
        {"ok": False},
        {"resultados": "no"},
        {"costo_usd": -1},
        {"costo_usd": float("nan")},
        {"costo_usd": True},
        {"costo_usd": 999},
        {"input_tokens": -1},
        {"output_tokens": 1.5},
        {"modelo": ""},
        {"modelo": 7},
        {"resultados": []},
    ],
)
def test_una_salida_mal_formada_se_descarta(cambio) -> None:
    with pytest.raises(corredor.MedicionInvalida):
        corredor.validar_salida(_salida(**cambio), PEDIDOS, presupuesto_usd=2.0)


def test_una_salida_con_escenarios_de_mas_repetidos_o_con_intentos_mal_se_descarta() -> None:
    base = _salida()
    extra = {"set": "procedimientos", "nombre": "zzz", "intentos": [{"ok": True}] * 4}
    for resultados in (
        [*base["resultados"], extra],
        [*base["resultados"], base["resultados"][0]],
        [{**base["resultados"][0], "intentos": [{"ok": True}] * 3}, base["resultados"][1]],
        [{**base["resultados"][0], "intentos": [{"ok": "si"}] * 4}, base["resultados"][1]],
    ):
        with pytest.raises(corredor.MedicionInvalida):
            corredor.validar_salida(_salida(resultados=resultados), PEDIDOS, presupuesto_usd=2.0)


# ------------------------------------------------------------------ escenarios
def _set(raiz: Path) -> None:
    carpeta = raiz / "procedimientos"
    carpeta.mkdir(parents=True)
    (carpeta / "negocio.json").write_text('{"x": 1}')
    (carpeta / "01-a.json").write_text(json.dumps({"procedimientos": ["p1"], "pasos": []}))
    (carpeta / "02-b.json").write_text(json.dumps({"procedimientos": ["p1", "p2"], "pasos": []}))
    (carpeta / "03-sin.json").write_text(json.dumps({"pasos": []}))


def test_los_escenarios_se_eligen_por_procedimiento_y_se_informan_los_que_no_tienen(tmp_path: Path) -> None:
    _set(tmp_path)
    con, sin = escenarios.elegir(["p1", "p2", "p3"], tmp_path)
    assert sin == ["p3"]
    assert [e.nombre for e in con["p1"]] == ["01-a", "02-b"]
    assert [e.nombre for e in con["p2"]] == ["02-b"]
    # Un escenario que ejercita dos procedimientos corre una sola vez.
    assert escenarios.por_set(con) == {"procedimientos": {"01-a": ["p1"], "02-b": ["p1", "p2"]}}


def test_la_huella_cambia_si_cambia_un_escenario_o_el_negocio(tmp_path: Path) -> None:
    _set(tmp_path)
    con, _ = escenarios.elegir(["p1"], tmp_path)
    antes = escenarios.sha256_de(con["p1"])
    assert antes == escenarios.sha256_de(list(reversed(con["p1"])))
    (tmp_path / "procedimientos" / "negocio.json").write_text('{"x": 2}')
    assert escenarios.sha256_de(con["p1"]) != antes
    despues = escenarios.sha256_de(con["p1"])
    (tmp_path / "procedimientos" / "01-a.json").write_text(json.dumps({"procedimientos": ["p1"], "pasos": [1]}))
    assert escenarios.sha256_de(con["p1"]) != despues


def test_una_carpeta_sin_journeys_no_declara_nada(tmp_path: Path) -> None:
    assert escenarios.declarados(tmp_path / "no-existe") == {}


# ------------------------------------------------------------------ guardas del subproceso
def test_el_subproceso_rechaza_bases_que_no_son_de_eval() -> None:
    g = _gate()
    ok = "postgresql+asyncpg://u:x@h/agentcore_eval"
    g.exigir_urls_de_eval(ok, ok, "agent_core")
    for runtime, dueno, negocio in (
        ("postgresql+asyncpg://u:x@h/agent_core", ok, "agent_core"),
        (ok, "postgresql+asyncpg://u:x@h/postgres", "agent_core"),
        ("", ok, ""),
        (ok, ok, "agentcore_eval"),
    ):
        with pytest.raises(g.CorridaFallida):
            g.exigir_urls_de_eval(runtime, dueno, negocio)


# ------------------------------------------------------------------ grabaciones
def test_los_negocios_efimeros_del_gate_se_normalizan_en_las_grabaciones() -> None:
    from core.llm import cassette

    a = cassette._VOLATIL.sub("X", "negocio gate-eval-0123456789 y journeys-01234567")
    assert a == "negocio X y X"
