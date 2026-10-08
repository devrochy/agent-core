# ADR-0118 — Habilidades versionadas con procedimientos, consultas y plantillas de agente; instalación con scope

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-16 y HU-AOS-34 (SPEC-26). Enmienda ADR-0019 (manifiesto, habilitación por `spec.packs` y
  consentimiento *fail-open* transitorio).

## Contexto

- La habilitación de un pack vive en `AgentSpec.packs` y su configuración en `plugin_configs`, con scope de negocio.
- El consentimiento de acciones es *fail-open* cuando no hay binding (`core/orchestrator/loop.py:782`). ADR-0019 lo aceptó como transitorio y dejó escrito cuándo apretarlo a fail-closed.
- No hay versión del contenido de un pack (playbooks, flujos, descripciones) que una release pueda fijar.
- Un mismo binario no puede tener dos versiones del código de un pack.

## Decisión

Las habilidades de la plataforma publican versiones inmutables con procedimientos, consultas y plantillas; se instalan con scope y sus acciones son fail-closed por agente.

1. **`pack_versions`** es un catálogo de sistema sin `tenant_id`. Es una excepción a la invariante 1, como `plans` (ADR-0045), `connection_types` (ADR-0069) y `webhook_events` (ADR-0028). Congela el contenido de cada versión:
   - procedimientos (superpoderes);
   - consultas tipadas;
   - acciones con metadatos v2 (versión, efecto, timeout, reintento, caché, respaldo, clases de datos);
   - plantillas de agente;
   - playbooks con hash y texto;
   - schemas y escenarios de prueba.

   Solo entran las habilidades de la plataforma (`packages/pack-*`). Los plugins del negocio (ADR-0089) quedan fuera, porque el catálogo es global y lo vería cualquier negocio. `app_runtime` solo tiene SELECT; el sync del arranque escribe con un upsert `SECURITY DEFINER`, como `sync_plans()` (ADR-0045).
2. **`pack_installations`** con scope de negocio o de conexión. La de conexión gana. La configuración migra desde `plugin_configs` con el mismo cifrado.
3. **Acciones fail-closed por agente:**
   - Un agente usa las acciones de la etapa en foco, las del core, los comandos y las que su versión declara en `tools`. Sin binding no hay acción.
   - Declarar un procedimiento consiente los scopes de sus acciones.
   - El agente General que sale del lector tolerante declara en `tools` todas las acciones del flujo v2 (ADR-0113), así no pierde ninguna fuera de foco.
4. **Código = binario único** (invariante 2). Una acción con cambio rompedor se publica como `nombre_v2` y convive con la anterior hasta que ninguna release activa la use.
5. **Secretos con scope, sin tabla nueva:** viajan cifrados en `pack_installations.config_encrypted` (ADR-0027), con el scope de la instalación. Las credenciales de Meta ya son por conexión (ADR-0094).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir con `spec.packs` + `plugin_configs` | Sin scope de conexión ni versión de contenido |
| Varias versiones de código de un pack en el proceso | Imposible sin aislar imports; viola el binario único |
| Mantener el consentimiento *fail-open* | Un agente podría usar acciones de otro |
| Tabla `vault.credentials` aparte | Duplica `config_encrypted`, `purge_tenant` no la recorre (solo mira `public`) y no tiene vía de escritura |

## Consecuencias

**Positivas:** las releases fijan el contenido que probaron; cada conexión instala lo suyo; las acciones son fail-closed; los secretos siguen el scope de la instalación.

**Negativas / lo que aceptamos pagar:** sincronizar el catálogo al arrancar; acciones duplicadas por un tiempo (`_v2`); migrar `plugin_configs`; los plugins del negocio no publican procedimientos en v1.

**Qué haría que revisáramos esto:** una necesidad real de versiones de código distintas por negocio (llevaría al plugin del negocio, ADR-0089, o a la topología), o un plugin del negocio que necesite publicar procedimientos (haría falta un dueño y RLS en el catálogo).

## Impacto en el código

`core/plugin_manager.py` (sincronización de `pack_versions` e instalaciones); `core/mcp/` (metadatos v2 de acciones y permisos); `packages/pack-*/pyproject.toml` (manifiesto); migraciones.
