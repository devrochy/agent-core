## Nivel de IA por agente: Estándar y Avanzado (2026-11-22, HU-BILL-24, ADR-0210)

- **Migración `0153`** (aditiva): `agents.nivel_ia text not null default 'estandar'` con CHECK `in ('estandar','avanzado')`. `agents` ya tenía RLS forzada; no hay tabla nueva.
- **Mapa en el core** (`core/llm/niveles.py`, versionado en código): `estandar` → `deepseek-flash` / `deepseek-flash` y `avanzado` → `deepseek-v4-pro` / `deepseek-v4-pro` (`primary` / `router`). Funciones puras del nivel del plan (`features.nivel_ia`: Premium y Empresarial incluyen Avanzado) y del recorte por techo.
- **Al publicar** (`versiones.publicar`, cubre a sus siete llamadores): con la llave de la plataforma el nivel del agente fija `model.primary` y `model.router` de la versión **antes del hash** y manda sobre el `modelo` de reemplazo del editor y sobre un modelo fijado a mano. Con llave propia (proveedor y llave en `app_settings`) no aplica y se conserva el modelo del negocio. Cambiar de nivel da una versión nueva y volver al anterior reusa la que ya existía.
- **Techo del plan en el turno** (`loop.py`, solo la resolución del modelo, y `kernel/politica_modelo.decidir`): si el plan no incluye Avanzado, el turno usa los modelos de Estándar aunque la versión diga otra cosa. Recorta `primary`, `router`, `fallback` y `highRiskStage.model`. Con llave propia no hay techo. Sin suscripción o con un plan desconocido tampoco (grandfathered, como `quota_decision`). Si la lectura del plan falla, se cierra en Estándar. No toca el spec compilado ni el hash.
- **API:**
  - `PATCH /api/v1/admin/agents/{id}` acepta `nivel_ia`. Pedir `avanzado` en un plan que no lo incluye da 409 `plan_nivel_ia_not_included` con `error.details = {nivel_ia, plan}` y no escribe nada (tampoco el resto del PATCH). Elegir nivel en el Coordinador da 409 `nivel_ia_not_applicable`.
  - `AgentSummary` y `AgentDetail` traen `nivel_ia` (por defecto `estandar`).
- **El gestor no ve el modelo** (§5). Para quien no es master:
  - `GET /admin/agent-models` devuelve `items` vacío, `provider` y `business_model` en `""` y `byok_model`, `effective_model` y `effective_source` en `null`. `min_phrases` y el consumo no cambian.
  - `GET /admin/settings` devuelve `brain.model_primary`, `model_fallback` y `model_router` en `null`. `model_primary` pasa a ser `str | null` en el esquema.
  - `PUT /admin/settings/brain` ignora los modelos que mande el gestor y guarda el resto.
  - El catálogo de opciones marca `model_primary`, `model_router` y `model_fallback` como del master.
  - La nota y el aviso de publicar ya no nombran el modelo.
- **Fuera:** «≈ créditos por conversación» por nivel. El core no tiene supuestos por conversación y el número sale de la calibración de HU-BILL-29.
