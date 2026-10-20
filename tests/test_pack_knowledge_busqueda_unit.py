"""Búsqueda y tool del pack de conocimiento sin base (ADR-0145 §3-§5, HU-AOS-49 parte 3).

- La configuración sale del bloque `knowledge` congelado; sin bloque o inválido, los defaults.
- `buscar_conocimiento` se ofrece solo con agente v3 en foco que la declare y tenga documentos
  listos; usa el agente del `ToolContext`, nunca uno de los argumentos.
- `audit_log` guarda ids y puntajes, no el texto (`resultado_auditable`).
- Grabación de embeddings: replay sin red, record con OpenAI simulado, clave exacta por texto.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pack_knowledge import busqueda, skills
from pack_knowledge.busqueda import Config, Fragmento, Resultado

from core.llm import embeddings
from core.llm.cassette import GrabacionDesactualizada, GrabacionProhibida
from core.mcp.host import new_tool_context
from core.mcp.types import ToolContext
from core.orchestrator.loop import resultado_auditable
from core.shared.tenant import tenant_scope

TENANT = uuid.UUID("20000000-0000-0000-0000-000000000049")
AGENTE = uuid.uuid4()


def _ctx(**extra: Any) -> ToolContext:
    return new_tool_context(
        conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), idempotency_key="k", channel="whatsapp", **extra
    )


# ------------------------------------------------------------------ configuración
def test_config_del_agente() -> None:
    assert Config.del_agente(None) == Config("hibrida", 4, None)
    assert Config.del_agente({"mode": "semantica", "topK": 2, "minScore": 0.5}) == Config("semantica", 2, 0.5)
    # Un bloque que no valida (topK fuera de rango) no rompe la búsqueda: defaults.
    assert Config.del_agente({"topK": 50}) == Config("hibrida", 4, None)


def test_umbral_calibrado_por_modo_salvo_que_el_agente_lo_fije() -> None:
    assert Config("hibrida").umbral == busqueda.MIN_SCORE_POR_MODO["hibrida"] == 0.45
    assert Config("semantica").umbral == busqueda.MIN_SCORE_POR_MODO["semantica"] == 0.30
    assert Config("semantica", min_score=0.6).umbral == 0.6


def test_tool_context_trae_el_agente_solo_si_lo_inyecta_el_core() -> None:
    vacio = _ctx()
    assert (vacio.agent_id, vacio.agent_version_id, vacio.knowledge) == (None, None, None)
    version = uuid.uuid4()
    lleno = _ctx(agent_id=AGENTE, agent_version_id=version, knowledge={"topK": 2})
    assert (lleno.agent_id, lleno.agent_version_id, lleno.knowledge) == (AGENTE, version, {"topK": 2})


# ------------------------------------------------------------------ predicado
@pytest.fixture
def foco(monkeypatch: pytest.MonkeyPatch):
    def _con(tools: list[str] | None, listos: bool = True) -> None:
        agente = None if tools is None else SimpleNamespace(agent_id=AGENTE, tools=frozenset(tools))
        monkeypatch.setattr(skills.release_vigente, "agente_del_turno", lambda: agente)

        async def _hay(_aid: uuid.UUID) -> bool:
            return listos

        monkeypatch.setattr(busqueda, "hay_documentos_listos", _hay)

    return _con


@pytest.mark.parametrize(
    ("tools", "listos", "esperado"),
    [
        (None, True, False),  # runtime legacy o sin release
        (["crear_pedido"], True, False),  # el agente no la declara
        (["buscar_conocimiento"], False, False),  # sin documentos listos
        (["buscar_conocimiento"], True, True),
    ],
)
async def test_predicado(foco, tools, listos, esperado) -> None:
    foco(tools, listos)
    assert await skills.KnowledgeSkill()._disponible(set()) is esperado


# ------------------------------------------------------------------ handler
def _fragmento(**extra: Any) -> Fragmento:
    base = dict(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        documento="Preguntas frecuentes",
        seccion="Envíos",
        pagina_desde=2,
        pagina_hasta=3,
        texto="El envío es gratis desde 120.000 pesos.",
        similitud=0.61,
        lexico=0.2,
        puntaje=0.0325,
    )
    base.update(extra)
    return Fragmento(**base)  # type: ignore[arg-type]


async def test_handler_usa_el_agente_del_contexto_y_no_el_de_los_argumentos(monkeypatch) -> None:
    vistos: list[tuple[uuid.UUID, str, Config]] = []

    async def _buscar(agent_id: uuid.UUID, consulta: str, config: Config) -> Resultado:
        vistos.append((agent_id, consulta, config))
        return Resultado(fragmentos=[_fragmento(), _fragmento(pagina_desde=None, pagina_hasta=None, seccion=None)], tokens_consulta=7)

    monkeypatch.setattr(busqueda, "buscar", _buscar)
    otro = uuid.uuid4()
    salida = await skills.KnowledgeSkill().buscar_conocimiento(
        {"consulta": "¿envío gratis?", "agent_id": str(otro), "tenant_id": str(uuid.uuid4())},
        _ctx(agent_id=AGENTE, knowledge={"topK": 2}),
    )
    assert vistos == [(AGENTE, "¿envío gratis?", Config("hibrida", 2, None))]
    primero, segundo = salida["fragmentos"]
    assert (primero["documento"], primero["seccion"], primero["paginas"]) == ("Preguntas frecuentes", "Envíos", "2-3")
    assert segundo["paginas"] is None
    assert salida["sin_resultados"] is False
    assert salida["tokens_consulta"] == 7


async def test_handler_sin_agente_y_sin_proveedor(monkeypatch) -> None:
    sin_agente = await skills.KnowledgeSkill().buscar_conocimiento({"consulta": "x"}, _ctx())
    assert sin_agente == {"fragmentos": [], "error": "sin_agente", "tokens_consulta": 0}

    async def _falla(*_a: Any) -> Resultado:
        raise embeddings.EmbeddingsError("http_500")

    monkeypatch.setattr(busqueda, "buscar", _falla)
    caido = await skills.KnowledgeSkill().buscar_conocimiento({"consulta": "x"}, _ctx(agent_id=AGENTE))
    assert caido["error"] == "busqueda_no_disponible"


# ------------------------------------------------------------------ auditoría
def test_auditoria_guarda_ids_y_puntajes_sin_texto() -> None:
    [tool] = skills.KnowledgeSkill().get_tools()
    resultado = {
        "fragmentos": [{"id": "c1", "document_id": "d1", "texto": "secreto del negocio", "puntaje": 0.03}],
        "sin_resultados": False,
        "tokens_consulta": 5,
    }
    auditado = resultado_auditable(tool, tool.name, resultado)
    assert auditado == {"fragmentos": [{"id": "c1", "document_id": "d1", "puntaje": 0.03}], "tokens_consulta": 5}
    assert "secreto" not in repr(auditado)


def test_auditoria_por_defecto_y_fail_closed() -> None:
    assert resultado_auditable(SimpleNamespace(), "otra", {"ok": True}) == {"ok": True}

    def _rompe(_r: dict[str, Any]) -> dict[str, Any]:
        raise KeyError("x")

    assert resultado_auditable(SimpleNamespace(resumen_de_auditoria=_rompe), "t", {"texto": "s"}) == {
        "resumen_fallido": True
    }


# ------------------------------------------------------------------ grabación de embeddings
async def test_replay_sin_grabacion_no_sale_a_la_red(tmp_path) -> None:
    with embeddings.grabacion(tmp_path / "g.json", "replay"):
        async with tenant_scope(TENANT):
            with pytest.raises(GrabacionDesactualizada):
                await embeddings.embeber_conocimiento(["hola"], proposito="consulta")


async def test_record_y_replay_exactos(tmp_path, monkeypatch) -> None:
    llamadas: list[list[str]] = []

    async def _openai(self, textos):  # type: ignore[no-untyped-def]
        llamadas.append(list(textos))
        return [[0.125 * (i + 1)] * embeddings.DIMENSION for i, _ in enumerate(textos)], 9

    monkeypatch.setattr(embeddings._OpenAI, "embeber", _openai)
    ruta = tmp_path / "g.json"
    with embeddings.grabacion(ruta, "record", api_key="sk-prueba"):
        async with tenant_scope(TENANT):
            grabado = await embeddings.embeber_conocimiento(["uno", "dos", "uno"], proposito="consulta")
    assert llamadas == [["uno", "dos"]]  # sin repetidos
    assert grabado.modelo == embeddings.MODELO_OPENAI
    with embeddings.grabacion(ruta, "replay"):
        async with tenant_scope(TENANT):
            repetido = await embeddings.embeber_conocimiento(["dos", "uno"], proposito="consulta")
    assert llamadas == [["uno", "dos"]]
    assert repetido.vectores == [grabado.vectores[1], grabado.vectores[0]]
    # La clave es exacta por texto: «uno» y «Uno» son textos distintos.
    assert embeddings._Grabado.clave("uno") != embeddings._Grabado.clave("Uno")


def test_grabacion_prohibida_en_produccion(tmp_path, monkeypatch) -> None:
    from core.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(GrabacionProhibida), embeddings.grabacion(tmp_path / "g.json", "replay"):
            pass
    finally:
        get_settings.cache_clear()
