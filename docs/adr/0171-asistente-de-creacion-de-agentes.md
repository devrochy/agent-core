# ADR-0171 — Asistente de creación de agentes, General de coordinación y poderes propios

- **Estado:** propuesto (se acepta al mergear la parte 1 de HU-AOS-48; los puntos 4 y 5 esperan el OK del dueño, P-34 y D-SPEC-39-5/6).
- **Fecha:** 2026-10-26
- **Contexto:** SPEC-39 (D-49, REQ-AOS-18, REQ-AOS-20 y REQ-UX-30); absorbe HU-AOS-48 de SPEC-31.
- **Enmienda:**
  - ADR-0139 §8 (la plantilla ya no deja al especialista sin enlaces de colaboración);
  - ADR-0155 (la guarda de enfoque también cubre `POST /versions`).
- **Usa:**
  - ADR-0112 (Coordinador sin tools);
  - ADR-0113 y ADR-0117 (versiones y releases);
  - ADR-0138 (desempate del enrutamiento);
  - ADR-0142 (tope de agentes publicados del plan);
  - ADR-0166 (el servidor sirve lo que el cliente pinta).

## Contexto

Crear un agente hoy es elegir una plantilla y un nombre, y completar un editor de cinco pasos técnicos (identidad, modelo, superpoderes, memoria y publicar). La APK no tiene pantalla de agentes. Además, el equipo no está bien repartido:
- **El General hereda todo.** El especialista `general` (`is_default`) sale del AgentSpec v2 con las tools y los procedimientos de todos los packs. El Coordinador (`kind=core`) no tiene tools: solo enruta.
- **Los especialistas heredan de más.** `sub_*` es de dueño `core` y llega a cualquier especialista de un negocio con proveedor de suscripciones; Cobranza hereda la familia `cob_*_suscripcion` aunque no sea el proveedor.
- **Un especialista nace aislado.** ADR-0139 §8 dejó fuera `a2a_graphs` y `agent_links`: sin enlace, el General no puede pasarle la conversación.
- **«Contratar plan» y «tomar pedido» confunden.** Son casi el mismo flujo; solo cambia si el negocio vende planes o productos.
- **El negocio nuevo no tiene General ni Coordinador.** El alta lo deja `legacy` y sin agentes; solo el master escribe en él (403 `aos_runtime_legacy`).

## Decisión

### 1. Poderes propios por agente (HU-AOS-51)

Un agente recibe solo lo suyo. La regla única vive en `herramienta_propia` (`core/presets/agentes.py`):
- **Del core:** se conservan escalar, perfil y memoria (D-SPEC-39-2). Se quita solo lo ajeno de los packs.
- **De un pack:** solo las tools del enfoque de la plantilla (`acciones_opcionales` acota por pack).
- **`sub_*`:** pertenecen al pack de Suscripciones, aunque su dueño registrado sea `core`.
- **`cob_*_suscripcion`:** solo si Cobranza es el proveedor de suscripciones del negocio (D-15, HU-SUB-15).

`POST /agents/{id}/versions` aplica la misma guarda de enfoque que `publish` (422 `superpoder_fuera_de_enfoque`). El CLI sigue sin ella: es una herramienta de operación.

### 2. Guion de preguntas en la plantilla (HU-AOS-48)

`PlantillaAgente` gana `seccion` (menú donde vive) y `preguntas[]`. Cada pregunta trae `id`, `texto`, `tipo` (opción o texto), `opciones[]` y `efecto` (qué procedimientos o ajustes activa). Reglas:
- Máximo 10 pasos entre preguntas y verificaciones.
- Solo se pregunta lo que decide algo. Catálogo, tipos de cita y cartera se verifican con `requirements[]` y se enlaza a su pantalla.
- Las frases (≥3 por superpoder) y el modelo no se preguntan: salen del pack y del proveedor del negocio. El editor actual queda como «Avanzado».
- El servidor es la fuente de verdad; el panel y la APK pintan lo que reciben (ADR-0166).

`GET /admin/agent-templates` devuelve `seccion`, `tools` y `preguntas[]`.

### 3. «Contratar plan» o «tomar pedido» según el negocio (D-SPEC-39-3)

La plantilla `ventas` no se parte. Su pregunta de tipo de negocio (productos, planes o ambos) preselecciona `tomar_pedido`, `contratar_plan` o los dos. «Planes» sin proveedor de suscripciones se ofrece con aviso (HU-SUB-09). Al crear no hay desempate (ADR-0138); este aplica solo al quitar un procedimiento de un agente ya publicado.

### 4. Crear desde el guion: `POST /admin/agents/from-script`

Cuerpo `{template_key, respuestas, name?}`. Reutiliza `crear_desde_plantilla` y la guarda de enfoque, y deja un **borrador sin publicar**. Devuelve `{agent_id, lock_version, requirements[]}`:
- **409 `key_taken`** si el negocio ya tiene el agente de esa plantilla, con el `agent_id` existente: el cliente ofrece «Editarlo».
- **422 por pregunta** si una respuesta no está en las opciones; no se crea nada.
- Los `requirements[]` no bloquean la publicación.
- Las respuestas solo afectan agentes nuevos: re-aplicar una plantilla no republica los existentes.

Publicar sigue siendo `POST /admin/agents/{id}/publish`, con el tope de agentes del plan (ADR-0142) y la conexión obligatoria (D-35).

### 5. General de coordinación y recorte progresivo (HU-AOS-52; propuesto)

El General es un agente de coordinación: core más los comandos de equipo `pasar_a_agente`, `encargar` y `consultar`. No tiene tools de pack cuando hay quien las tenga:
- **Sin especialista de un pack**, el General conserva las tools de ese pack. No se recorta a ciegas.
- **Al publicar el primer especialista de un pack**, en la misma operación:
  1. se escribe el enlace A2A General → agente (`a2a_graphs` y `agent_links`, modo `handoff`), de forma idempotente;
  2. se publica una versión nueva del General sin las tools de ese pack, por el camino de versiones con canary y reversa (HU-AOS-32).
- **Reversa:** publicar de nuevo la versión anterior del General, y quitar el enlace con `PUT /admin/a2a/graph`.
- La respuesta de `publish` suma `general_actualizado`.

Esto enmienda ADR-0139 §8: la plantilla sigue sin escribir grafos, pero publicar el especialista sí.

### 6. General y Coordinador en el alta (HU-AOS-53; propuesto, bloqueado por P-34)

`alta_negocio.py` llama a `backfill_agentes` y `asegurar_instancias_base` para que todo negocio nuevo nazca con General y Coordinador. El runtime resultante (v3 o `legacy`) y lo que ve un negocio `legacy` se deciden con P-34.

## Alternativas

| Opción | Pros | Contras |
|---|---|---|
| Recortar el General (elegida) | Un solo agente por defecto; no hay agente nuevo que mantener | Cambia el comportamiento de negocios ya publicados |
| Agente nuevo «de coordinación» | No toca el General actual | Dos agentes por defecto y confusión sobre cuál atiende |
| Quitar también escalar, perfil y memoria a los especialistas | Poderes mínimos | El especialista no puede escalar ni recordar al cliente |
| Solo ajeno de packs (elegida) | Equilibrio entre mínimo y útil | Hay que mantener la regla de dueño de `sub_*` |
| Plantilla `planes` aparte de `ventas` | Preguntas más simples | Dos plantillas casi iguales y riesgo de `key_taken` |
| Pregunta de tipo de negocio en `ventas` (elegida) | Una sola plantilla; el CRM no confunde | La plantilla trae preguntas condicionales |

**Recomendación:** recortar el General de forma progresiva por pack, conservar el core a los especialistas y resolver «planes o productos» con una pregunta en `ventas`.

## Consecuencias

- **Sin tablas ni migraciones nuevas.** `preguntas[]` y `seccion` viven en JSON de plantillas; el enlace A2A y la versión del General usan tablas existentes con su RLS.
- **`docs/openapi.json` cambia** (`AgentTemplate` y `from-script`); el panel y la APK regeneran tipos.
- **Riesgo operativo:** recortar el General cambia quién atiende. Se mitiga por pack, con especialista y enlace, con canary y sin republicar sin OK del dueño.
- **Orden de entrega:** HU-AOS-51 → 48 → 52 → 53 en el backend; luego panel (HU-AOS-54) y APK (HU-AOS-55 y 56).
- **Pendiente del dueño:** P-34, tope del plan Free, rol que ve el asistente y proveedor LLM por agente (D-SPEC-31-3).
