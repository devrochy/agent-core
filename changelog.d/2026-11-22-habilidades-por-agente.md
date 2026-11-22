## Las habilidades del negocio son las de sus agentes (2026-11-22, HU-AOS-91, parte C2)

- Una habilidad es del negocio solo si algún agente activo la tiene instalada. `packs_del_negocio` lee `pack_installations` (scope `agent`) y ya no lee `agent_specs.packs`. Lo usan los crons, el panel, la cuota `plugins` y el build de releases.
- Se retira el interruptor a nivel negocio.
  - `POST /api/v1/admin/plugins/{id}/action` con `activate`/`deactivate` responde **410** `business_activation_retired`. `retry` sigue igual.
  - En el panel SSR, `/admin/plugins/{id}/activar|desactivar` también responde 410 y los botones desaparecen.
  - `PUT /admin/plugins/{id}/config` guarda y activa el código, pero no habilita la habilidad.
- El alta no elige habilidades.
  - `onboard_tenant` no recibe `packs`, `agent-core tenant create` no tiene `--packs`, `POST /platform/tenants` ignora `packs` y el wizard SSR no los pide.
  - El preset solo siembra configuración, datos y persona: no habilita packs ni escribe el spec.
- Al crear un agente desde una plantilla, sus habilidades se instalan en ese agente. `asegurar_habilidades` es el chequeo previo: que el despliegue traiga el pack, la cuota y la activación del código. Los presets v2 cuentan los packs de su manifiesto, porque los instala el agente que crean.
- El negocio de prueba del tester suma el agente de Cobranza: sin él, Cobranza no sería del negocio. El escenario de suscripciones crea el agente de Suscripciones desde su plantilla.
- `/settings` filtra guardrails y tools por las habilidades del negocio.
- Los journeys instalan las habilidades de la corrida en el agente `atencion`. Mantienen los mismos playbooks y en el mismo orden.
- Antes de desplegar, correr `agent-core db packs-sin-instalacion --todos` en cada entorno y resolver lo que quede habilitado sin agente.
