"""Enganche de respuestas por voz en el egress (ADR-0197)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.gateway import sender
from core.kernel import egress
from core.mcp.host import mcp_host
from core.mcp.types import VoicePlan


async def test_entregar_usa_voz_si_la_politica_lo_pide(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: dict[str, object] = {}

    async def _resolve(_cid: uuid.UUID, _texto: str) -> VoicePlan:
        return VoicePlan(include_text=True, plugin_id="pack_voice")

    async def _voz(cid: uuid.UUID, texto: str, **kwargs: object) -> bool:
        llamadas["voz"] = (cid, texto, kwargs)
        return True

    async def _texto(*_a: object, **_k: object) -> bool:
        llamadas["texto"] = True
        return True

    async def _escala(*_a: object, **_k: object) -> None:
        llamadas["escala"] = True

    monkeypatch.setattr(mcp_host, "resolve_voice", _resolve)
    monkeypatch.setattr(sender, "send_voice_to_conversation", _voz)
    monkeypatch.setattr(sender, "send_to_conversation", _texto)
    monkeypatch.setattr("core.orchestrator.escalation.escalate", _escala)

    cid = uuid.uuid4()
    resultado = SimpleNamespace(text="hola", image_url=None, confirmacion=None, escalated=False)
    await egress.entregar(cid, resultado)

    assert llamadas.get("voz") == (cid, "hola", {"include_text": True})
    assert "texto" not in llamadas


async def test_entregar_no_usa_voz_si_la_respuesta_lleva_imagen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llamadas: dict[str, object] = {}

    async def _resolve(_cid: uuid.UUID, _texto: str) -> VoicePlan:
        raise AssertionError("con imagen no se consulta la política de voz")

    async def _texto(_cid: uuid.UUID, _texto: str, **kwargs: object) -> bool:
        llamadas["texto"] = kwargs
        return True

    async def _escala(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(mcp_host, "resolve_voice", _resolve)
    monkeypatch.setattr(sender, "send_to_conversation", _texto)
    monkeypatch.setattr("core.orchestrator.escalation.escalate", _escala)

    resultado = SimpleNamespace(
        text="mirá", image_url="https://api/files/foto.jpg", confirmacion=None, escalated=False
    )
    await egress.entregar(uuid.uuid4(), resultado)

    assert llamadas["texto"] == {"image_url": "https://api/files/foto.jpg"}


async def test_entregar_no_convierte_una_confirmacion_en_voz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llamadas: dict[str, object] = {}

    async def _resolve(_cid: uuid.UUID, _texto: str) -> VoicePlan:
        raise AssertionError("una confirmación no pasa por la política de voz")

    async def _texto(*_a: object, **_k: object) -> bool:
        llamadas["texto"] = True
        return True

    async def _escala(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(mcp_host, "resolve_voice", _resolve)
    monkeypatch.setattr(sender, "send_to_conversation", _texto)
    monkeypatch.setattr("core.orchestrator.escalation.escalate", _escala)

    resultado = SimpleNamespace(
        text="¿confirmás?", image_url=None, confirmacion=SimpleNamespace(modo="texto"), escalated=False
    )
    await egress.entregar(uuid.uuid4(), resultado)

    assert llamadas.get("texto") is True


async def test_send_voice_no_se_usa_en_un_canal_sin_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        async def get(self, _model, _id):
            return SimpleNamespace(
                channel="whatsapp",
                connection_id=uuid.uuid4(),
                meta_connection_id=None,
                tenant_id=uuid.uuid4(),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    async def _credenciales(*_a: object, **_k: object):
        return "meta", {"access_token": "tok"}

    monkeypatch.setattr(sender, "get_session", lambda: FakeSession())
    monkeypatch.setattr(sender, "resolve_credentials", _credenciales)

    assert await sender.send_voice_to_conversation(uuid.uuid4(), "hola") is False
