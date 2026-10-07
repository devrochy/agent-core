"""Registrar un número en la Cloud API con PIN (`meta_numbers_service.register_number`).

Antes solo lo hacía el wizard de Embedded Signup; ahora cualquier conexión (BYOK, manual).
Con Graph, el token store, la BD y Redis falsos: lo que se prueba es el orden (el PIN se
guarda antes de llamar a Meta), que el número sea de la conexión, el PIN (un rechazo de Meta
no borra el bueno) y los errores.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import core.admin.meta_numbers_service as service
from core.gateway.meta_errors import GraphError

TENANT = uuid.uuid4()
CONNECTION = uuid.uuid4()
PHONE_ID = "ph-123"
PIN_KIND = "phone_register_pin"


class _FakeRedis:
    def __init__(self):
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


def _rechazo(message: str, code: int) -> GraphError:
    """Meta contestó con un error (hay `http_status`): el PIN no quedó en el número."""
    return GraphError(message, category="PERMANENT", http_status=400, code=code)


@pytest.fixture
def mundo(monkeypatch):
    """Conexión con credenciales y un número suyo; anota escrituras y llamadas en `eventos`."""
    eventos: list[tuple] = []
    tokens: dict[tuple[str, str], str] = {}
    phone = SimpleNamespace(id=uuid.uuid4(), tenant_id=TENANT, registered=False, pin_set=False)
    numeros = {PHONE_ID: phone}
    connection = SimpleNamespace(id=CONNECTION, tenant_id=TENANT, health={})
    creds = SimpleNamespace(access_token="tok", app_secret="sec", graph_api_version="v21.0")
    estado = SimpleNamespace(graph_error=None, phone=phone, creds=creds)

    async def fake_resolve(cid, tenant_id):
        assert (cid, tenant_id) == (CONNECTION, TENANT)
        return connection, None, estado.creds

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _Session:
        async def execute(self, stmt):
            # El select filtra por número, conexión y negocio (en el WHERE, no en el SELECT);
            # el falso solo conoce el número.
            where = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True}))
            assert f"phone_numbers.connection_id = '{CONNECTION.hex}'" in where.replace("-", "")
            assert f"phone_numbers.tenant_id = '{TENANT.hex}'" in where.replace("-", "")
            return _Result(next((v for k, v in numeros.items() if f"'{k}'" in where), None))

        async def get(self, _model, _id):
            return estado.phone

    @asynccontextmanager
    async def fake_session():
        yield _Session()

    async def fake_put_token(connection_id, kind, subject, value, *, tenant_id):
        assert (connection_id, tenant_id) == (CONNECTION, TENANT)
        eventos.append(("put_token", kind, subject, value))
        tokens[(kind, subject)] = value

    async def fake_get_token(connection_id, kind, subject, *, tenant_id):
        assert (connection_id, tenant_id) == (CONNECTION, TENANT)
        return tokens.get((kind, subject))

    async def fake_revoke_token(connection_id, kind, subject, *, tenant_id):
        assert (connection_id, tenant_id) == (CONNECTION, TENANT)
        eventos.append(("revoke_token", kind, subject))
        tokens.pop((kind, subject), None)

    async def fake_register(phone_number_id, token, app_secret, version, connection_id, *, pin):
        eventos.append(("register", phone_number_id, token, pin))
        if estado.graph_error is not None:
            raise estado.graph_error

    redis = _FakeRedis()
    monkeypatch.setattr(service, "resolve_waba_and_credentials", fake_resolve)
    monkeypatch.setattr(service, "get_session", fake_session)
    monkeypatch.setattr("core.gateway.meta_token_store.put_token", fake_put_token)
    monkeypatch.setattr("core.gateway.meta_token_store.get_token", fake_get_token)
    monkeypatch.setattr("core.gateway.meta_token_store.revoke_token", fake_revoke_token)
    monkeypatch.setattr("core.gateway.meta_phone_numbers.register_phone_number", fake_register)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)
    return SimpleNamespace(
        eventos=eventos, tokens=tokens, phone=phone, connection=connection, estado=estado, redis=redis
    )


def _pin_guardado(mundo) -> str | None:
    return mundo.tokens.get((PIN_KIND, PHONE_ID))


@pytest.mark.asyncio
async def test_sin_pin_genera_uno_lo_guarda_antes_de_llamar_a_meta_y_marca_registrado(mundo):
    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID) is None

    (guardado, llamada) = mundo.eventos
    assert guardado[:3] == ("put_token", PIN_KIND, PHONE_ID)
    pin = guardado[3]
    assert len(pin) == 6 and pin.isdigit()
    assert llamada == ("register", PHONE_ID, "tok", pin)
    assert (mundo.phone.registered, mundo.phone.pin_set) == (True, True)


@pytest.mark.asyncio
async def test_sin_pin_reusa_el_guardado(mundo):
    # El wizard de Embedded Signup fija un PIN que nadie vio: generar otro daría 133005 seguro.
    mundo.tokens[(PIN_KIND, PHONE_ID)] = "135790"

    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID) is None

    assert mundo.eventos == [("register", PHONE_ID, "tok", "135790")]
    assert (mundo.phone.registered, mundo.phone.pin_set) == (True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("vacio", ["", "  "])
async def test_un_pin_vacio_es_generarlo(mundo, vacio):
    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin=vacio) is None
    pin = mundo.eventos[1][3]
    assert len(pin) == 6 and pin.isdigit()


@pytest.mark.asyncio
async def test_con_pin_usa_ese_y_lo_guarda(mundo):
    mundo.tokens[(PIN_KIND, PHONE_ID)] = "135790"

    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin="246810") is None

    assert mundo.eventos[1] == ("register", PHONE_ID, "tok", "246810")
    assert _pin_guardado(mundo) == "246810"


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", ["12345", "1234567", "12a456", "\uff11\uff12\uff13\uff14\uff15\uff16"])
async def test_un_pin_mal_formado_no_llega_a_meta(mundo, pin):
    error = await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin=pin)
    assert error == "El PIN tiene que ser de 6 dígitos"
    assert mundo.eventos == []


@pytest.mark.asyncio
async def test_un_numero_que_no_es_de_la_conexion_no_llega_a_meta(mundo):
    error = await service.register_number(TENANT, str(CONNECTION), "ph-de-otra-conexion")
    assert error == "El número no es de esta conexión"
    assert mundo.eventos == []


@pytest.mark.asyncio
async def test_sin_credenciales_no_llega_a_meta(mundo):
    mundo.estado.creds = None
    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID) == "No hay conexión lista todavía"
    assert mundo.eventos == []


@pytest.mark.asyncio
async def test_en_coexistencia_no_se_registra(mundo):
    mundo.connection.health = {"coexistence": True}

    error = await service.register_number(TENANT, str(CONNECTION), PHONE_ID)

    assert error is not None and "app de WhatsApp Business" in error
    assert mundo.eventos == []
    assert mundo.redis.keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "esperado"),
    [
        (_rechazo("PIN mismatch", 133005), "otro PIN"),
        (_rechazo("too many", 133008), "bloqueó"),
        (_rechazo("Número inválido", 100), "Número inválido"),
    ],
)
async def test_un_rechazo_de_meta_devuelve_el_pin_que_habia(mundo, error, esperado):
    mundo.tokens[(PIN_KIND, PHONE_ID)] = "135790"
    mundo.estado.graph_error = error

    resultado = await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin="246810")

    assert resultado is not None and esperado in resultado
    assert mundo.phone.registered is False
    assert _pin_guardado(mundo) == "135790"


@pytest.mark.asyncio
async def test_un_rechazo_sin_pin_previo_no_deja_guardado_el_nuevo(mundo):
    mundo.estado.graph_error = _rechazo("PIN mismatch", 133005)

    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID) is not None

    assert _pin_guardado(mundo) is None
    assert mundo.eventos[-1] == ("revoke_token", PIN_KIND, PHONE_ID)


@pytest.mark.asyncio
async def test_sin_respuesta_de_meta_se_queda_el_pin_nuevo(mundo):
    # Timeout: no se sabe si el registro llegó; si llegó, el PIN del número es el nuevo.
    mundo.tokens[(PIN_KIND, PHONE_ID)] = "135790"
    mundo.estado.graph_error = GraphError("Timeout", category="TRANSIENT", is_transient=True)

    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin="246810") is not None

    assert _pin_guardado(mundo) == "246810"
    assert mundo.phone.registered is False


@pytest.mark.asyncio
async def test_ya_registrado_es_exito_pero_no_da_el_pin_por_fijado(mundo):
    mundo.tokens[(PIN_KIND, PHONE_ID)] = "135790"
    mundo.estado.graph_error = _rechazo("Phone number is already registered", 1)

    assert await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin="246810") is None

    assert (mundo.phone.registered, mundo.phone.pin_set) == (True, False)
    assert _pin_guardado(mundo) == "135790"


@pytest.mark.asyncio
async def test_un_intento_por_minuto(mundo):
    mundo.estado.graph_error = _rechazo("PIN mismatch", 133005)
    await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin="111111")
    segundo = await service.register_number(TENANT, str(CONNECTION), PHONE_ID, pin="222222")

    assert segundo == "Esperá un minuto antes de reintentar el registro"
    assert [e for e in mundo.eventos if e[0] == "register"] == [("register", PHONE_ID, "tok", "111111")]
    assert list(mundo.redis.keys) == [f"register_number:{TENANT}:{PHONE_ID}"]
