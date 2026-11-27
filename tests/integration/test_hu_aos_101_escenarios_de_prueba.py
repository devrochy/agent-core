"""HU-AOS-101 · escenarios de prueba del agente: la IA propone, el dueño escribe, aprueba y borra (ADR-0212).

- **Propuesta:** hasta 10 en `borrador` con `origen='ia'`; solo tools del agente; sin repetir títulos; cobra una
  fila `generacion` en `usage_ledger`; el prompt lleva la configuración del agente y nada de otro negocio.
- **Manual:** el dueño escribe y aprueba (`origen='manual'`, `estado='aprobado'`); tool desconocida, tools en
  conflicto, título repetido y aprobar sin nada que juzgar se rechazan.
- **Límites:** 50 por agente, borrado lógico que libera el título, un pedido de propuesta a la vez, sin saldo.
- **Aislamiento y permisos:** agente o escenario de otro negocio es 404 sin eco; un colaborador sin permiso, 403.

Sin LLM real: el proveedor es un doble con JSON guionado (regla de costo del dueño).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from _ayudas_sql import _sql

pytestmark = [
    pytest.mark.aislamiento,  # escenarios por negocio (ADR-0212, RLS)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

TOOLS = ["consultar_inventario", "crear_pedido"]
PW = "Colab-pass-123"


class ProveedorGuionado:
    """Doble del LLM: devuelve `contenido` o lanza `error`; guarda lo que recibió."""

    provider_name = "guionado"
    model = "guionado"

    def __init__(
        self, contenido: str = "", error: Exception | None = None, demora: float = 0.0
    ) -> None:
        self.contenido = contenido
        self.error = error
        self.demora = demora
        self.llamadas = 0
        self.entradas: list[str] = []
        self.sistemas: list[str] = []

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMUsageInfo

        self.llamadas += 1
        self.entradas.append(kwargs["messages"][0].content)
        self.sistemas.append(kwargs["system_blocks"][0])
        if self.demora:
            await asyncio.sleep(self.demora)
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=self.contenido,
            stop_reason="end_turn",
            usage=LLMUsageInfo(input_tokens=1500, output_tokens=900),
            model="guionado",
            latency_ms=5,
        )

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.002


def _propuesta(n: int, **extra: Any) -> str:
    items = [
        {
            "titulo": f"Caso {i}",
            "objetivo_cliente": f"Quiere resolver la duda {i}",
            "datos_cliente": {"nombre": "Laura"},
            "criterios": [f"Responde la duda {i}"],
            "tools_esperadas": ["consultar_inventario"],
            "tools_prohibidas": ["crear_pedido"],
        }
        for i in range(n)
    ]
    return "Aquí van:\n" + json.dumps({"escenarios": items, **extra})


def _proveedor(monkeypatch, proveedor: ProveedorGuionado) -> ProveedorGuionado:
    from core.pruebas import generador

    monkeypatch.setattr(generador, "_proveedor", lambda: proveedor)
    return proveedor


async def _negocio(
    descripcion: str = "Barbería en Medellín: cortes y productos para el cabello.",
) -> uuid.UUID:
    from _habilidades import onboard_tenant

    slug = uniq("aos101")
    tid, _ = await onboard_tenant(
        name=f"AOS101 {slug}",
        slug=slug,
        business_description=descripcion,
        packs=["pack_commerce"],
        admin_email="admin@local",
        sembrar_ejemplos=False,
    )
    return tid


async def _agente(tid: uuid.UUID, nombre: str = "Ventas") -> uuid.UUID:
    aid = uuid.uuid4()
    await _sql(
        tid,
        "insert into agents (id, tenant_id, key, kind, name, description) "
        "values (:id, :t, :k, 'specialist', :n, 'Vende cortes y productos')",
        id=aid,
        t=tid,
        k=f"ventas_{uuid.uuid4().hex[:6]}",
        n=nombre,
    )
    contenido = {
        "contract": "agentspec/3",
        "tools": [{"tool": t, "enabled": True} for t in TOOLS],
        "guardrails": [
            {"name": "sin_descuentos", "kind": "forbidden_phrase", "phrases": ["te regalo"]}
        ],
    }
    await _sql(
        tid,
        "insert into agent_versions (id, tenant_id, agent_id, version, label, contract, content, content_hash) "
        "values (:id, :t, :a, 1, 'v1', 'agentspec/3', cast(:c as jsonb), :h)",
        id=uuid.uuid4(),
        t=tid,
        a=aid,
        c=json.dumps(contenido),
        h=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    return aid


def _url(aid: uuid.UUID, sufijo: str = "") -> str:
    return f"/api/v1/admin/agents/{aid}/pruebas{sufijo}"


def _manual(**kw: Any) -> dict[str, Any]:
    base = {
        "titulo": "Pregunta por el horario",
        "objetivo_cliente": "Quiere saber si abren el domingo",
        "datos_cliente": {"nombre": "Camilo"},
        "criterios": ["Dice el horario del domingo"],
        "tools_esperadas": [],
        "tools_prohibidas": ["crear_pedido"],
    }
    return {**base, **kw}


async def _ledger(tid: uuid.UUID) -> list[Any]:
    return await _sql(
        tid,
        "select kind, credits, conversation_id, result->>'proposito' from usage_ledger where tenant_id = :t",
        t=tid,
    )


# ---------------------------------------------------------------- proponer
async def test_proponer_guarda_hasta_diez_en_borrador_y_cobra_generacion(
    app_client, monkeypatch
) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    proveedor = _proveedor(monkeypatch, ProveedorGuionado(_propuesta(12)))

    r = await app_client.post(_url(aid, "/proponer"), headers=h)

    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert len(cuerpo["escenarios"]) == 10  # tope por pedido
    assert cuerpo["descartados"] == 2
    assert {e["estado"] for e in cuerpo["escenarios"]} == {"borrador"}
    assert {e["origen"] for e in cuerpo["escenarios"]} == {"ia"}
    primero = cuerpo["escenarios"][0]
    assert primero["tools_esperadas"] == ["consultar_inventario"] and primero[
        "tools_prohibidas"
    ] == ["crear_pedido"]
    # La llamada queda en el ledger como `generacion`, sin conversación y con créditos.
    filas = [f for f in await _ledger(tid) if f[0] == "generacion"]
    assert len(filas) == 1 and filas[0][2] is None and filas[0][3] == "escenarios_de_prueba"
    assert filas[0][1] is not None and filas[0][1] > 0
    assert cuerpo["creditos"] is not None and cuerpo["creditos"] > 0
    # El prompt lleva la configuración del agente y las instrucciones vienen del playbook del core.
    entrada = json.loads(proveedor.entradas[0])
    assert entrada["agente"]["nombre"] == "Ventas" and entrada["cantidad"] == 10
    assert {t["nombre"] for t in entrada["agente"]["tools"]} == set(TOOLS)
    assert "Barbería" in entrada["negocio"]["descripcion"]
    assert (
        "propones casos de prueba" in proveedor.sistemas[0].lower()
        or "Propones casos de prueba" in proveedor.sistemas[0]
    )
    listado = (await app_client.get(_url(aid), headers=h)).json()
    assert (
        len(listado["escenarios"]) == 10
        and listado["tope"] == 50
        and listado["ultima_corrida"] is None
    )


async def test_proponer_descarta_tools_ajenas_titulos_repetidos_y_malformados(
    app_client, monkeypatch
) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    existente = await app_client.post(_url(aid), json=_manual(titulo="Ya existe"), headers=h)
    assert existente.status_code == 201
    contenido = json.dumps(
        {
            "escenarios": [
                {
                    "titulo": "Con tool inventada",
                    "objetivo_cliente": "Quiere algo",
                    "criterios": ["Responde"],
                    "tools_esperadas": ["consultar_inventario", "borrar_la_base"],
                    "tools_prohibidas": ["crear_pedido", "hackear"],
                },
                {"titulo": "ya EXISTE", "objetivo_cliente": "Repetido por mayúsculas"},
                {"titulo": "   ", "objetivo_cliente": "Sin título"},
                {"titulo": "Sin objetivo"},
                {
                    "titulo": "Mismo en las dos",
                    "objetivo_cliente": "x",
                    "tools_esperadas": ["crear_pedido"],
                    "tools_prohibidas": ["crear_pedido"],
                },
                "no soy un objeto",
            ]
        }
    )
    _proveedor(monkeypatch, ProveedorGuionado(contenido))

    r = await app_client.post(_url(aid, "/proponer"), headers=h)

    assert r.status_code == 201, r.text
    por_titulo = {e["titulo"]: e for e in r.json()["escenarios"]}
    assert set(por_titulo) == {"Con tool inventada", "Mismo en las dos"}
    assert por_titulo["Con tool inventada"]["tools_esperadas"] == ["consultar_inventario"]
    assert por_titulo["Con tool inventada"]["tools_prohibidas"] == ["crear_pedido"]
    assert por_titulo["Mismo en las dos"]["tools_esperadas"] == ["crear_pedido"]
    assert por_titulo["Mismo en las dos"]["tools_prohibidas"] == []
    assert r.json()["descartados"] == 4


async def test_proponer_con_proveedor_caido_no_guarda_ni_cobra(app_client, monkeypatch) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    _proveedor(monkeypatch, ProveedorGuionado(error=RuntimeError("caído")))

    r = await app_client.post(_url(aid, "/proponer"), headers=h)

    assert r.status_code == 503 and r.json()["error"]["code"] == "ia_no_disponible"
    assert (await app_client.get(_url(aid), headers=h)).json()["escenarios"] == []
    assert [f for f in await _ledger(tid) if f[0] == "generacion"] == []


async def test_proponer_con_salida_invalida_no_guarda_pero_el_ledger_conserva_el_cobro(
    app_client, monkeypatch
) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    _proveedor(monkeypatch, ProveedorGuionado("no es json"))

    r = await app_client.post(_url(aid, "/proponer"), headers=h)

    assert r.status_code == 502 and r.json()["error"]["code"] == "propuesta_invalida"
    assert (await app_client.get(_url(aid), headers=h)).json()["escenarios"] == []
    assert (
        len([f for f in await _ledger(tid) if f[0] == "generacion"]) == 1
    )  # el proveedor ya cobró


async def test_proponer_sin_saldo_no_llama_a_la_ia(app_client, monkeypatch) -> None:
    from core.billing import creditos
    from core.billing.enforcement import QuotaExceeded

    async def sin_saldo(*a: Any, **k: Any) -> Any:
        raise QuotaExceeded("creditos", 300, 300)

    monkeypatch.setattr(creditos, "alcanza", sin_saldo)
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    proveedor = _proveedor(monkeypatch, ProveedorGuionado(_propuesta(3)))

    r = await app_client.post(_url(aid, "/proponer"), headers=h)

    assert r.status_code == 409 and r.json()["error"]["code"] == "plan_limit_creditos"
    assert proveedor.llamadas == 0


async def test_dos_propuestas_a_la_vez_cobran_una_sola(app_client, monkeypatch) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    proveedor = _proveedor(monkeypatch, ProveedorGuionado(_propuesta(3), demora=0.6))

    a, b = await asyncio.gather(
        app_client.post(_url(aid, "/proponer"), headers=h),
        app_client.post(_url(aid, "/proponer"), headers=h),
    )

    assert sorted([a.status_code, b.status_code]) == [201, 409]
    assert "en_curso" in (a.text + b.text)
    assert proveedor.llamadas == 1
    assert len([f for f in await _ledger(tid) if f[0] == "generacion"]) == 1


# ---------------------------------------------------------------- escribir, aprobar, editar, borrar
async def test_el_dueno_escribe_y_aprueba_su_escenario(app_client) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)

    creado = await app_client.post(_url(aid), json=_manual(), headers=h)
    assert creado.status_code == 201, creado.text
    assert creado.json()["origen"] == "manual" and creado.json()["estado"] == "borrador"

    aprobado = await app_client.patch(
        _url(aid, f"/{creado.json()['id']}"), json={"estado": "aprobado"}, headers=h
    )
    assert aprobado.status_code == 200, aprobado.text
    assert aprobado.json()["estado"] == "aprobado" and aprobado.json()["origen"] == "manual"

    directo = await app_client.post(
        _url(aid), json=_manual(titulo="Otro", estado="aprobado"), headers=h
    )
    assert directo.status_code == 201 and directo.json()["estado"] == "aprobado"


async def test_escenario_manual_con_reglas_de_forma(app_client) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)

    ajena = await app_client.post(
        _url(aid), json=_manual(tools_esperadas=["tool_que_no_tiene"]), headers=h
    )
    conflicto = await app_client.post(
        _url(aid),
        json=_manual(tools_esperadas=["crear_pedido"], tools_prohibidas=["crear_pedido"]),
        headers=h,
    )
    vacio = await app_client.post(
        _url(aid), json=_manual(criterios=[], tools_prohibidas=[], estado="aprobado"), headers=h
    )
    sin_titulo = await app_client.post(_url(aid), json=_manual(titulo="  "), headers=h)

    assert ajena.status_code == 422 and ajena.json()["error"]["code"] == "tool_desconocida"
    assert (
        conflicto.status_code == 422 and conflicto.json()["error"]["code"] == "tools_en_conflicto"
    )
    assert vacio.status_code == 422 and vacio.json()["error"]["code"] == "escenario_incompleto"
    assert sin_titulo.status_code == 422
    assert (await app_client.get(_url(aid), headers=h)).json()["escenarios"] == []


async def test_titulo_unico_por_agente_y_el_borrado_logico_lo_libera(app_client) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    otro = await _agente(tid, "Citas")
    h = await api_headers(app_client, tid)
    primero = await app_client.post(_url(aid), json=_manual(), headers=h)

    repetido = await app_client.post(
        _url(aid), json=_manual(titulo="PREGUNTA POR EL HORARIO"), headers=h
    )
    en_otro_agente = await app_client.post(_url(otro), json=_manual(), headers=h)
    borrado = await app_client.delete(_url(aid, f"/{primero.json()['id']}"), headers=h)
    de_nuevo = await app_client.post(_url(aid), json=_manual(), headers=h)

    assert repetido.status_code == 409 and repetido.json()["error"]["code"] == "titulo_repetido"
    assert en_otro_agente.status_code == 201
    assert borrado.status_code == 204 and de_nuevo.status_code == 201
    fila = await _sql(
        tid,
        "select deleted_at from agent_test_cases where id = :i",
        i=uuid.UUID(primero.json()["id"]),
    )
    assert fila[0][0] is not None  # borrado lógico: la fila sigue
    assert len((await app_client.get(_url(aid), headers=h)).json()["escenarios"]) == 1


async def test_editar_cambia_solo_lo_enviado_y_no_deja_chocar_titulos(app_client) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    a = (await app_client.post(_url(aid), json=_manual(), headers=h)).json()
    b = (await app_client.post(_url(aid), json=_manual(titulo="Segundo"), headers=h)).json()

    editado = await app_client.patch(
        _url(aid, f"/{a['id']}"), json={"criterios": ["Nuevo criterio"]}, headers=h
    )
    choque = await app_client.patch(
        _url(aid, f"/{b['id']}"), json={"titulo": "pregunta por el horario"}, headers=h
    )
    nulo = await app_client.patch(_url(aid, f"/{a['id']}"), json={"titulo": None}, headers=h)
    retoque = await app_client.patch(
        _url(aid, f"/{a['id']}"), json={"titulo": "Pregunta por el horario"}, headers=h
    )

    assert (
        editado.json()["criterios"] == ["Nuevo criterio"]
        and editado.json()["titulo"] == a["titulo"]
    )
    assert choque.status_code == 409 and choque.json()["error"]["code"] == "titulo_repetido"
    assert nulo.status_code == 422
    assert retoque.status_code == 200  # su propio título no choca consigo mismo


async def test_tope_de_cincuenta_escenarios_por_agente(app_client, monkeypatch) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    await _sql(
        tid,
        "insert into agent_test_cases (tenant_id, agent_id, titulo, objetivo_cliente, origen) "
        "select :t, :a, 'Relleno ' || g, 'Objetivo', 'manual' from generate_series(1, 49) g",
        t=tid,
        a=aid,
    )
    ultimo = await app_client.post(_url(aid), json=_manual(), headers=h)
    de_mas = await app_client.post(_url(aid), json=_manual(titulo="Uno de más"), headers=h)
    proveedor = _proveedor(monkeypatch, ProveedorGuionado(_propuesta(3)))
    propuesta = await app_client.post(_url(aid, "/proponer"), headers=h)

    assert ultimo.status_code == 201
    assert de_mas.status_code == 422 and de_mas.json()["error"]["code"] == "tope_de_escenarios"
    assert (
        propuesta.status_code == 422 and propuesta.json()["error"]["code"] == "tope_de_escenarios"
    )
    assert proveedor.llamadas == 0  # no se paga una propuesta que no cabe


async def test_la_propuesta_se_recorta_al_cupo_que_queda(app_client, monkeypatch) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    h = await api_headers(app_client, tid)
    await _sql(
        tid,
        "insert into agent_test_cases (tenant_id, agent_id, titulo, objetivo_cliente, origen) "
        "select :t, :a, 'Relleno ' || g, 'Objetivo', 'manual' from generate_series(1, 47) g",
        t=tid,
        a=aid,
    )
    proveedor = _proveedor(monkeypatch, ProveedorGuionado(_propuesta(10)))

    r = await app_client.post(_url(aid, "/proponer"), headers=h)

    assert r.status_code == 201 and len(r.json()["escenarios"]) == 3
    assert json.loads(proveedor.entradas[0])["cantidad"] == 3


# ---------------------------------------------------------------- aislamiento y permisos
async def test_otro_negocio_no_ve_ni_toca_los_escenarios_ni_el_agente(
    app_client, monkeypatch
) -> None:
    a = await _negocio()
    b = await _negocio("Pastelería en Cali")
    agente_a = await _agente(a)
    agente_b = await _agente(b)
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    caso_a = (await app_client.post(_url(agente_a), json=_manual(), headers=ha)).json()
    proveedor = _proveedor(monkeypatch, ProveedorGuionado(_propuesta(2)))

    # Con el contexto del negocio B, el agente y el escenario de A no existen.
    assert (await app_client.get(_url(agente_a), headers=hb)).status_code == 404
    assert (
        await app_client.post(_url(agente_a), json=_manual(titulo="Intruso"), headers=hb)
    ).status_code == 404
    assert (await app_client.post(_url(agente_a, "/proponer"), headers=hb)).status_code == 404
    assert (
        await app_client.patch(
            _url(agente_a, f"/{caso_a['id']}"), json={"estado": "aprobado"}, headers=hb
        )
    ).status_code == 404
    assert (
        await app_client.delete(_url(agente_a, f"/{caso_a['id']}"), headers=hb)
    ).status_code == 404
    # Ni mezclando el agente de B con el escenario de A.
    assert (
        await app_client.patch(
            _url(agente_b, f"/{caso_a['id']}"), json={"estado": "aprobado"}, headers=hb
        )
    ).status_code == 404
    assert (
        await app_client.delete(_url(agente_b, f"/{caso_a['id']}"), headers=hb)
    ).status_code == 404
    assert proveedor.llamadas == 0
    # El escenario de A sigue intacto y B no tiene ninguno.
    assert (await app_client.get(_url(agente_a), headers=ha)).json()["escenarios"][0][
        "estado"
    ] == "borrador"
    assert (await app_client.get(_url(agente_b), headers=hb)).json()["escenarios"] == []
    # La RLS lo ratifica en la base: desde el scope de B no se ve la fila de A.
    assert (
        await _sql(b, "select 1 from agent_test_cases where id = :i", i=uuid.UUID(caso_a["id"]))
        == []
    )


async def _colaborador(tid: uuid.UUID, permisos: dict[str, str]) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('aos101')}-colab@test.local"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion, acceso in permisos.items():
            session.add(
                AdminPermission(admin_id=admin.id, section=seccion, access=acceso, tenant_id=tid)
            )
    return email


async def _cabeceras(app_client, tid: uuid.UUID, email: str) -> dict[str, str]:
    return await api_headers(app_client, tid, email=email, password=PW)


async def test_colaborador_sin_permiso_recibe_403_y_con_lectura_solo_lista(
    app_client, monkeypatch
) -> None:
    tid = await _negocio()
    aid = await _agente(tid)
    dueno = await api_headers(app_client, tid)
    caso = (await app_client.post(_url(aid), json=_manual(), headers=dueno)).json()
    proveedor = _proveedor(monkeypatch, ProveedorGuionado(_propuesta(2)))
    sin = await _cabeceras(app_client, tid, await _colaborador(tid, {"agents": "none"}))
    lector = await _cabeceras(app_client, tid, await _colaborador(tid, {"agents": "read"}))
    editor = await _cabeceras(app_client, tid, await _colaborador(tid, {"agents": "write"}))

    for h in (sin,):
        assert (await app_client.get(_url(aid), headers=h)).status_code == 403
    for h in (sin, lector):
        assert (
            await app_client.post(_url(aid), json=_manual(titulo="X"), headers=h)
        ).status_code == 403
        assert (await app_client.post(_url(aid, "/proponer"), headers=h)).status_code == 403
        assert (
            await app_client.patch(
                _url(aid, f"/{caso['id']}"), json={"estado": "aprobado"}, headers=h
            )
        ).status_code == 403
        assert (await app_client.delete(_url(aid, f"/{caso['id']}"), headers=h)).status_code == 403
    assert (await app_client.get(_url(aid), headers=lector)).status_code == 200
    assert (
        await app_client.post(_url(aid), json=_manual(titulo="Del editor"), headers=editor)
    ).status_code == 201
    assert proveedor.llamadas == 0
