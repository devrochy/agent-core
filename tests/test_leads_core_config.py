"""HU-AOS-70 · Config de Captar leads y frontera de los packs (ADR-0193).

Sin base: la config guarda los secretos para cifrarlos enteros y nunca los expone al serializar; lo desconocido se
ignora; los catálogos del core coinciden con los CHECK de la migración 0146; ningún pack importa los modelos de leads
del core (invariante 5 y ADR-0193 §4: los packs pasan por `core.leads`).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from core.leads import catalogo
from core.leads.config import ConfigCaptarLeads, a_guardar, desde_guardado

RAIZ = Path(__file__).resolve().parents[1]
MIGRACION = RAIZ / "alembic" / "versions" / "0146_leads_en_el_core.py"
MODELOS_DE_LEADS = {"Lead", "LeadStageChange", "LeadFollowup", "LeadSettings"}


def test_los_secretos_se_guardan_para_cifrar_y_no_se_muestran():
    config = ConfigCaptarLeads(lead_intake_secret="s3cr3t", lead_intake_turnstile="0xLLAVE")
    guardado = a_guardar(config)
    assert guardado["lead_intake_secret"] == "s3cr3t"
    assert guardado["lead_intake_turnstile"] == "0xLLAVE"
    assert "s3cr3t" not in repr(config) and "0xLLAVE" not in config.model_dump_json()
    assert desde_guardado(guardado).lead_intake_secret.get_secret_value() == "s3cr3t"


def test_lo_que_ya_no_es_de_captar_leads_se_ignora():
    # Viene de la config de commerce: `auto_create_lead` lo reemplaza asegurar el lead, el origen es catálogo cerrado.
    config = desde_guardado(
        {"auto_create_lead": False, "lead_intake_source": "facebook", "pipeline_name": "X", "follow_up_hours": 6}
    )
    assert config.follow_up_hours == 6
    assert not hasattr(config, "auto_create_lead") and "lead_intake_source" not in a_guardar(config)


def _tupla_sql(nombre: str) -> tuple[str, ...]:
    texto = MIGRACION.read_text(encoding="utf-8")
    m = re.search(rf'^{nombre} = "\(([^)]*)\)"', texto, re.M)
    assert m, nombre
    return tuple(v.strip().strip("'") for v in m.group(1).split(","))


def test_los_catalogos_coinciden_con_la_migracion():
    assert _tupla_sql("_ETAPAS") == catalogo.ETAPAS
    assert _tupla_sql("_ORIGENES") == catalogo.ORIGENES
    assert _tupla_sql("_MOTIVOS") == catalogo.CODIGOS_DE_MOTIVO


def test_ningun_pack_importa_los_modelos_de_leads_del_core():
    infractores: list[str] = []
    for archivo in (RAIZ / "packages").glob("pack-*/**/*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and nodo.module == "core.db.models":
                usados = {a.name for a in nodo.names} & MODELOS_DE_LEADS
                if usados:
                    infractores.append(f"{archivo.relative_to(RAIZ)}: {sorted(usados)}")
    assert not infractores, "Los packs usan core.leads, no los modelos: " + "; ".join(infractores)
