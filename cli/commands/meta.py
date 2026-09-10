"""agent-core meta: diagnóstico y setup mínimo de WhatsApp Cloud API (Meta).

Meta no expone una API para crear la App ni el producto WhatsApp — eso es un
paso manual, único, en developers.facebook.com. Este comando cubre lo que SÍ
es automatizable después de ese paso: validar credenciales, ver qué números
comparten la misma cuenta de WhatsApp Business (WABA), y confirmar que la App
está suscripta al WABA (la causa más común de "verifiqué el webhook pero no
me llegan mensajes").
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import httpx
import questionary
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cli.commands.doctor import FAIL, OK, WARN

app = typer.Typer(no_args_is_help=True)
console = Console()

GRAPH_BASE = "https://graph.facebook.com/v21.0"


async def _get(client: httpx.AsyncClient, path: str, token: str, **params: str) -> tuple[bool, dict[str, Any]]:
    try:
        response = await client.get(
            f"{GRAPH_BASE}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as exc:
        return False, {"error": {"message": f"Error de red: {str(exc)[:160]}"}}
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    return response.status_code < 400, data


async def _post(client: httpx.AsyncClient, path: str, token: str, **params: str) -> tuple[bool, dict[str, Any]]:
    try:
        response = await client.post(
            f"{GRAPH_BASE}/{path}",
            params={**params, "access_token": token},
        )
    except Exception as exc:
        return False, {"error": {"message": f"Error de red: {str(exc)[:160]}"}}
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    return response.status_code < 400, data


REQUIRED_SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")


async def _run_doctor(
    phone_number_id: str, access_token: str, app_id: str, waba_id: str, apply_fix: bool
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Devuelve (filas para la tabla, acciones corregibles pendientes)."""
    rows: list[tuple[str, str, str]] = []
    fixable: list[tuple[str, str]] = []  # (descripción, waba_id) para --fix

    async with httpx.AsyncClient(timeout=15) as client:
        # ---- 1. Token: tipo, vigencia y scopes (vía debug_token, self-inspección)
        ok, dbg = await _get(client, "debug_token", access_token, input_token=access_token)
        token_data = dbg.get("data", {}) if ok else {}
        if not ok or not token_data.get("is_valid"):
            rows.append(("Token", FAIL, dbg.get("error", {}).get("message", "Token inválido o expirado")))
            return rows, fixable
        if not app_id:
            app_id = str(token_data.get("app_id", ""))
        scopes = set(token_data.get("scopes", []))
        missing_scopes = [s for s in REQUIRED_SCOPES if s not in scopes]
        token_type = token_data.get("type", "?")
        rows.append((
            "Token", OK if not missing_scopes else FAIL,
            f"tipo {token_type}, app {token_data.get('application', '?')} ({app_id})"
            + (f" — faltan scopes: {', '.join(missing_scopes)}" if missing_scopes else ""),
        ))
        if token_type == "USER":
            rows.append((
                "Tipo de token", WARN,
                "Es un token de usuario: probablemente temporal (~24h). Para pruebas repetidas "
                "conviene un system user token de larga duración (Business Settings → System Users).",
            ))

        # ---- 2. Número: existe, calidad y límite de envío
        ok, data = await _get(
            client,
            phone_number_id,
            access_token,
            fields="id,verified_name,display_phone_number,quality_rating,"
            "messaging_limit_tier,code_verification_status",
        )
        if not ok:
            msg = data.get("error", {}).get("message", "credenciales inválidas")
            rows.append(("Número de WhatsApp", FAIL, f"Meta rechazó la consulta: {msg}"))
            return rows, fixable

        rows.append((
            "Número de WhatsApp", OK,
            f"{data.get('display_phone_number', '?')} — {data.get('verified_name', 'sin nombre verificado')}",
        ))
        rows.append((
            "Verificación del número", OK if data.get("code_verification_status") == "VERIFIED" else WARN,
            str(data.get("code_verification_status", "desconocido")),
        ))
        rows.append((
            "Límite de envío (tier)", OK,
            f"{data.get('messaging_limit_tier', 'desconocido')} · calidad: {data.get('quality_rating', '?')}",
        ))

        if not waba_id:
            rows.append((
                "Cuenta de WhatsApp Business (WABA)", WARN,
                "No se puede derivar del número (Meta no expone esa relación por API). Pasá "
                "--waba-id (Meta Business Settings → Cuentas de WhatsApp, o WhatsApp Manager) "
                "para chequear números hermanos y suscripción de la app.",
            ))
            return rows, fixable

        # ---- 3. Otros números bajo la MISMA cuenta (para evitar el lío de "¿cuál es cuál?")
        ok, numbers_data = await _get(
            client, f"{waba_id}/phone_numbers", access_token, fields="id,display_phone_number,verified_name"
        )
        if ok:
            numbers = numbers_data.get("data", [])
            if len(numbers) > 1:
                listado = ", ".join(
                    f"{n.get('display_phone_number', '?')} ({n.get('id')})" for n in numbers
                )
                rows.append((
                    "Números en esta cuenta (WABA)", WARN,
                    f"Hay {len(numbers)} números bajo la misma cuenta: {listado}. "
                    "Confirmá cuál es el que estás probando.",
                ))
            else:
                rows.append(("Números en esta cuenta (WABA)", OK, "Solo este número."))
        else:
            rows.append(("Números en esta cuenta (WABA)", WARN, "No se pudo listar (permiso insuficiente)."))

        # ---- 3. App(s) suscriptas al WABA: sin esto, el webhook no recibe nada
        ok, subs_data = await _get(client, f"{waba_id}/subscribed_apps", access_token)
        if not ok:
            rows.append((
                "App suscripta al WABA", WARN,
                "No se pudo consultar (falta whatsapp_business_management en el token).",
            ))
        else:
            subs = subs_data.get("data", [])
            sub_ids = {str(s.get("whatsapp_business_api_data", {}).get("id", "")) for s in subs}
            if not subs:
                rows.append((
                    "App suscripta al WABA", FAIL,
                    "Ninguna app está suscripta a este WABA: por eso el webhook nunca recibe "
                    "mensajes aunque el handshake haya sido exitoso.",
                ))
                fixable.append((f"Suscribir la app al WABA {waba_id}", waba_id))
            elif app_id and app_id not in sub_ids:
                rows.append((
                    "App suscripta al WABA", FAIL,
                    f"Hay apps suscriptas ({', '.join(sub_ids)}) pero no la tuya ({app_id}).",
                ))
                fixable.append((f"Suscribir la app {app_id} al WABA {waba_id}", waba_id))
            else:
                rows.append(("App suscripta al WABA", OK, f"{len(subs)} app(s) suscripta(s): {', '.join(sub_ids)}"))

        if apply_fix and fixable:
            for descripcion, target_waba in fixable:
                confirmed = await questionary.confirm(
                    f"¿{descripcion}? (llama a la API de Meta ahora)", default=False
                ).ask_async()
                if confirmed:
                    ok, result = await _post(client, f"{target_waba}/subscribed_apps", access_token)
                    if ok:
                        rows.append((descripcion, OK, "Corregido."))
                    else:
                        rows.append((
                            descripcion, FAIL,
                            result.get("error", {}).get("message", "Meta rechazó la corrección"),
                        ))

    return rows, fixable


def _render_rows(title: str, rows: list[tuple[str, str, str]]) -> None:
    table = Table(title=title, show_lines=False)
    table.add_column("Chequeo", style="bold")
    table.add_column("Estado", justify="center")
    table.add_column("Detalle", overflow="fold")
    for name, status, detail in rows:
        table.add_row(name, status, detail)
    console.print(table)


@app.command()
def doctor(
    phone_number_id: str = typer.Option(..., "--phone-number-id", help="phone_number_id de Meta a diagnosticar"),
    access_token: str = typer.Option(..., "--access-token", help="Access token (temporal o de system user)"),
    app_id: str = typer.Option(
        "", "--app-id", help="App ID propio (si no se pasa, se lee del token vía debug_token)"
    ),
    waba_id: str = typer.Option(
        "", "--waba-id",
        help="ID de la cuenta de WhatsApp Business (Meta Business Settings → Cuentas de WhatsApp). "
        "Sin esto no se puede chequear números hermanos ni suscripción de la app.",
    ),
    fix: bool = typer.Option(
        False, "--fix", help="Ofrece corregir lo corregible (con confirmación) en vez de solo reportar"
    ),
) -> None:
    """Diagnostica el setup de WhatsApp Cloud API mínimo para hacer pruebas.

    No crea la App de Meta (eso es manual, una vez, en developers.facebook.com).
    Valida lo que sí se puede automatizar: token (tipo, scopes, vigencia), número,
    WABA compartido entre números, y suscripción de la app al WABA (causa más
    común de "no me llegan mensajes" pese a que el webhook verificó bien).
    """
    rows, fixable = asyncio.run(_run_doctor(phone_number_id, access_token, app_id, waba_id, fix))
    _render_rows("agent-core meta doctor", rows)

    if fixable and not fix:
        console.print(
            f"\n[yellow]{len(fixable)} cosa(s) corregible(s) encontrada(s).[/yellow] "
            "Repetí con --fix para que te pregunte si las corrijo."
        )


CHECKLIST_MANUAL = """\
Esto lo hace Meta a mano, una vez — no hay API para saltárselo:

  1. developers.facebook.com → Mis Apps → Crear App → tipo "Negocios".
  2. Agregar el producto [bold]WhatsApp[/bold]: Meta provisiona automáticamente
     un número y una cuenta de WhatsApp Business (WABA) de prueba.
  3. En WhatsApp → Configuración de la API, anotar:
     - [bold]App ID[/bold] y [bold]App Secret[/bold] (Configuración básica de la app)
     - [bold]access token temporal[/bold] y [bold]phone_number_id[/bold]
     - [bold]WABA ID[/bold] (arriba, "ID de la cuenta de WhatsApp Business")
  4. (Opcional, para el token de larga duración) un [bold]Business ID[/bold] y un
     token de administrador del Business Manager con permiso
     [bold]business_management[/bold] — Business Settings → Usuarios del sistema,
     o Graph API Explorer pidiendo ese scope.
"""


async def _find_or_create_system_user(
    client: httpx.AsyncClient, business_id: str, admin_token: str, name: str
) -> tuple[bool, str, str]:
    """Busca un system user con este nombre; si no existe, lo crea. (ok, id, mensaje)."""
    ok, data = await _get(client, f"{business_id}/system_users", admin_token, fields="id,name")
    if ok:
        for su in data.get("data", []):
            if su.get("name") == name:
                return True, str(su["id"]), "reutilizado"
    ok, data = await _post(client, f"{business_id}/system_users", admin_token, name=name, role="ADMIN")
    if not ok:
        return False, "", data.get("error", {}).get("message", "Meta rechazó la creación del system user")
    return True, str(data.get("id", "")), "creado"


async def _assign_asset(
    client: httpx.AsyncClient, asset_id: str, system_user_id: str, admin_token: str
) -> tuple[bool, str]:
    ok, data = await _post(
        client, f"{asset_id}/assigned_users", admin_token, user=system_user_id, tasks='["MANAGE"]'
    )
    return ok, "" if ok else data.get("error", {}).get("message", "Meta rechazó la asignación")


async def _setup_long_lived_token(
    business_id: str, admin_token: str, app_id: str, waba_id: str, slug: str
) -> tuple[list[tuple[str, str, str]], str | None]:
    """Genera (o reutiliza) un token de larga duración vía system user. No persiste el admin_token."""
    rows: list[tuple[str, str, str]] = []
    su_name = f"agent-core-{slug}"

    async with httpx.AsyncClient(timeout=15) as client:
        ok, su_id, msg = await _find_or_create_system_user(client, business_id, admin_token, su_name)
        if not ok:
            rows.append(("System user", FAIL, msg))
            return rows, None
        rows.append(("System user", OK, f"{su_name} ({su_id}) — {msg}"))

        ok, msg = await _assign_asset(client, waba_id, su_id, admin_token)
        rows.append(("Asignar WABA al system user", OK if ok else FAIL, msg or "asignado"))
        if not ok:
            return rows, None

        ok, msg = await _assign_asset(client, app_id, su_id, admin_token)
        rows.append(("Asignar App al system user", OK if ok else FAIL, msg or "asignado"))
        if not ok:
            return rows, None

        ok, data = await _post(
            client, f"{su_id}/access_tokens", admin_token,
            app_id=app_id, scope=",".join(REQUIRED_SCOPES),
        )
        if not ok:
            rows.append(("Generar token", FAIL, data.get("error", {}).get("message", "Meta rechazó el token")))
            return rows, None
        token = str(data.get("access_token", ""))
        rows.append(("Generar token", OK, "Token de larga duración generado."))
        return rows, token


async def _configure_flow(slug: str, skip_intro: bool) -> None:
    from cli.tenant_ref import resolve_tenant

    try:
        tid = await resolve_tenant(slug)
    except typer.BadParameter:
        crear = await questionary.confirm(
            f"El tenant '{slug}' no existe. ¿Crearlo ahora?", default=True
        ).ask_async()
        if not crear:
            raise typer.Exit(1) from None
        tid = await _create_minimal_tenant(slug)

    if not skip_intro:
        console.print(Panel(CHECKLIST_MANUAL, title="1) Paso manual en Meta (una vez)", border_style="cyan"))

    app_id = (await questionary.text("App ID (Enter = detectarlo del token):").ask_async()) or ""
    app_secret = await questionary.text("App Secret:").ask_async()
    access_token = await questionary.text("Access token (temporal está bien):").ask_async()
    phone_number_id = await questionary.text("phone_number_id:").ask_async()
    waba_id = await questionary.text("WABA ID:").ask_async()

    if await questionary.confirm(
        "¿Tenés Business ID + token de administrador para generar un token de larga duración?",
        default=False,
    ).ask_async():
        business_id = await questionary.text("Business ID:").ask_async()
        admin_token = await questionary.password("Token de administrador (no se guarda):").ask_async()
        rows, new_token = await _setup_long_lived_token(business_id, admin_token, app_id or "", waba_id, slug)
        _render_rows("2) Token de larga duración", rows)
        if new_token:
            access_token = new_token
            console.print("[green]Usando el token de larga duración recién generado.[/green]")
        else:
            console.print("[yellow]Sigo con el access token temporal que ya diste.[/yellow]")

    rows, fixable = await _run_doctor(phone_number_id, access_token, app_id, waba_id, apply_fix=True)
    _render_rows("3) Diagnóstico", rows)
    if fixable:
        console.print("[yellow]Quedó algo sin corregir arriba — revisá el detalle antes de seguir.[/yellow]")

    numero_ok = any(name == "Número de WhatsApp" and status == OK for name, status, _ in rows)
    if not numero_ok:
        seguir = await questionary.confirm(
            "El número no pasó la validación de Meta (ver tabla arriba). "
            "¿Guardar la conexión igual?", default=False,
        ).ask_async()
        if not seguir:
            console.print("[yellow]Cancelado: no se guardó ninguna conexión.[/yellow]")
            return

    verify_token = (
        await questionary.text(
            "Verify token (Enter = generar uno):"
        ).ask_async()
    ) or secrets.token_urlsafe(16)

    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await save_connection(
            label=f"WhatsApp ({slug})",
            credentials={
                "phone_number_id": phone_number_id,
                "app_secret": app_secret,
                "access_token": access_token,
                "verify_token": verify_token,
            },
        )
    console.print(f"[green]✓ Conexión guardada para el tenant '{slug}'.[/green]")

    from cli.commands.tenant import print_webhook_panel

    print_webhook_panel(slug, verify_token)


async def _create_minimal_tenant(slug: str):
    from core.onboarding import onboard_tenant

    name = await questionary.text("Nombre del negocio:", default=slug).ask_async()
    business = await questionary.text("Descripción del negocio:").ask_async()
    tid, _steps = await onboard_tenant(
        name=name,
        slug=slug,
        business_description=business or "",
        language="es",
        persona={"role": "asistente", "tone": "cercano", "locale": "es-ES"},
        escalation_triggers=["asesor", "humano", "persona real", "reclamo", "queja"],
        packs=[],
    )
    console.print(f"[green]✓ Tenant '{slug}' creado.[/green]")
    return tid


@app.command()
def configure(
    slug: str = typer.Argument(..., help="Slug del tenant (se crea si no existe)"),
    skip_intro: bool = typer.Option(False, "--skip-intro", help="No repetir el checklist manual de Meta"),
) -> None:
    """Wizard: valida, genera credenciales y conecta WhatsApp de un tenant.

    No crea la App de Meta (paso manual, una vez). Sí automatiza todo lo demás:
    token de larga duración (system user), corrección de la suscripción al WABA,
    y guardar la conexión cifrada del tenant — reusa `meta doctor` y
    `core.gateway.connections.save_connection`.
    """
    asyncio.run(_configure_flow(slug, skip_intro))
