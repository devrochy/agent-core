# ADR-0190 — Las habilidades se instalan y configuran por agente; el pack de especialidad define el tipo de agente

- **Estado:** propuesto (se acepta al mergear el tren 3 de [SPEC-44](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-44-habilidades-por-agente/tasks.md), cuando el runtime lea las habilidades del agente).
- **Fecha:** 2026-11-12
- **Contexto:** [SPEC-44](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-44-habilidades-por-agente/spec.md) ([D-68](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/decisiones.md#d-68--las-habilidades-son-del-agente-comercio-agenda-cobranza-y-suscripciones-son-tipos-de-agente), REQ-AOS-24).
- **Usa:** D-35 (se venden agentes), ADR-0004 (tablas por pack con prefijo), ADR-0019 (habilitación por tenant, que este ADR lleva al agente), ADR-0117 (releases y caché por versión), ADR-0145 (conocimiento por agente, el precedente), ADR-0171 «asistente de creación de agentes» (el General pierde las acciones del especialista; hay otro ADR-0171, el del resumen por agente, que no aplica) y las invariantes 1 (multi-tenant con RLS), 4 (contratos solo por ADR), 5 (un pack no importa de otro) y 8 (sin borrados físicos).
- **Nota de estado:** ADR-0193 (propuesto, 2026-11-13): el modelo de leads y las tools de Captar leads pasan al core (D-70); enmienda la decisión 1 y la alternativa `pack_leads`.

## ADRs que reemplaza o enmienda

| ADR | Relación | Qué cambia |
|---|---|---|
| **ADR-0173** (la plantilla habilita sus habilidades) | **Reemplaza** | Crear un agente desde una plantilla instala las habilidades **en ese agente**, no en el negocio. `asegurar_habilidades` sigue siendo la única puerta |
| **ADR-0155** (superpoderes por enfoque) | **Enmienda** | El enfoque y los requisitos salen del pack de especialidad (el cambio que el ADR dejó previsto para [HU-AOS-16](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-26-agent-os/hu/HU-AOS-16-habilidades-con-procedimientos-y-scope.md) parte 2). `PlantillaAgente` queda como vista derivada |
| **ADR-0118** §2 y §5 (habilidades por scope) | **Enmienda** | Scope nuevo `agent`; `enabled_packs` lee `pack_installations` (la deuda del §2); precedencia, cifrado y crons por agente (ver Decisión 2 a 4) |
| **ADR-0067** (capacidad de pagos del core) | **Enmienda** | `capability_for` y `create_link` reciben el agente; la cuenta Wompi sale de la instalación del agente; `core/billing/enrutador.py` resuelve el agente por la transacción |
| ADR-0138 (enrutamiento) | Nombra | Quitarle al General las acciones de un especialista cambia el desempate; se revisa con el replay |
| ADR-0157 (avisos), ADR-0166/0182 (Configuración), ADR-0172 (opciones de plantillas), ADR-0139 (plantillas del core) | Nombra | Los avisos de configuración salen de la instalación del agente; «Herramientas del agente» deja Configuración; el lint de opciones usa los bindings del agente de la conexión |
| ADR-0142 (planes por agentes) | Sin cambio | Los planes ya no limitan habilidades; se borra el código muerto `can_activate_plugin` |

Ningún ADR aceptado se edita: al aceptarse este, cada uno recibe una nota de estado.

## Contexto

Ver el «Problema» de [SPEC-44](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-44-habilidades-por-agente/spec.md#problema). En una frase: D-35 dice que la habilidad es del agente, pero `spec.packs`, `plugin_configs` y `payment_registry` solo conocen al negocio, y el panel muestra lo mismo en tres lugares.

## Decisión

1. **Dos clases de pack.** `BaseModule.tipo: Literal["especialidad", "transversal"]`.
   - **Especialidad** (`pack_commerce` → Ventas, `pack_calendar` → Agenda, `pack_cobranza` → Cobranza, `pack_subscriptions` → Suscripciones) define un tipo de agente: sus habilidades obligatorias y opcionales, procedimientos y playbooks, que hoy están en `core/presets/plantillas/*.json`.
   - **Transversal** (`pack_payments`, `pack_knowledge`) se instala en uno o varios agentes.
   - **Captar leads** no es un pack: es el grupo de acciones `leads`, que se enciende por agente. Fuera de un agente de ventas pasa por `core/admin/lead_registry.py`, así ningún pack importa el modelo `Lead` de commerce.
2. **Instalación por agente.** `pack_installations` suma `scope='agent'` y `agent_id` (FK a `agents`, nula en los otros scopes), con índice único parcial `(tenant_id, agent_id, pack_id) WHERE scope = 'agent' AND deleted_at IS NULL` y RLS forzada. **Precedencia: conexión › agente › negocio.** El subconjunto congelado por release de una conexión sigue mandando. El agente en foco usa solo las habilidades de sus instalaciones (la release las congela por componente). `spec.packs` del negocio **no se retira**: queda como la unión de las habilidades de sus agentes (instalar en un agente habilita en el negocio con `asegurar_habilidades`), porque la leen ~60 lectores fuera de la conversación (crons, panel, `payment_registry`). Un agente sin instalaciones propias hereda las del negocio hasta la resiembra.
3. **Configuración partida.** Cada pack declara `config_negocio` y `config_agente`. La del agente vive en `pack_installations.config_encrypted`, que ya existe y va cifrada atada a `(tenant_id, installation_id)`. La del negocio sigue en `plugin_configs`. Va al **negocio** todo campo que lea un proceso sin conversación sin saber el agente, que deba ser único en el negocio o que comparta otro pack; va al **agente** el comportamiento en la conversación. El inventario mostró que eso deja en el agente unos 20 campos (mensajes, umbrales y guardrails de la conversación) y el resto en el negocio; el reparto campo por campo está en [design.md](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-44-habilidades-por-agente/design.md). Dentro de la conversación, `load_config(tenant, pack, agent_id)` combina ambas y la del agente pisa en sus campos.
4. **Crons y procesos sin conversación.** Corren por instalación activa. Un agente archivado no corre los crons de sus instalaciones. Los registros que cobran o avisan fuera de la conversación (suscripción, obligación de cobranza, transacción de pago) guardan el `agent_id` obligatorio.
5. **La release congela la referencia, no la configuración.** El manifiesto lleva el id de la instalación del agente y nunca la configuración ni llaves. Rotar una llave no pide una release nueva.
6. **Cuentas Wompi.** Tabla `payments_accounts` del pack (ADR-0004): `tenant_id`, nombre, llaves y secreto de eventos cifrados, `sandbox`, `link_expira_horas` y el mínimo de pago en línea (hoy `pago_en_linea_monto_minimo` de commerce), borrado lógico y RLS forzada. La moneda sigue siendo del negocio. La vigencia y el mínimo van con la cuenta porque también los usan crons y panel sin agente. La instalación de Pagos en un agente guarda solo `cuenta_id`. Una cuenta usada por un agente no se borra. **Webhook por cuenta:** `/webhooks/payments/wompi/{slug}/{cuenta_id}`, con la firma validada por el secreto de esa cuenta. La URL por negocio se elimina. `PaymentTransaction` suma `cuenta_id` y `agent_id`, obligatorios.
7. **Contrato de pagos.** `payment_registry.capability_for(agente)` y `PaymentCapability.create_link(..., agente)`: la cuenta sale de la instalación del agente. Los consumidores (commerce, suscripciones, cobranza y el enrutador de facturación) pasan el agente. Sin Pagos instalado, el consumidor cae a transferencia como hoy.
8. **Grupos de acciones.** Cada ficha de tool declara solo la clave `grupo`; la etiqueta la pone el panel (criterio de ADR-0155 §8). El control por acción es el `ToolBindingV3` que ya existe (`enabled`, `maxCallsPerConversation`, `requiresConfirmation`). `/admin/settings/skills` pasa a ser solo el valor por defecto para agentes nuevos.
9. **API del agente.** `GET /admin/agents/{id}/acciones` (agrupadas, con binding) y `GET/PUT /admin/agents/{id}/habilidades` (instalar, quitar y configurar transversales; secretos nunca en claro). `/admin/agent-templates` conserva su forma.
10. **Sin migración de datos.** La data de test y de producción no es real (D-68). El cambio entra sin doble escritura ni bandera, y los entornos se borran y se resiembran.

## Consecuencias

- **Positivas.** D-35 se cumple en el código. La ficha del agente dice la verdad sobre lo que hace. Dos agentes del mismo tipo pueden comportarse distinto. Pagos sirve a varios agentes con cuentas distintas. Se salda la deuda de ADR-0118 §2, y las opciones de plantillas validan contra el agente correcto.
- **Negativas.**
  - Refactor grande en core, seis packs, pagos, panel y APK.
  - Los hashes de los seis packs cambian y hay que reconstruir las releases.
  - Las fases 1 a 3 entran juntas por una rama de integración.
  - Test y producción se borran y se resiembran.
- **Seguridad del dato.**
  - Las llaves de Wompi viven solo en `payments_accounts`, cifradas.
  - Ningún manifiesto lleva secretos.
  - El webhook por cuenta impide confirmar un pago con el secreto de otra cuenta.
  - Antes de borrar producción se exportan los leads de la landing del negocio `solvershub`.
- **Observabilidad.** Los logs de pago llevan `cuenta_id` y `agent_id`, nunca llaves. Hay un aviso cuando un agente cobra en línea sin cuenta elegida.
- **Qué haría que revisáramos esto:**
  - Un cliente que necesite catálogo o pedidos separados por agente.
  - Un segundo proveedor de pago que no encaje en «cuenta del negocio que el agente elige».
  - Que el soporte muestre que los dueños no entienden la diferencia entre habilidad y tipo de agente.

## Alternativas consideradas

- **Mejorar solo los nombres del panel y dejar el modelo por negocio.** Rechazada (D-68, opción B): la configuración seguiría siendo una por negocio.
- **Llaves de Wompi escritas en cada agente.** Rechazada por el dueño: rotar sería N ediciones y las copias quedarían desactualizadas.
- **Un pack `pack_leads`.** Rechazada: el modelo `Lead` vive en commerce y sacarlo rompe la invariante 5 o duplica el mapeo de una tabla.
- **Columna `agent_id` en `option_templates`.** Rechazada: la plantilla de Meta es de la conexión; basta con validar contra el agente que la atiende.
- **Expand/contract con doble escritura y bandera.** Rechazada: no hay data real que proteger.

## Impacto en el código

- `core/db/models.py` (`PackInstallationRecord`) y una migración del core; `packages/pack-payments` (alembic, `config.py`, `module.py::wompi_webhook`, `payments.py`).
- `core/agents/spec.py`, `core/agents/habilitacion.py`, `core/plugin_manager.py`, `core/mcp/types.py::BaseModule`.
- `core/orchestrator/loop.py`, `core/kernel/oferta.py`, `core/releases/build.py`, `core/payments/__init__.py`, `core/billing/enrutador.py`.
- `core/presets/plantillas/*.json` y `core/presets/__init__.py::PlantillaAgente`, `core/agents/requisitos.py`, `core/primeros_pasos/reglas.py`, `core/agents/editor.py`.
- `core/kernel/opciones/lint.py`, `core/admin/plantillas_opciones_service.py`.
- Seis packs: `tipo`, esquemas partidos, `grupo` en las fichas, versión nueva y `packages/pack-versions.lock.json`.
