# PLUGIN-CONTRACT · La Biblia del desarrollador de plugins

> **SUPERSEDIDO parcialmente por ADR-0001 y ADR-0011.** El modelo de plugins embebidos
> in-process se conserva, pero dos secciones están obsoletas: el acceso a datos por
> `core.db.session.get_session()` (§13) viola la invariante #6 — los plugins recibirán solo
> `ToolContext.db` (ScopedDb con RLS, ver `packages/contracts`), y el lenguaje de referencia
> es Python (ADR-0011). El contrato canónico de tipos vive en `packages/contracts`.

Todo lo que necesitas para construir un plugin de agent-core. Si algo no está aquí,
está en el código de referencia:

- `plugins/pack_commerce/` — el plugin **completo**: 3 skills (leads, catálogo y
  ventas), esquema de datos grande (13 tablas), cron jobs, guardrail de montos y un
  **panel admin propio** con plantillas (`pack_commerce/templates/crm/`).
- `plugins/pack_calendar/` — el plugin mínimo con OAuth2 y rutas admin propias.

## 1. Qué es un plugin

Un **paquete Python** que se instala con `pip install -e` y corre **en el mismo proceso**
que el core (ver docs/MONOLITH-MODEL.md). Aporta:

- **Skills** (agrupaciones semánticas de **Tools** que el LLM puede llamar)
- **Configuración** (Pydantic → formulario auto-generado en el panel)
- **Tablas propias** (migraciones Alembic propias, opcional)
- **Cron jobs** (`@register_cron`, opcional)
- **Guardrails** (validaciones post-LLM, opcional)
- **Suscripciones a eventos** del core (opcional)
- **Rutas y menú del panel admin** (opcional)

## 2. Ciclo de vida

```
Install (pip) → Detect (scan de entry_points al arrancar)
             → Configure (formulario en /admin/plugins, JSON cifrado con Fernet)
             → Activate (migraciones propias + on_activate(host))
             → Run
             → [PluginError en runtime] → auto-disable + aviso en el panel
```

Estados en la tabla `plugins`: `detected → configured → active | disabled | error`.

## 3. Estructura mínima

```
plugins/mod_mi_plugin/
├── pyproject.toml               ← entry_point + manifest
├── alembic.ini + alembic/       ← SOLO si tiene tablas propias
└── mod_mi_plugin/
    ├── __init__.py
    ├── config.py                ← schema Pydantic
    ├── skills.py                ← BaseSkill + MCPTools
    ├── models.py                ← SQLAlchemy propio (opcional)
    ├── jobs.py                  ← @register_cron (opcional)
    └── module.py                ← BaseModule (obligatorio)
```

Genera todo con: `agent-core plugin scaffold mod-mi-plugin`

## 4. El entry_point (cómo te encuentra el core)

```toml
# pyproject.toml
[project.entry-points."agent_core.plugins"]
mod_mi_plugin = "mod_mi_plugin.module:Module"

[tool.agent_core.plugin.entities]   # manifest para el wizard de importación (opcional)
mi_tabla = ["campo1", "campo2"]
```

## 5. BaseModule (el contrato obligatorio)

```python
from fastapi import APIRouter
from core.mcp.types import BaseModule

admin_router = APIRouter(prefix="/mi-plugin", tags=["mi_plugin"])  # rutas admin propias

class Module(BaseModule):
    id = "mod_mi_plugin"          # DEBE coincidir con el entry_point
    name = "Mi Plugin"
    version = "1.0.0"
    description = "Qué hace"
    requires_config = True        # False = se activa sin formulario
    config_schema = MiConfig      # None si no hay configuración
    admin_menu = [                # entradas del menú lateral (solo mientras esté activo)
        {"label": "Mi Sección", "path": "/mi-plugin/vista"},
    ]
    admin_router = admin_router   # APIRouter propio (opcional)
    declared_skills = [MiSkill]   # para documentación SDD sin activar

    def on_activate(self, host) -> None:
        host.register_skill(MiSkill())                          # tools para el LLM
        host.register_guardrail("mi_check", self._check, 5)     # validación post-LLM
        host.subscribe(ON_CONVERSATION_STARTED, self._on_start) # eventos del core
        import mod_mi_plugin.jobs                               # registra los cron

    def on_deactivate(self) -> None:
        ...  # limpieza opcional; el host retira skills/guardrails/eventos solo
```

### 5.1 Panel admin propio (rutas + plantillas)

Si el plugin aporta UI, define un `APIRouter` con un prefijo limpio (ej. `/crm`) y
guarda tus plantillas en `<plugin>/<plugin>/templates/`. Para extender el layout del
core (menú lateral, flash), apunta Jinja2 a ambas carpetas:

```python
from pathlib import Path
from fastapi.templating import Jinja2Templates

TEMPLATES = Path(__file__).parent / "templates"
CORE_TEMPLATES = Path(__file__).resolve().parents[3] / "core" / "admin" / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES), str(CORE_TEMPLATES)])
```

Así tus plantillas pueden hacer `{% extends "base.html" %}` y reutilizar el menú.

**Menú agrupado por plugin**: el core agrupa las entradas de `admin_menu` de cada
plugin activo bajo el nombre del módulo (`plugin_manager.active_admin_menu()`), y
resalta la sección activa comparando el path de la petición con cada entrada. No hace
falta lógica extra en el plugin para eso: solo define bien `admin_menu` y las rutas.

Referencia completa de UI: `plugins/pack_commerce/pack_commerce/module.py` +
`plugins/pack_commerce/pack_commerce/templates/crm/`.

## 6. Configuración (Pydantic → formulario)

```python
from pydantic import Field, SecretStr
from core.mcp.types import BasePluginConfig

class MiConfig(BasePluginConfig):
    api_key: SecretStr = Field(..., description="Se muestra como password y se cifra")
    umbral: int = Field(default=10, description="Texto de ayuda bajo el campo")
    modo: Literal["a", "b"] = Field(default="a", description="Se renderiza como <select>")
    activo: bool = Field(default=True, description="Checkbox")
```

Reglas:
- `SecretStr` → input password. Vacío al re-guardar = conserva el valor anterior.
- Todo se persiste **cifrado con Fernet** en `plugin_configs.config_encrypted`, con
  `tenant_id` — cada tenant tiene su propia config del mismo plugin.
- `self.config` (seteado por `PluginManager.activate()`) es un atajo **solo válido
  dentro de `on_activate()`**, para el tenant que disparó la PRIMERA activación del
  plugin en este proceso. La instancia de `BaseModule` es un singleton compartido por
  todos los tenants (`mcp_host` es un registro global) — `self.config` fuera de
  `on_activate()` puede tener la config de OTRO tenant. **En handlers de tools, nunca
  uses `self.config`**: recargá con `await plugin_manager.load_config(plugin_id)`
  (tenant-scoped por `get_current_tenant_id()`) en cada llamada, como hace
  `pack_calendar/skills.py::_tenant_config()`.
- Claves con prefijo `_` (ej. `_oauth_tokens`) se persisten pero NO aparecen en el form.

## 7. Skills y Tools

```python
from core.mcp.types import BaseSkill, MCPTool, ToolContext

class MiSkill(BaseSkill):
    name = "mi_skill"
    description = "Agrupación semántica"
    keywords = ["cita", "agendar"]   # [] = siempre activa; con keywords el
                                     # SkillRouter la inyecta solo si el turno las menciona

    def get_tools(self) -> list[MCPTool]:
        return [MCPTool(
            name="mi_tool",
            description="CUÁNDO usarla y CUÁNDO NO — el LLM decide leyendo esto",
            parameters={  # JSON Schema estándar (formato MCP)
                "type": "object",
                "properties": {"x": {"type": "string", "description": "..."}},
                "required": ["x"],
            },
            handler=self.mi_tool,
        )]

    async def mi_tool(self, arguments: dict, context: ToolContext) -> dict:
        # context: conversation_id, customer_id, idempotency_key, channel
        # ¡Inyectados por el core! El LLM NUNCA los ve ni los controla.
        return {"status": "ok"}
```

Reglas de las tools:
- El handler es una **corrutina** que devuelve un dict JSON-serializable.
- **Idempotencia obligatoria en escrituras**: usa `context.idempotency_key` con un
  UNIQUE en tu tabla (ver `crear_lead` en pack_commerce).
- **El LLM propone, la tool decide**: valida SIEMPRE contra la BD, nunca confíes
  en los argumentos del modelo para datos críticos.
- Errores: deja que las excepciones suban. El core convierte cualquier excepción en
  `PluginError`, **desactiva tu plugin** y el asistente sigue vivo. Un plugin roto
  jamás tumba el asistente.

### Claves reservadas del resultado (SafeRenderer)

| Clave | Efecto |
|---|---|
| `_safe_append` | Texto adjuntado VERBATIM al final de la respuesta (el LLM no puede parafrasearlo). Para links de pago, cuentas, URLs exactas. |
| `_image_url` | Imagen adjunta a la última burbuja de WhatsApp. |

El LLM recibe el resultado SIN estas claves (no puede copiarlas mal). Ver docs/SAFE-RENDERER.md.

## 8. Memoria del core disponible para plugins

```python
from core.memory import entity as entity_memory

# Capa 2 (perfil durable, visible en el prompt de TODAS las conversaciones futuras):
await entity_memory.upsert_attributes(context.customer_id, {"email": "x@y.com"})
profile = await entity_memory.get_profile(context.customer_id)
```

## 9. Tablas propias y migraciones

- Prefija tus tablas con el plugin: `crm_leads`, no `leads` (evita colisiones).
- Usa tu propia `Base` de SQLAlchemy (NO importes la del core).
- Árbol Alembic propio con `version_table = alembic_version_<plugin_id>`:
  el plugin_manager corre `alembic upgrade head` de tu árbol al activarte.
- Referencias al core (customer_id, conversation_id): columnas UUID SIN ForeignKey
  física (FK lógica) — permite desinstalar el plugin sin romper el core.
- Al desinstalar, tus tablas QUEDAN (los datos son del negocio). El borrado es manual.

## 10. Cron jobs

```python
from core.tasks.decorators import register_cron

@register_cron("mod_mi_plugin", "0 9 * * *")   # min hora díaMes mes díaSemana (UTC)
async def mi_job() -> None:
    ...
```

- Corre en el worker Celery (proceso hermano). Se salta automáticamente si el plugin
  está inactivo.
- Mensajería proactiva: respeta la ventana de 24h de WhatsApp (solo puedes escribir
  texto libre a quien te escribió hace <24h). Ver `follow_up_leads` de pack_commerce.

## 11. Guardrails

```python
from core.mcp.types import GuardrailResult

async def mi_guardrail(response_text: str, tool_results: list[dict]) -> GuardrailResult:
    if problema_detectado:
        return GuardrailResult(
            passed=False,
            reason="qué falló",
            retry_instruction="instrucción correctiva para el reintento del LLM",
        )
    return GuardrailResult(passed=True)

# en on_activate:
host.register_guardrail("mi_check", mi_guardrail, priority=5)  # menor = primero
```

Presupuesto del core: 1 reintento; si reincide, escala a humano. Un guardrail que
lanza excepción se ignora (fail-open) y se loggea: un guardrail roto no silencia al bot.

## 12. Eventos del core

| Evento | Payload | Cuándo |
|---|---|---|
| `message.received` | conversation_id, customer_id, content | Inbound persistido |
| `message.sent` | conversation_id, content | Outbound enviado |
| `conversation.started` | conversation_id, customer_id | Primera vez del hilo |
| `conversation.closed` | conversation_id | Cierre |
| `escalation.triggered` | conversation_id, reason | Handoff creado |
| `tool.executed` | tool_name, plugin_id, conversation_id | Tool ejecutada |
| `plugin.activated` / `plugin.disabled` | plugin_id (, reason) | Ciclo de vida |

Un handler que falla se loggea y NO interrumpe el flujo.

## 13. Qué puede importar un plugin (y qué NO)

```
✓ core.mcp.types          (BaseModule, BaseSkill, MCPTool, ToolContext, GuardrailResult)
✓ core.db.session         (get_session)
✓ core.memory.entity      (upsert_attributes, get_profile)
✓ core.tasks.decorators   (register_cron)
✓ core.shared.events      (constantes de eventos)
✓ core.shared.logger / exceptions
✓ core.gateway.sender     (send_to_conversation — SOLO desde jobs, nunca desde tools)

✗ core.orchestrator.*     (el loop es del core; usa eventos y el host)
✗ core.plugin_manager     (no te auto-gestiones)
✗ core.admin.views        (aporta tu propio admin_router)
✗ Modelos de otro plugin  (los dominios no se importan entre sí; coordina vía eventos)
```

## 14. Checklist antes de publicar

- [ ] `pip install -e plugins/mod_x` sin errores y aparece en `agent-core status`
- [ ] Formulario del panel se genera y guarda (secretos como password)
- [ ] Tools con descripciones que dicen cuándo usarlas Y cuándo no
- [ ] Escrituras idempotentes con `context.idempotency_key`
- [ ] Datos sensibles por `_safe_append`, nunca en el texto del LLM
- [ ] Migraciones propias con version_table separada
- [ ] Desactivar el plugin en el panel: menú desaparece, tools desaparecen, cron se salta
- [ ] Un raise en tu tool desactiva el plugin sin tumbar el asistente
