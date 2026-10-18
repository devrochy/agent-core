# ADR-0110 — Presets de agente: manifiesto en el core + fragmentos por pack

- **Estado:** aceptado
- **Fecha:** 2026-10-08
- **Contexto:** HU-FWK-02 (SPEC-05, decisión D-SPEC-05-1 = B: cada pack declara sus fragmentos y el preset los
  compone). Se apoya en ADR-0109 (composición de flujos), ADR-0083 (`seed_data`) y ADR-0019 (packs por tenant).

## Contexto

Hoy cada negocio se arma a mano: `onboard_tenant` recibe una lista de packs, guarda una config vacía y
activa cada uno. Con `pack_payments` falla porque exige claves (`requires_config`). El hook
`BaseModule.seed_data(tenant_id, data)` (ADR-0083) existe pero solo lo usa el CLI con un archivo del negocio.
`min_plan` de cada pack solo se muestra en el catálogo y no se valida en el alta. Sin presets no hay alta
automática (SPEC-07, HU-SH-03) ni "un tipo de negocio con un clic".

## Decisión

Un **preset** es un manifiesto corto en el core que **referencia** packs. Cada pack declara sus propios
**fragmentos** por vertical. `apply_preset` compone las dos cosas, sin copiar nada de los packs y sin
prompts.

### 1. Manifiesto (`core/presets/<id>.json`)

| Campo | Ejemplo (`restaurante`) | Regla |
|---|---|---|
| `id`, `version` | `"restaurante"`, `1` | `version` entera; sube con cada cambio del manifiesto |
| `nombre`, `descripcion` | "Restaurante" | Para el master y el catálogo; **nunca** entra al prompt |
| `plan_minimo` | `"basic"` | Debe cubrir el `min_plan` de cada pack |
| `packs` | `["pack_commerce", "pack_payments"]` | Ids de packs instalados |
| `playbooks` | `["ventas", "pagos"]` | Solo **nombres** de playbooks de esos packs (invariante 3) |
| `persona` | `{"tone": "calido", "warmth": 4, "emojis": "sparse"}` | Campos estructurados del `AgentSpec`, no texto libre |
| `app_settings` | `{"split_style": "un_mensaje", "language": "es"}` | Solo claves conocidas de `AppSettings`/`behavior_config` |
| `conexiones_requeridas` | `["whatsapp"]` | Tipos de conexión (ADR-0072) |
| `pasos_de_activacion` | `["conectar_whatsapp", "cargar_catalogo", "configurar_pagos"]` | Claves que el panel muestra como primeros pasos |
| `golden_set` | `"restaurante-base"` | Golden set con el que se valida el preset |

El manifiesto se valida con un modelo Pydantic con `extra="forbid"`: un campo desconocido o un prompt
colado en el manifiesto es un error de carga, no un dato.

### 2. Fragmentos por pack (`packages/pack-x/presets/<vertical>.json`)

```json
{"config": {"checkout_fields": {"plan": ["nombre", "correo"]}}, "seed": {"productos": [...]}}
```

- `config`: config por defecto **sin secretos**. Se valida contra el `config_schema` del pack. Un campo
  `SecretStr` presente en un fragmento es un error.
- `seed`: datos de ejemplo. Se pasan tal cual al hook existente `seed_data(tenant_id, data)` (ADR-0083), que
  tiene que ser idempotente.
- Las **secciones de flujo** no van en el fragmento: salen del `flow.json` del pack, que el runtime compone
  siempre (ADR-0109).
- Un pack sin fragmento para esa vertical se habilita con sus defaults.

### 3. `apply_preset(tenant_id, preset_id, version=None)`

Corre en el `tenant_scope` del negocio y es idempotente:

1. **Valida**: el manifiesto, que los packs estén instalados y que el plan del negocio cubra `plan_minimo`
   y el `min_plan` de cada pack (por `sort_order` del plan).
2. **Habilita los packs** en una versión nueva del `AgentSpec` (`save_spec_if_changed`), con sus playbooks.
   Un pack que exige claves y no las tiene (`requires_config` con campos secretos obligatorios) **no se
   habilita**: queda como **pendiente de configurar**, con su paso en `pasos_de_activacion`. El alta no falla.
   Cuando el negocio carga las claves en el panel, el pack se habilita por el camino de siempre.
3. **Escribe la config por defecto** de cada pack habilitado: solo completa las claves que faltan
   (`{**fragmento, **config_existente}`); nunca pisa lo que el negocio ya cambió.
4. **Siembra** con `seed_data(tenant_id, fragmento.seed)` de cada pack habilitado.
5. **Persona y `AppSettings`**: aplica los campos del manifiesto que el negocio no haya cambiado. El flujo
   ya se compone solo (ADR-0109).
6. **Registra**: `tenants.settings["preset"] = {id, version, aplicado_en, pendientes}` (JSONB existente, sin
   migración) y `audit_log` con `action="preset_aplicado"`, los packs y los pendientes.

Volver a aplicar el mismo preset no duplica nada. Aplicar una `version` nueva repite los pasos con la misma
regla de no pisar.

**Entradas:** `agent-core tenant create --preset <id>` y el consumidor de aprovisionamiento (HU-SH-03)
llaman a la misma función. Catálogo inicial (HU-FWK-03): `comercio`, `restaurante` y `cobranza`. HU-SUB-11
agrega `planes-y-suscripciones`.

### 4. Lugar en el orden de personalización

Configuración → pack → plugin del tenant → topología de despliegue (invariante 2). Un preset es
**configuración**: combina packs existentes, defaults y datos de ejemplo, pero **no agrega código** ni
sustituye a un pack. Todo lo que pone queda editable después por los caminos normales (panel,
`AppSettings`, config del pack, secciones `tenant` del flujo). Si un negocio necesita lógica que ningún pack
tiene, el escalón siguiente es un pack o un plugin del tenant, nunca un preset.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| A · JSON completo del preset en el core (secciones, config y seeds copiados) | Duplica lo que cada pack ya sabe y envejece igual que las copias de flujo que corrigió ADR-0109. Fue la recomendación original; el cliente eligió B. |
| B puro · sin manifiesto: cada pack declara a qué verticales pertenece | No hay un lugar donde ver un preset completo, ni dónde fijar plan mínimo, persona o pasos de activación. |
| C · tabla editable por el master | Sin historial ni revisión, y un preset mal armado afectaría altas automáticas sin pasar por CI. |
| Fallar el alta si un pack exige claves | Rompe el alta automática: las claves de pago las carga el cliente después. |

## Consecuencias

**Positivas:** un tipo de negocio se aplica con una sola llamada; cada pack es dueño de sus datos y su
config por vertical; el alta automática no falla por claves faltantes; volver a aplicar es seguro.

**Negativas / lo que aceptamos pagar:** un preset nuevo requiere un release del core (manifiesto) y,
normalmente, de los packs (fragmentos). "No pisar lo que el negocio cambió" se decide por clave: si el
negocio dejó una clave igual al default anterior, una versión nueva del preset puede cambiarla.

**Qué haría que revisáramos esto:** que los presets cambien más seguido que los releases (pasaría a valer
una tabla versionada); o que un preset necesite secciones de flujo propias de la vertical que no quepan en
el `flow.json` del pack.

## Impacto en el código (HU-FWK-03)

- `core/presets/` (manifiestos, modelo `PresetManifest`, `apply_preset`).
- `packages/pack-*/presets/<vertical>.json` (fragmentos); `seed_data` implementado en `pack_commerce`.
- `core/onboarding.py` y `cli/commands/tenant.py` (`--preset`; el CLI deja de importar `pack_commerce.models`).
- Panel: primeros pasos desde `tenants.settings["preset"].pendientes` (HU-SH-06).


## Adenda (2026-10-18, HU-AOS-46): presets v2

El manifiesto gana `agentes` (keys de plantillas de agente del core) y, en un negocio con `aos_runtime = 'v3'` y
`AOS_CONNECTION_ROUTING`, `apply_preset` crea además los agentes especialistas, sus instancias, las reglas del
Coordinador y una release candidata sin activarla. Los cuatro presets pasan a `version` 2. Detalle en ADR-0139.
