"""HU-AOS-12 · lógica del editor de tools declarativas sin secretos (unitarios, sin BD)."""

from __future__ import annotations

from typing import Any

from core.agents.declarativas import MASCARA, enmascarar, literales_sin_migrar, preparar
from core.mcp.declarative import secret_ref, validate_declarative_tools

DOM = "api.proveedor.co"
ACCIONES = {"crear_pedido", "consultar_inventario"}


def tool(headers: dict[str, Any], dominios: list[str] | None = None, nombre: str = "consultar") -> dict[str, Any]:
    return {"name": nombre, "url": f"https://{DOM}/x", "allowed_domains": dominios or [DOM], "headers": headers}


def test_validate_conserva_referencias_y_stringifica_lo_demas() -> None:
    t = validate_declarative_tools([tool({"Authorization": {"secret": "tok"}, "X-N": 3, "X-Mal": {"secret": "MAL"}})])[0]
    assert t["headers"] == {"Authorization": {"secret": "tok"}, "X-N": "3", "X-Mal": "{'secret': 'MAL'}"}
    assert secret_ref({"secret": "tok"}) == "tok" and secret_ref({"secret": "tok", "x": 1}) is None


def test_enmascara_literales_sensibles() -> None:
    raw = [tool({"authorization": "Bearer x", "Cookie": {"secret": "c"}, "X-Cliente": "a"})]
    assert enmascarar(raw)[0]["headers"] == {"authorization": MASCARA, "Cookie": {"secret": "c"}, "X-Cliente": "a"}
    assert literales_sin_migrar(raw) == ["consultar.authorization"]


def test_literal_nuevo_se_rechaza_y_la_mascara_migra() -> None:
    r = preparar([tool({"Authorization": "Bearer nuevo"})], [], {}, {}, ACCIONES)
    assert list(r.errores) == ["tools[0].headers.Authorization"] and r.tools == []
    assert "Bearer nuevo" not in str(r.errores)

    r = preparar([tool({"Authorization": MASCARA})], [tool({"Authorization": "Bearer viejo"})], {}, {}, ACCIONES)
    assert r.errores == {}
    assert r.tools[0]["headers"] == {"Authorization": {"secret": "consultar_authorization"}}
    secreto = r.secretos["consultar_authorization"]
    assert secreto.valor.get_secret_value() == "Bearer viejo" and secreto.dominios == (DOM,)

    # Máscara sin literal guardado detrás: es un literal más.
    assert preparar([tool({"Authorization": MASCARA})], [], {}, {}, ACCIONES).errores


def test_referencias_y_dominios() -> None:
    conf = {"tok": [DOM]}
    assert preparar([tool({"Authorization": {"secret": "tok"}})], [], {}, conf, ACCIONES).errores == {}
    assert preparar([tool({"Authorization": {"secret": "otro"}})], [], {}, conf, ACCIONES).errores
    # Dominio nuevo sin reescribir el secreto → error; reescribiéndolo, se ata a ambos.
    dos = tool({"Authorization": {"secret": "tok"}}, [DOM, "otro.co"])
    assert "vuelve a escribirlo" in next(iter(preparar([dos], [], {}, conf, ACCIONES).errores.values()))
    r = preparar([dos], [], {"tok": "Bearer y"}, conf, ACCIONES)
    assert r.errores == {} and r.secretos["tok"].dominios == (DOM, "otro.co")
    # Quitar un secreto que una tool usa → error; uno sin uso → se quita.
    assert preparar([tool({"Authorization": {"secret": "tok"}})], [], {"tok": None}, conf, ACCIONES).errores
    r = preparar([], [], {"tok": None}, conf, ACCIONES)
    assert r.errores == {} and r.quitar == ["tok"]
    # Secreto enviado que ninguna tool usa → error.
    assert "secrets.suelto" in preparar([], [], {"suelto": "x"}, {}, ACCIONES).errores


def test_contrato_y_binario() -> None:
    assert "tools[0].name" in preparar([tool({}, nombre="crear_pedido")], [], {}, {}, ACCIONES).errores
    http = tool({"Authorization": {"secret": "tok"}}) | {"url": f"http://{DOM}/x"}
    assert "https" in str(preparar([http], [], {}, {"tok": [DOM]}, ACCIONES).errores)
    assert preparar([tool({}), tool({})], [], {}, {}, ACCIONES).errores == {"tools[1].name": "La tool 'consultar' está repetida."}
    assert preparar([{"name": "sin url"}], [], {}, {}, ACCIONES).errores
    assert preparar([tool({}, ["10.0.0.1"])], [], {}, {}, ACCIONES).errores
    assert preparar({"no": "lista"}, [], {}, {}, ACCIONES).errores


def test_la_mascara_no_lleva_el_valor_guardado_a_un_dominio_nuevo() -> None:
    guardada = [tool({"Authorization": "Bearer viejo"})]
    otra = tool({"Authorization": MASCARA}, [DOM, "suyo.com"])
    r = preparar([otra], guardada, {}, {}, ACCIONES)
    assert "vuelve a escribirlo" in r.errores["tools[0].headers.Authorization"]
    assert r.secretos == {} and r.tools == []


def test_la_mascara_no_se_reusa_desde_otra_tool_de_la_misma_peticion() -> None:
    guardada = [tool({"Authorization": "Bearer viejo"})]
    legit = tool({"Authorization": MASCARA})
    ladrona = tool({"Authorization": {"secret": "consultar_authorization"}}, ["suyo.com"], nombre="ladrona") | {
        "url": "https://suyo.com/x"
    }
    r = preparar([legit, ladrona], guardada, {}, {}, ACCIONES)
    assert "vuelve a escribirlo" in r.errores["tools[1].headers.Authorization"]
    assert "suyo.com" not in str([s.dominios for s in r.secretos.values()])
