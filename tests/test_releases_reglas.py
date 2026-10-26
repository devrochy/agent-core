"""HU-AOS-14 · Reglas puras de las releases por conexión (ADR-0117 §2 y §4).

Sin base de datos: hash del manifiesto, asignación sticky por episodio, revalidación de tareas,
efecto irreversible, la ranura de la release del turno y lo que leen de ella los playbooks, el
flujo y el prompt. Lo que necesita Postgres está en `tests/integration/test_releases_por_conexion.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from core.kernel.release_del_turno import (
    con_release,
    decidir_destino,
    fijar_release_del_turno,
    plan_revalidacion,
    tuvo_efecto_irreversible,
)
from core.kernel.state import Proyeccion
from core.releases import manifiesto as man
from core.shared import release_vigente

R1, R2, R3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
T = uuid.uuid4()


def _manifiesto(**cambios: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "componentes": [
            man.Componente("i2", "a2", "general", "specialist", "v2", 1, "h2", "agentspec/3"),
            man.Componente("i1", "a1", "coordinador", "core", "v1", 1, "h1", "kernelspec/1"),
        ],
        "habilidades": [
            man.Habilidad("pack_b", "1.0.0", "x"),
            man.Habilidad("pack_a", "2.0.0", "y"),
        ],
        "plugins_del_negocio": ["mi_plugin"],
        "overrides": {"tono": "Hablá cercano."},
        "flujo_del_negocio": {"sections": [{"id": "propia", "tools": ["x"]}]},
        "app_settings": {
            "assistant_name": "Ana",
            "business_description": "",
            "language": "es",
            "brand_voice": None,
        },
        "built_with": {"git_sha": "abc"},
    }
    base.update(cambios)
    return man.armar(**base)


# ---------------------------------------------------------------- manifiesto
def test_manifiesto_ordenado_y_hash_sin_built_with() -> None:
    m = _manifiesto()
    assert [c["role"] for c in m["components"]] == ["core", "specialist"]
    assert [s["pack_id"] for s in m["skills"]] == ["pack_a", "pack_b"]
    assert m["schema"] == "release/2"
    # Un deploy (otro git_sha) no cambia el hash; un texto del negocio, sí.
    assert man.hash_de(m) == man.hash_de(_manifiesto(built_with={"git_sha": "otro"}))
    assert man.hash_de(m) != man.hash_de(_manifiesto(overrides={"tono": "Otro texto."}))
    assert man.hash_de(m) != man.hash_de(_manifiesto(app_settings={"assistant_name": "Beto"}))


def test_resumen_no_lleva_textos() -> None:
    resumen = man.resumen(_manifiesto())
    texto = str(resumen)
    assert "Hablá cercano." not in texto and "Ana" not in texto
    assert resumen["playbook_overrides"][0]["name"] == "tono"
    assert resumen["flow_tenant"]["sections"] == 1


# ---------------------------------------------------------------- asignación
def test_sin_release_toma_la_activa() -> None:
    assert decidir_destino(None, R1, None, episodio_nuevo=False, retirada=False) == (R1, "primera")


def test_sticky_por_episodio_y_rollback_a_mitad_de_episodio() -> None:
    # Se publicó la 2 y la conversación sigue en la 1 hasta el fin del episodio.
    assert decidir_destino(R1, R2, None, episodio_nuevo=False, retirada=False) == (R1, None)
    assert decidir_destino(R1, R2, None, episodio_nuevo=True, retirada=False) == (
        R2,
        "episodio_nuevo",
    )
    # Su release fue retirada por un rollback: cambia ya en este turno.
    assert decidir_destino(R2, R1, None, episodio_nuevo=False, retirada=True) == (R1, "retirada")
    # Dentro del episodio, la del canary se queda.
    assert decidir_destino(R3, R1, R3, episodio_nuevo=False, retirada=False) == (R3, None)
    # Al empezar un episodio se vuelve a sortear (HU-AOS-32): con el canary al 0 %, a la activa.
    assert decidir_destino(R3, R1, R3, episodio_nuevo=True, retirada=False) == (R1, "episodio_nuevo")


def test_canary_reparto_sticky_y_retirada() -> None:
    """HU-AOS-32: la cubeta es estable (sha256, no `hash()`), reparte cerca del porcentaje, cambia
    con otro canary, y una release retirada a mitad de episodio va a la activa sin sortear."""
    import uuid

    from core.kernel.release_del_turno import LANE_CANARY, LANE_ESTABLE, carril, cubeta

    convs = [uuid.UUID(int=i) for i in range(10_000)]
    assert all(cubeta(R3, c) == cubeta(R3, c) for c in convs[:50])
    al_canary = sum(1 for c in convs if decidir_destino(None, R1, R3, episodio_nuevo=False, retirada=False,
                                                       canary_percent=10, conversation_id=c)[0] == R3)
    assert 900 <= al_canary <= 1100, al_canary
    otro = uuid.uuid4()
    assert sum(1 for c in convs[:2000] if (cubeta(R3, c) < 10) != (cubeta(otro, c) < 10)) > 100
    # Dentro del episodio no se sortea: sigue en la activa aunque su cubeta sea del canary.
    c = next(c for c in convs if cubeta(R3, c) < 10)
    assert decidir_destino(R1, R1, R3, episodio_nuevo=False, retirada=False, canary_percent=10, conversation_id=c) == (R1, None)
    assert decidir_destino(R1, R1, R3, episodio_nuevo=True, retirada=False, canary_percent=10, conversation_id=c) == (R3, "episodio_nuevo")
    # Retirada a mitad de episodio: a la activa, sin sorteo.
    assert decidir_destino(R2, R1, R3, episodio_nuevo=False, retirada=True, canary_percent=50, conversation_id=c) == (R1, "retirada")
    assert carril(R3, R1, R3) == LANE_CANARY and carril(R1, R1, R3) == LANE_ESTABLE and carril(R1, R1, None) == LANE_ESTABLE


# ---------------------------------------------------------------- revalidación
def _tarea(stage: str = "pago", slots: dict[str, Any] | None = None) -> Proyeccion:
    ahora = datetime.now(UTC)
    return Proyeccion(
        procedure_id="pack_commerce.compra",
        procedure_version="1.0.0",
        status="active",
        stage=stage,
        stack_position=0,
        last_seq=3,
        created_at=ahora,
        updated_at=ahora,
        slots={k: {"value": v} for k, v in (slots or {}).items()},
    )


def _procedimiento(slots: dict[str, Any], etapas: list[str], version: str = "1.1.0") -> Any:
    from agentcore_contracts import Procedure

    return Procedure.model_validate(
        {
            "id": "pack_commerce.compra",
            "version": version,
            "title": "Compra",
            "slots": slots,
            "stages": [{"id": e} for e in etapas],
        }
    )


def test_revalidacion_conserva_datos_declarados_y_recalcula_etapa() -> None:
    tarea = _tarea(stage="pago", slots={"producto": "x", "color": "rojo"})
    nuevo = _procedimiento({"producto": {"type": "string"}}, ["elegir", "confirmar"])
    datos = plan_revalidacion(tarea, nuevo, desde=R2, hacia=R1)
    assert datos == {
        "from_version": "1.0.0",
        "to_version": "1.1.0",
        "to_stage": "elegir",
        "dropped_slots": ["color"],
        "from_release_id": str(R2),
        "to_release_id": str(R1),
    }
    # Un procedimiento abierto no declara datos: se conservan todos y la etapa vigente si existe.
    abierto = _procedimiento({}, ["pago"], version="0.0.0")
    datos = plan_revalidacion(tarea, abierto, desde=R2, hacia=R1)
    assert datos["dropped_slots"] == [] and datos["to_stage"] == "pago"


@dataclass
class _Ev:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Info:
    side_effect: str | None


def test_efecto_irreversible_por_catalogo_o_desconocido() -> None:
    catalogo = {"leer": _Info("read"), "pagar": _Info("irreversible"), "nuevo": _Info(None)}
    assert not tuvo_efecto_irreversible([_Ev("action_succeeded", {"action": "leer"})], catalogo)
    assert not tuvo_efecto_irreversible([_Ev("action_requested", {"action": "pagar"})], catalogo)
    assert tuvo_efecto_irreversible([_Ev("action_succeeded", {"action": "pagar"})], catalogo)
    # Sin efecto conocido (hoy todas las acciones: HU-AOS-34) se trata como irreversible.
    assert tuvo_efecto_irreversible([_Ev("action_succeeded", {"action": "nuevo"})], catalogo)
    assert tuvo_efecto_irreversible(
        [_Ev("action_succeeded", {"action": "fuera_del_catalogo"})], catalogo
    )


# ---------------------------------------------------------------- ranura del turno
def _artefactos(tenant_id: uuid.UUID = T) -> release_vigente.ArtefactosDeRelease:
    return release_vigente.ArtefactosDeRelease(
        tenant_id=tenant_id,
        connection_id=uuid.uuid4(),
        release_id=R1,
        lane="stable",
        idle_minutes=60,
        app_settings={"assistant_name": "Congelada", "brand_voice": None, "otro": "no entra"},
        playbooks={"tono": "---\nname: tono\n---\nTexto congelado"},
        flow_tenant={
            "sections": [{"id": "propia", "label": "Propia", "tools": ["buscar"], "pack": "tenant"}]
        },
    )


async def _en_scope(tenant_id: uuid.UUID, fn: Any) -> Any:
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        return await fn()


def test_la_ranura_vive_solo_en_el_turno_y_solo_para_su_negocio() -> None:
    assert not release_vigente.fijar(_artefactos())  # fuera de un turno no hay ranura

    @con_release
    async def turno() -> tuple[Any, Any, int]:
        release_vigente.fijar(_artefactos())
        propia = release_vigente.vigente()
        ajena = await _en_scope(uuid.uuid4(), lambda: _devolver(release_vigente.vigente()))
        return propia, ajena, release_vigente.idle_minutes(720)

    propia, ajena, idle = asyncio.run(_en_scope(T, turno))
    assert propia is not None and propia.release_id == R1
    assert ajena is None
    assert idle == 60
    assert asyncio.run(_en_scope(T, lambda: _devolver(release_vigente.vigente()))) is None


async def _devolver(valor: Any) -> Any:
    return valor


def test_la_ranura_se_cierra_aunque_el_turno_falle() -> None:
    @con_release
    async def turno() -> None:
        release_vigente.fijar(_artefactos())
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(_en_scope(T, turno))
    assert asyncio.run(_en_scope(T, lambda: _devolver(release_vigente.vigente()))) is None


def test_prompt_con_los_campos_congelados() -> None:
    from core.llm.prompt_builder import PromptContext

    ctx = PromptContext(assistant_name="Caliente", business_description="desc", brand_voice="voz")
    assert release_vigente.con_valores_de_release(ctx) is ctx  # sin release: el mismo objeto

    @con_release
    async def turno() -> PromptContext:
        release_vigente.fijar(_artefactos())
        return release_vigente.con_valores_de_release(ctx)

    nuevo = asyncio.run(_en_scope(T, turno))
    assert (nuevo.assistant_name, nuevo.business_description, nuevo.brand_voice) == (
        "Congelada",
        "desc",
        None,
    )


def test_playbook_y_flujo_salen_de_la_release(tmp_path: Any, monkeypatch: Any) -> None:
    from agentcore_contracts import AgentSpec

    from core.agents import playbooks
    from core.agents.flow import resolve_flow
    from core.agents.spec import DEFAULT_SPEC

    spec = AgentSpec.model_validate(
        DEFAULT_SPEC
        | {
            "tenantId": str(T),
            "playbooks": [
                {"name": "tono", "description": ""},
                {"name": "identidad", "description": ""},
            ],
        }
    )

    @con_release
    async def turno() -> tuple[Any, Any, Any]:
        release_vigente.fijar(_artefactos())
        tono = await playbooks.load_playbook(spec, "tono", tenant_slug="no-se-usa")
        identidad = await playbooks.load_playbook(spec, "identidad", tenant_slug="no-se-usa")
        flujo = await resolve_flow(spec, slug="no-se-usa")
        return tono, identidad, flujo

    # Con release, el disco del negocio no se lee (ni el override ni el flow.json).
    monkeypatch.setattr(
        "core.onboarding.tenant_dir", lambda slug: (_ for _ in ()).throw(AssertionError(slug))
    )
    monkeypatch.setattr(
        "core.agents.flow.load_tenant_flow",
        lambda slug: (_ for _ in ()).throw(AssertionError(slug)),
    )
    tono, identidad, flujo = asyncio.run(_en_scope(T, turno))
    assert tono == "Texto congelado"
    assert identidad is not None  # sin override congelado: el default del core
    assert flujo is not None and any(s.id == "propia" for s in flujo.sections)


def test_fijar_no_consulta_nada_con_el_ruteo_por_conexion_apagado(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", False)

    def _sin_base() -> Any:
        raise AssertionError("no debe abrir sesión")

    monkeypatch.setattr("core.db.session.get_session", _sin_base)

    @dataclass
    class _Conv:
        id: uuid.UUID
        tenant_id: uuid.UUID
        connection_id: uuid.UUID

    assert (
        asyncio.run(
            _en_scope(T, lambda: fijar_release_del_turno(_Conv(uuid.uuid4(), T, uuid.uuid4())))
        )
        is None
    )
