---
name: onboard-tenant
description: >
  Da de alta un cliente nuevo en agent-core: artefactos en TENANTS_ROOT, tenant y packs,
  agente, canal WhatsApp, evals y publicación. Usar SOLO cuando el usuario diga "nuevo
  cliente", "dar de alta a", "onboarding de" o "conectar el WhatsApp de <negocio>".
argument-hint: "<nombre del negocio>"
---

Onboarding de `$ARGUMENTS`. Ejecuta en orden y reporta al final el tiempo total, lo que
quedó configurado y lo pendiente.

**El alta es configuración, nunca código** (invariante 2). Si algún paso exige escribir
código, no es onboarding: es un pack incompleto. Anótalo como deuda en la HU que
corresponda de `solvershub-docs` y dilo explícitamente en el reporte.

## 0. Datos y entorno

Pedir al usuario: nombre, slug, descripción del negocio, vertical y packs, idioma, tono,
email del owner, plan y **entorno** (test o producción; producción solo con su
confirmación explícita).

Las credenciales (tokens de Meta, llaves de Wompi, contraseñas) **nunca** se escriben en
el chat ni en un comando de la sesión: el usuario las carga en el panel o corre él mismo
el comando.

## 1. Artefactos en `TENANTS_ROOT/<slug>/`

Se preparan **antes** de crear el tenant, porque el alta corre el seed. Están fuera del
repo (gitignored, `docs/MULTITENANT.md`):

```
tenants/<slug>/
  catalog.json        # catálogo de commerce ("pack": "pack_commerce")
  <plugin_id>.json    # seed por pack vía BaseModule.seed_data
  docs/voz-de-marca.md  # → brand_voice (obligatoria)
  playbooks/<n>.md    # override de un playbook ya permitido (ADR-0026), opcional
  evals/              # golden set del tenant: spec.json + scenarios/
```

- Hoy **ningún pack implementa `seed_data`**: solo se importa `catalog.json`. Las
  `<plugin_id>.json` empiezan a servir con HU-FWK-03 / HU-SUB-11.
- En los entornos de Coolify, `TENANTS_ROOT` es `/app/tenants` y tiene que estar en un
  volumen persistente (ADR-0085). Sin volumen, los artefactos se pierden en el próximo
  deploy.

## 2. Tenant y packs

```bash
agent-core tenant create -y --name "<Nombre>" --slug <slug> \
  --business "<descripción>" --language es --tone cercano \
  --packs pack_commerce,pack_calendar --admin-email <owner@negocio>
```

- Sin `--admin-password`: el owner entra por **invitación** (ADR-0081, Colaboradores en
  el panel). Una contraseña en la línea de comandos queda en el historial.
- Sin `--wa-*`: WhatsApp se conecta en el paso 4 desde el panel.
- **No** incluir `pack_payments` hasta cargar las llaves de Wompi: el alta falla con
  packs que exigen llaves (HU-FWK-03). Se habilita en el paso 5.
- El alta corre el seed: catálogo, seeds de packs y `brand_voice`. Para repetirlo:
  `agent-core tenant seed <slug>`. Ojo: un re-seed pisa las secciones de flujo propias
  etiquetadas con un pack (HU-FWK-01).
- Equivalente en el panel: master → Tenants → nuevo.
- El resumen del CLI todavía dice "Canary 5 %": está desactualizado (ver paso 7).

## 3. Plan

Master asigna el plan del tenant (`tenant_subscriptions`). Solo Free tiene prueba de 30
días; los planes pagos se cobran desde el primer día (D-16).

## 4. Canal WhatsApp

- **BYOK (hoy)**: panel → Conexiones → asistente de WhatsApp. El usuario carga
  `phone_number_id`, la WABA, el token y el app secret.
- `agent-core tenant webhook <slug>` imprime la Callback URL y el verify token para
  registrar en Meta (campo `messages`).
- Verificar con un mensaje real desde un número de prueba **antes** de seguir.
- **Embedded Signup todavía no existe** (HU-SH-05): no prometerlo. BYOK de producción
  (entorno, PIN, promoción) llega con HU-CONN-06; revisar el checklist de producción del
  tenant.

## 5. Agente y packs

Todo desde el panel, nada en código:
- **Configuración**: identidad, modelo, tono, `split_style` (`un_mensaje` recomendado),
  disparadores de escalamiento, horario y bindings con `requiresConfirmation` para tools
  irreversibles (ADR-0050).
- **Habilidades**: la config de cada pack se valida contra su schema. Llaves de Wompi
  (pagos BYOK) → recién ahí habilitar `pack_payments`.

## 6. Evals del tenant

Al menos 10 conversaciones representativas de **este** cliente en
`TENANTS_ROOT/<slug>/evals/` (skill `eval-author`), con los journeys de su vertical:

```bash
uv run python evals/runner.py --tenant <slug>
```

No se publica sin pasar. Meta del plan: ≥95 %.

## 7. Prueba y publicación

- "Probar asistente" (playground) **ejecuta tools con efectos reales** (HU-ARCH-14):
  usarlo solo con datos de prueba.
- Publicar la versión del AgentSpec: `agent-core agent list <slug>` y
  `agent-core agent activate <versión> <slug>`. El rollback es un paso:
  `agent-core agent rollback <slug>`. Ese es el "canary" de ADR-0016 (gate + rollback),
  no un reparto de tráfico.
- Invitar al owner (Colaboradores) y entregar: acceso al panel, cómo escalar a humano y
  cómo pedir cambios.
- Revisión a las 24 h: escalamientos, costo por conversación, errores de tool.

## Cuando lleguen las HU

- Presets (HU-FWK-03): reemplazan los pasos 1, 2 y 5 por `--preset`.
- Embedded Signup (HU-SH-05): reemplaza el paso 4.
- Aprovisionamiento automático (HU-SH-03): el alta de clientes de SolversHub deja de ser
  manual.

Actualizar esta skill en la misma HU que cambie el proceso.
