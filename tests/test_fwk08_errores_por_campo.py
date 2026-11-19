"""HU-FWK-08: los errores de config traen el campo que falló."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError


class _ConfigEjemplo(BaseModel):
    monto: int


def test_plugin_config_error_trae_fields() -> None:
    from core.shared.exceptions import PluginConfigError

    try:
        _ConfigEjemplo(monto="no-es-numero")  # type: ignore[arg-type]
    except ValidationError as exc:
        fields = {".".join(str(p) for p in err["loc"]): err["msg"] for err in exc.errors()}
        assert "monto" in fields

    err = PluginConfigError("pack_x", "inválida", {"monto": "no es un número"})
    assert err.fields == {"monto": "no es un número"}


def test_api_error_bad_request_con_fields() -> None:
    from core.api.errors import ApiError

    err = ApiError.bad_request("Inválida", "invalid_config", fields={"campo": "malo"})
    assert err.fields == {"campo": "malo"}
    assert err.status_code == 400
