# ADR-0173 — La plantilla habilita sus habilidades (onboarding sin «habilidad habilitada» previa)

- **Estado:** propuesto (se acepta al mergear agent-core#601).
- **Fecha:** 2026-10-27
- **Contexto:** el dueño no podía elegir el tipo de negocio en el onboarding (SPEC-41, HU-UX-21) ni crear su
  primer agente (SPEC-39). Decisión D-53 del repo de docs.
- **Enmienda:**
  - ADR-0139 §2 y la adenda del editor (2026-10-18): las `habilidades` de la plantilla dejan de ser una **puerta**
    (precondición de que la plantilla se ofrezca) y pasan a ser lo que crear el agente **habilita**. §3
    (`aplicar_agentes`, la ruta del preset) no cambia.
  - ADR-0155 §6-9: `missing_skills` cambia de significado y `will_enable` y `enabled_skills` se suman al contrato.
  - ADR-0171 (asistente de creación): `from-script` también habilita.
- **Usa:** ADR-0019 y ADR-0118 (`AgentSpec.packs` sigue siendo la fuente de verdad de la habilitación por negocio),
  ADR-0142 (tope de agentes del plan) y ADR-0047/0048 (permisos por sección).

## Contexto

Las cuatro plantillas (`ventas`, `agenda`, `cobranza`, `suscripciones`) son el «tipo de negocio» del onboarding.
Hasta hoy `GET /admin/agent-templates` calculaba `available = not faltan and bool(poderes)` con
`faltan = plantilla.habilidades − spec.packs`, y `crear_desde_plantilla` respondía 409 `template_unavailable`.

Un negocio nuevo en Free tiene `spec.packs == []`: el alta self-service llama a `onboard_tenant` sin packs ni
preset, y `apply_preset` falla en Free por `_validar_plan`. Resultado: las cuatro plantillas salían bloqueadas, el
paso 1 del recorrido no avanzaba y el onboarding a pantalla completa no tenía salida a Habilidades. Ningún ADR cubría
un Free sin packs en el onboarding.

## Decisión

1. **Elegir el tipo de negocio habilita sus habilidades.** Crear el agente desde una plantilla
   (`POST /admin/agents/from-script` y `POST /admin/agent-templates/{key}/agents`) habilita en el backend las
   `habilidades` obligatorias de la plantilla que el negocio aún no tiene. Las `opcionales` no se habilitan
   (`pack_payments` exige claves y se configura en Habilidades).
2. **`spec.packs` sigue mandando en runtime.** El filtrado por habilitación (tools, procedimientos, flujos,
   releases, menús) no cambia. Lo que sobra es pedírsela al usuario antes de poder empezar. «Habilidades» queda
   para configurar y ver la salud de lo instalado, y para activar o desactivar a mano.
3. **Un solo punto.** `core/agents/habilitacion.asegurar_habilidades(tenant_id, packs, actor)` concentra la regla:
   comprueba que el módulo esté instalado, la cuota, activa el pack con su configuración por defecto
   (`ensure_pack_activated`), guarda **una** versión de spec con la unión de packs (relee y reintenta una vez si
   perdió la carrera; si no, 409 `concurrent_edit`), siembra el flujo del pack, registra el log
   `pack_habilitado_por_plantilla` y la auditoría `skills_enabled_by_template`. Las funciones de habilitación se
   mueven de `core/admin/views.py` (capa SSR) a ese módulo, con reexports.
4. **Sin validar `min_plan`.** Igual que la ruta manual de hoy. El límite comercial real es el tope de agentes del
   plan (ADR-0142; Free = 1), que se aplica al publicar. `apply_preset` y el alta por WhatsApp conservan
   `_validar_plan`.
5. **Los packs quedan habilitados aunque se borre el agente.** Sin refcount ni limpieza: es inofensivo (no cobra ni
   expone nada que el dueño no haya elegido) y evita deshabilitar por sorpresa lo que otro agente usa.
6. **Un pack que no activa por falta de configuración se habilita igual.** Queda en `spec.packs`, no aporta tools
   mientras no esté activo, y se avisa como `habilidad_sin_configurar` (ADR-0155 §6, no bloqueante). Los cuatro
   packs de las plantillas tienen defaults en todo su `config_schema`, así que hoy activan con config vacía.
7. **Contrato (aditivo).**
   - `AgentTemplate.will_enable` = `habilidades − presentes`. `missing_skills` pasa a significar «el módulo no está
     instalado en este despliegue», y `available = not missing_skills and bool(poderes)`. Los poderes se calculan
     sobre `presentes ∪ habilidades` (`ofrecible()` es puro).
   - `FromTemplateOut.enabled_skills` y `FromScriptOut.enabled_skills`.
   - `GET /admin/agent-templates` es solo lectura: nunca habilita.
   - Errores nuevos: 403 `skills_forbidden`, 409 `concurrent_edit`, y 409 `template_unavailable` solo si falta el
     módulo. Las habilidades van en `error.details.skills`, no en `fields` (el panel pinta `fields` como error de
     un paso).
8. **Autorización.** Si `will_enable` no está vacío se exige `settings:write` **y** `plugins:write` (como
   `POST /admin/plugins/{id}/action`). Master pasa; el dueño del negocio tiene `write` en `plugins` por su rol.
   Sin permiso, 403 `skills_forbidden` sin ningún efecto («Pídele al dueño que active esta habilidad»). Si todo ya
   está habilitado, basta `settings:write`. El parámetro `puede_habilitar` del servicio es **cerrado por omisión**.
9. **Orden en `crear_desde_plantilla`:** scope → 404 → 422 `invalid_answers` → 409 `key_taken` → 403
   `skills_forbidden` → `asegurar_habilidades` → relectura de `habilitados` → derivar y crear. Todo lo que puede
   fallar va antes de habilitar; reintentar no duplica nada.
10. **Primeros pasos.** `activar_preset` no se muestra si todos los packs del preset ya están habilitados: un Free con
    un agente funcionando no debe ver «tu tipo de negocio necesita Basic».
11. **Rollout directo, sin bandera.** El cambio es aditivo; el panel y la APK nuevos contra un backend viejo dan 409,
    así que el orden de despliegue es backend, panel y APK (OTA).

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Pedir al dueño que active la habilidad antes (hoy) | Es el hueco que bloquea el onboarding; la habilitación no le dice nada a un no técnico |
| Habilitar en `GET agent-templates` | Un GET no puede escribir; habilitar sin que el dueño decida |
| Habilitar al elegir el tipo en el panel (endpoint aparte) | Dos llamadas para una intención; el agente podría crearse sin su habilidad |
| Validar `min_plan` al habilitar | Un Free no podría elegir ningún tipo; el tope de agentes ya es el límite comercial |
| Deshabilitar al borrar el agente | Requiere refcount y puede apagar lo que otro agente usa |

## Consecuencias

- Un negocio nuevo puede completar el recorrido de bienvenida sin pasar por Habilidades.
- El usuario puede tener habilitada una habilidad por la que aún no configuró nada; el aviso
  `habilidad_sin_configurar` y Habilidades lo cubren.
- Deuda conocida, fuera de este cambio: `enable_pack_for_tenant` ignora el `None` de `save_spec_version` en la ruta
  manual, `save_spec_version` no es compare-and-swap y la ruta manual no audita `skills_toggled`. El OpenAPI no
  documenta los 403/409 de las rutas de plantillas.
- Sin tablas ni migración. Rollback: revertir el PR (las habilidades ya habilitadas quedan, inofensivo), revertir el
  panel y republicar la OTA anterior.

## Impacto en el código

`core/agents/habilitacion.py` (nuevo), `core/agents/editor.py`, `core/api/routers/agent_editor.py`,
`core/admin/views.py` (reexports), `core/primeros_pasos/{reglas,calculo}.py`, `docs/openapi.json`.
