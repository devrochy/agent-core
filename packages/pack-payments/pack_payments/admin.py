"""Vista admin de la configuración de pagos del tenant (pack-payments).

Aporta a la pestaña "Pagos" de Configuración: formulario de credenciales Wompi
por tenant (cifradas, `plugin_configs`), botón "Probar y guardar" (crea un link
de prueba real contra Wompi) y la URL del webhook para registrar en Wompi.

Se monta dentro de `/admin/plugins/pack_payments/...` (el mismo prefijo que el
`admin_router` del pack, que el core incluye al arrancar).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from core.admin.auth import current_admin
from core.admin.context import base_context
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_payments.config import PaymentsConfig
from pack_payments.proveedor import build_provider
from pack_payments.providers.base import PaymentRequestRejected
from pack_payments.providers.wompi import min_amount_cop

log = get_logger(__name__)

PLUGIN_ID = "pack_payments"

router = APIRouter(prefix=f"/admin/plugins/{PLUGIN_ID}/config", tags=[PLUGIN_ID])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _core_templates_dir() -> Path:
    """Directorio de plantillas del core (para heredar `base.html`).

    Se resuelve por el paquete `core` instalado, no por el layout del repo: el
    pack puede correr desde una wheel y `parents[N]` apuntaría a otro sitio.
    """
    import core

    return Path(core.__file__).resolve().parent / "admin" / "templates"


_templates.env.loader.searchpath.append(str(_core_templates_dir()))


def _render(name: str, ctx: dict):
    return _templates.TemplateResponse(ctx["request"], name, ctx)


async def _require_settings_write(request: Request, *, write: bool) -> bool:
    """La pestaña Pagos pertenece a la sección de Configuración (ADR-0049): se
    autoriza contra `settings`, no contra `plugins` (el prefijo de la ruta)."""
    from core.admin.permissions import require_section

    return await require_section(request, "settings", write=write)


async def _wompi_webhook_url() -> str:
    from core.config import get_settings
    from core.db.models import Tenant
    from core.db.session import get_session

    base = get_settings().public_base_url.rstrip("/")
    slug = ""
    tid = get_current_tenant_id()
    if tid is not None:
        async with get_session() as session:
            tenant = await session.get(Tenant, tid)
            slug = tenant.slug if tenant else ""
    return f"{base}/webhooks/payments/wompi/{slug}" if slug else "(sin tenant)"


async def _pack_enabled() -> bool:
    """El pack debe estar habilitado para el tenant activo (ADR-0049): la tab se
    oculta sin él, pero el acceso directo a la URL también debe rechazarse."""
    from core.agents.spec import enabled_packs, load_active_spec

    try:
        spec = await load_active_spec()
    except Exception:
        return False
    return PLUGIN_ID in enabled_packs(spec)


@router.get("", response_class=HTMLResponse)
async def payments_config_page(request: Request):
    ctx = await base_context(request, "settings")
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _require_settings_write(request, write=False):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    if not await _pack_enabled():
        return RedirectResponse("/admin/configuracion?flash=El pack de pagos no está habilitado&flash_type=danger", status_code=303)

    from core.admin.context import config_tabs
    from core.plugin_manager import plugin_manager

    configured = False
    public_key = ""
    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    if isinstance(config, PaymentsConfig):
        configured = True
        public_key = config.public_key

    ctx.update(
        tab="pagos",
        config_tabs=await config_tabs(request),
        configured=configured,
        public_key=public_key,
        webhook_url=await _wompi_webhook_url(),
        min_amount_sandbox=min_amount_cop(sandbox=True),
        min_amount_production=min_amount_cop(sandbox=False),
        flash=request.query_params.get("flash"),
        flash_type=request.query_params.get("flash_type") or "success",
    )
    return _render("pagos.html", ctx)


@router.post("")
async def payments_config_save(
    request: Request,
    public_key: str = Form(default=""),
    private_key: str = Form(default=""),
    events_secret: str = Form(default=""),
    test: str = Form(default=""),
):
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _require_settings_write(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    if not await _pack_enabled():
        return RedirectResponse("/admin/configuracion?flash=El pack de pagos no está habilitado&flash_type=danger", status_code=303)

    from core.plugin_manager import plugin_manager

    existing: PaymentsConfig | None = None
    try:
        loaded = await plugin_manager.load_config(PLUGIN_ID)
        if isinstance(loaded, PaymentsConfig):
            existing = loaded
    except Exception:
        existing = None

    def _back(flash: str, flash_type: str) -> RedirectResponse:
        return RedirectResponse(
            f"/admin/plugins/{PLUGIN_ID}/config?flash={flash}&flash_type={flash_type}",
            status_code=303,
        )

    # Campo vacío = conservar el guardado (no borrar por accidente).
    public_key = public_key.strip() or (existing.public_key if existing else "")
    private_key = private_key.strip() or (
        existing.private_key.get_secret_value() if existing else ""
    )
    events_secret = events_secret.strip() or (
        existing.events_secret.get_secret_value() if existing else ""
    )
    if not (public_key and private_key and events_secret):
        return _back("Faltan credenciales: pública, privada y secreto de eventos", "danger")

    sandbox = private_key.startswith("prv_test_")
    values = {
        "public_key": public_key,
        "private_key": private_key,
        "events_secret": events_secret,
        "sandbox": sandbox,
        "currency": existing.currency if existing else "COP",
        "link_expira_horas": existing.link_expira_horas if existing else 24,
    }

    if test:
        # "Probar y guardar": un link real por el mínimo confirma que la llave
        # privada sirve. Si falla, NO se persiste (mismo criterio que agent-sale).
        try:
            config = PaymentsConfig.model_validate(values)
            provider = build_provider(config)
            await provider.create_payment_link(
                reference="prueba-conexion",
                amount=float(provider.min_amount_cop),
                currency="COP",
                expires_in_hours=1,
            )
        except PaymentRequestRejected as exc:
            log.warning("payments_config_test_rejected", error=str(exc)[:200])
            return _back("Wompi rechazó la prueba de conexión. Verificá las llaves.", "danger")
        except Exception as exc:  # red/infra
            log.warning("payments_config_test_failed", error=type(exc).__name__)
            return _back("No se pudo validar la llave contra Wompi (revisá la conexión).", "danger")

    try:
        await plugin_manager.save_config(PLUGIN_ID, values)
    except Exception as exc:
        return _back(f"Config inválida: {str(exc)[:160]}", "danger")

    suffix = " (probada contra Wompi)" if test else ""
    return _back(f"Pasarela de pagos guardada{suffix}", "success")
