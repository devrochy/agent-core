"""SPEC-40 · piezas puras de la API de plantillas con opciones (HU-OPC-05): hash, lista de `datos`, mapeo de errores,
forma de las rutas y el desglose de «avisos sin plantilla». La API contra Postgres está en
`tests/integration/test_opciones_api.py`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from core.admin import plantillas_opciones_service as svc
from core.api.routers import plantillas_opciones as router_mod
from core.api.routers import settings as settings_router
from core.gateway import opciones_envio
from core.kernel.opciones import contexto, envio
from core.kernel.opciones.definicion import Definicion, hash_de


def _definicion(**cambios) -> Definicion:
    base = {
        "body": "Hola {cliente.nombre}",
        "variables": ["cliente.nombre"],
        "opciones": [
            {
                "id": "ver", "titulo": "Ver", "accion": {"tool": "t", "args_fijos": {"a": 1, "b": 2}},
                "respuesta_ok": "ok", "respuesta_error": "error",
            }
        ],
    }
    return Definicion.model_validate({**base, **cambios})


def test_el_hash_es_estable_no_depende_del_orden_de_las_claves_y_cambia_con_el_contenido() -> None:
    a = _definicion()
    b = Definicion.model_validate(
        {
            "opciones": [
                {
                    "respuesta_error": "error", "respuesta_ok": "ok", "titulo": "Ver", "id": "ver",
                    "accion": {"args_fijos": {"b": 2, "a": 1}, "tool": "t"},
                }
            ],
            "variables": ["cliente.nombre"], "body": "Hola {cliente.nombre}",
        }
    )
    assert hash_de(a) == hash_de(b) and len(hash_de(a)) == 64
    assert hash_de(a) != hash_de(_definicion(body="Hola otra vez {cliente.nombre}"))
    assert hash_de(a) == hash_de(_definicion(exclusivo=True, ttl_horas=24, footer=None)), "los valores por defecto no cambian el hash"
    assert hash_de(a) != hash_de(_definicion(ttl_horas=48))


def test_la_lista_de_rutas_de_datos_esta_cerrada_y_es_la_de_los_eventos_sin_cobranza() -> None:
    # ADR-0178 D3: son las rutas de los eventos que pueden apuntar a una plantilla con opciones. Cada ruta nueva exige
    # actualizar este conjunto a propósito (revisión): son datos que una opción puede citar y pasar a una tool.
    assert set(contexto.RUTAS_DE_DATOS) == {
        "cita.titulo", "cita.fecha", "cita.hora", "cita.zona", "cita.enlace",
        "suscripcion.concepto", "prueba.termina", "invitacion.correo",
    }
    assert not set(contexto.RUTAS_DE_DATOS) & set(contexto.VARIABLES_BASE)
    with pytest.raises(contexto.DatosInvalidos):
        contexto.normalizar_datos({"pedido.numero": "A-1"})
    with pytest.raises(contexto.DatosInvalidos):
        contexto.normalizar_datos({"cargo.monto": "1000"})  # cobranza: no se ofrece
    assert contexto.normalizar_datos(None) == {} and contexto.normalizar_datos({}) == {}
    assert contexto.catalogo_de_variables() == {**contexto.VARIABLES_BASE, **contexto.RUTAS_DE_DATOS}
    assert {v["ruta"]: v["origen"] for v in svc.catalogo_variables()} == {
        **{r: "conversacion" for r in contexto.VARIABLES_BASE}, **{r: "servidor" for r in contexto.RUTAS_DE_DATOS}
    }


def test_ninguna_ruta_de_datos_es_un_id() -> None:
    """Los datos de un evento son valores para mostrar. Un id (de cita, de cargo, de cliente) que llegara a un
    argumento de tool exigiría comprobar que pertenece al negocio y al cliente de la conversación."""
    for ruta in contexto.RUTAS_DE_DATOS:
        ultimo = ruta.rsplit(".", 1)[-1]
        assert ultimo != "id" and not ultimo.endswith("_id") and not ultimo.startswith("id_"), ruta


def test_una_ruta_agregada_a_la_lista_entra_al_catalogo_y_a_datos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(contexto.RUTAS_DE_DATOS, "cita.fecha", "Fecha de la cita")
    assert contexto.normalizar_datos({"cita.fecha": "2026-11-02"}) == {"cita.fecha": "2026-11-02"}
    assert {v["ruta"]: v["origen"] for v in svc.catalogo_variables()}["cita.fecha"] == "servidor"
    with pytest.raises(contexto.DatosInvalidos):
        contexto.normalizar_datos({"cliente.nombre": "Mallory"})


def test_toda_falla_de_envio_tiene_su_estado_http_o_es_un_502_documentado() -> None:
    fallas = {
        v for k, v in vars(envio).items()
        if k.isupper() and isinstance(v, str)
        and k in {"BANDERA_APAGADA", "CLAVE_INVALIDA", "NO_ENCONTRADA", "CONVERSACION_NO_ENCONTRADA", "NO_PUBLICADA",
                  "CONTEXTO_INCOMPLETO", "SIN_PLANTILLA", "PAUSADA", "NO_ENCOLADO", "DATOS_INVALIDOS", "ERROR_INTERNO"}
    }
    sin_mapear = fallas - set(svc._HTTP_DE_SALIDA)
    assert sin_mapear == {envio.NO_ENCOLADO, envio.DATOS_INVALIDOS, envio.ERROR_INTERNO}, "estos tres son 502 `no_se_pudo_enviar`"


def test_cada_ruta_exige_permiso_y_bandera_y_las_de_escritura_ademas_ser_duenio() -> None:
    rutas = [r for r in router_mod.router.routes if isinstance(r, APIRoute)]
    assert len(rutas) == 13
    for r in rutas:
        dependencias = {d.call for d in r.dependant.dependencies}
        escribe = r.methods - {"GET"} and r.path not in {"/plantillas-opciones/validar", "/plantillas-opciones/vista-previa"}
        esperada = router_mod.escritura if escribe else router_mod.lectura
        assert esperada in dependencias, (r.methods, r.path)
        assert (router_mod.lectura in dependencias) != (router_mod.escritura in dependencias), (r.path, "una sola de las dos")
    assert {r.path for r in rutas} >= {
        "/plantillas-opciones/catalogo-acciones", "/plantillas-opciones/catalogo-variables", "/plantillas-opciones/{clave}/enviar-prueba",
    }


def test_las_rutas_fijas_van_antes_que_la_clave_para_que_ninguna_clave_las_tape() -> None:
    gets = [r.path for r in router_mod.router.routes if isinstance(r, APIRoute) and "GET" in r.methods]
    assert gets.index("/plantillas-opciones/catalogo-acciones") < gets.index("/plantillas-opciones/{clave}")
    assert gets.index("/plantillas-opciones/catalogo-variables") < gets.index("/plantillas-opciones/{clave}")


def test_una_definicion_enorme_se_rechaza_antes_de_validarla() -> None:
    with pytest.raises(svc.ErrorPlantillaOpciones) as exc:
        svc.leer_forma({"body": "x", "opciones": [], "variables": ["a" * 100] * 30, "relleno": "z" * 200_000})
    assert exc.value.status == 422 and exc.value.code == "definicion_invalida"


# ---------------------------------------------------------------- «avisos sin plantilla»
def test_el_prefijo_del_desglose_es_el_de_los_envios_de_opciones() -> None:
    assert settings_router.PREFIJO_OPCIONES == f"{opciones_envio.PREFIJO_ID}:"


def _mensaje(client_id: str | None, contenido: str):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    return SimpleNamespace(conversation_id="c1", content=contenido, created_at=datetime.now(UTC), client_message_id=client_id)


def test_un_envio_de_opciones_se_distingue_de_un_aviso_por_evento() -> None:
    opcion = settings_router._missing_item(
        _mensaje("opt_send:4b1f:f12ab34c", "Aviso «opciones:cobro_vencido» sin enviar: la plantilla de Meta no está aprobada")
    )
    assert (opcion.tipo, opcion.clave) == ("opcion", "cobro_vencido")
    evento = settings_router._missing_item(_mensaje("aviso:1", "Aviso «subscription.charge_created» sin enviar: evento sin plantilla"))
    assert (evento.tipo, evento.clave) == ("evento", None)
    sin_id = settings_router._missing_item(_mensaje(None, "Aviso «opciones:x» sin enviar: lo que sea"))
    assert (sin_id.tipo, sin_id.clave) == ("evento", None), "el tipo lo da el id del mensaje, no el texto"
    raro = settings_router._missing_item(_mensaje("opt_send:x:f1", "texto sin la forma esperada"))
    assert (raro.tipo, raro.clave) == ("opcion", None)


@pytest.mark.asyncio
async def test_el_servicio_se_niega_si_el_tenant_pedido_no_es_el_del_scope() -> None:
    import uuid

    from core.shared.tenant import tenant_scope

    a, b = uuid.uuid4(), uuid.uuid4()
    async with tenant_scope(a):
        for llamada in (svc.listar(b), svc.obtener(b, "x"), svc.publicar(b, "t", "x"), svc.enviar_prueba(b, "t", "x", uuid.uuid4(), "12345678"),
                        svc.catalogo_acciones(b)):
            with pytest.raises(PermissionError):
                await llamada
