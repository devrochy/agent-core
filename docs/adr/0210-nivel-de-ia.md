# ADR-0210 — Nivel de IA: el negocio elige un nivel y el core lo traduce al modelo

- **Estado:** propuesto (HU-BILL-17, 2026-11-22).
- **Fecha:** 2026-11-22
- **Contexto:** D-79 y SPEC-51 (`solvershub-docs`, #291), HU-BILL-24.
  **Usa:** invariante 4 de `CLAUDE.md` (contratos congelados), ADR-0050 (`model.primary`/`fallback`/`router`), HU-AOS-79
  (`modelo` de la versión, `core/agents/versiones.py:517`), ADR-0208 (el gasto real se cobra en créditos) y
  D-22 (modelo por defecto de la llave de plataforma).

## Contexto

Hoy el modelo del agente es `model.primary` de `AgentModelV3` (`packages/contracts/agentcore_contracts/agentspec.py:101`), un texto con el nombre del modelo. Por defecto es `LLM_MODEL = deepseek-flash` (`core/config.py:142`). `core/llm/difficulty_router.py` decide si activa el razonamiento de ese modelo.

D-79 pide tres cosas:
- que Premium y Empresarial puedan elegir un agente «más inteligente»;
- que el negocio elija **solo el nivel**, sin saber qué modelo corre por debajo;
- que más adelante se pueda sumar otro proveedor, además de DeepSeek, sin cambiar lo que ve el cliente.

## Decisión

**El negocio elige un nivel de IA por agente (`estandar` o `avanzado`). El core lo traduce a un modelo al publicar la versión, y el modelo nunca se muestra al negocio.**

1. **El nivel es dato del agente.**
   - **Dónde se guarda:** en una columna `nivel_ia` de `agents`, con los valores `estandar` | `avanzado` y `estandar` por defecto (migración aditiva).
   - **El contrato no cambia:** `AgentSpecV3` sigue igual (invariante 4 de `CLAUDE.md`). El nivel no entra en el contrato: lo que entra es el modelo que sale de él.
   - **Cuándo aplica un cambio:** cambiar el nivel no hace nada hasta publicar, y publicar pasa por el gate pass^k y el canary (ADR-0117). El panel avisa «se aplica al publicar».
2. **Mapa de niveles en el core.**
   - **Dónde vive:** en `core/llm/niveles.py`, como un dict versionado en código. Fija `primary` y `router` de cada nivel:
     - `estandar` → `deepseek-flash` / `deepseek-flash`;
     - `avanzado` → `deepseek-v4-pro` / `deepseek-v4-pro`.
   - **Por qué también el `router`:** `route_model` cambia de modelo, no solo de modo. Toma `spec.model.router or LLM_MODEL_REASONING` (`core/orchestrator/loop.py:550`, `core/llm/difficulty_router.py:74`), y `LLM_MODEL_REASONING` es `deepseek-flash` (`core/config.py:143`). Sin fijar el router, un turno difícil en Avanzado bajaría a flash.
   - **El `fallback` no va en el mapa:** sigue siendo el de hoy (`LLM_MODEL_FALLBACK`).
   - **Sumar un proveedor** (ya existe `core/llm/anthropic_provider.py`) es cambiar el mapa y publicar versiones nuevas, sin que el negocio vea nada.
3. **Cuándo se traduce.**
   - **Dentro de `publicar`:** el nivel se resuelve en `publicar` (`core/agents/versiones.py:501`, donde hoy se aplica `modelo`, línea 531), leyendo `agents.nivel_ia`. Así sirve para los siete llamadores (`core/agents/editor.py:819`, `core/agents/base.py:163` y `:228`, `core/presets/agentes.py:467` y `:606`, `referencias_retiradas.py:510` y `:519`). Si se resolviera en cada llamador, un preset o una republicación automática devolvería un agente Avanzado a flash sin avisar.
   - **Prioridad:** con la llave de la plataforma, el nivel manda sobre el `modelo` de reemplazo que pasa el editor cuando el modelo fue retirado.
   - **Con llave propia (BYOK, `_usa_llave_propia`):** `publicar` no aplica el nivel y conserva el modelo del negocio (`modelo_del_negocio`, `core/agents/editor.py:511-517` y `:780-782`), igual que el turno en el punto 7. Si no, un negocio con proveedor propio publicaría `deepseek-flash` y su llamada fallaría siempre.
   - **Versión congelada:** una versión publicada guarda su modelo. El hash de la versión incluye `model.primary` (`versiones.py:535`), así que cambiar de nivel da una versión nueva, y volver al nivel anterior reusa la que ya existía (ADR-0117). Cambiar el mapa no toca las versiones vivas: se aplica en la próxima publicación.
4. **Gate de plan.**
   - **Quién puede elegir Avanzado:** solo los planes con `features.nivel_ia = "avanzado"`, que hoy son Premium y Empresarial. Desde otro plan responde 409 `plan_nivel_ia_not_included`.
   - **Al bajar de plan:** el turno usa los modelos de Estándar mientras el plan no incluya Avanzado, aunque la versión diga otra cosa.
     - El override va en `_resolved_model` (`loop.py:1806`), con este orden: llave propia (BYOK) > nivel efectivo según el plan > `spec.model.primary` > entorno.
     - Recorta también `spec.model.router` (`loop.py:550`), `spec.model.fallback` (`loop.py:673`, `:1862`) y `highRiskStage.model` (`core/orchestrator/politica_modelo.py:53`), que no pueden subir a un modelo de un nivel que el plan no incluye.
     - No toca el spec compilado (`_compilado_del_turno`, `loop.py:199`) ni el hash. La caché por versión (HU-AOS-35) queda intacta.
     - No hace falta republicar, y al volver a subir de plan se restaura.
   - **`highRiskStage.model`:** se mantiene para el master, dentro del techo del nivel efectivo.
5. **El negocio no ve el modelo.**
   - **Qué ve el gestor:** la API y el panel solo devuelven `nivel_ia` y «≈ N créditos por conversación» del tarifario vigente.
   - **Lo que hoy expone el modelo y se oculta al gestor:** las notas con el nombre del modelo (`core/agents/editor.py:782`, `:830-832`), `byok_model` (`core/api/routers/agent_editor.py:183`, `:489`) y `core/api/routers/settings.py:415`. `model.primary`, `fallback` y `router` quedan solo para el master (`settings.py`, `cerebro`), como ya pasa con `cost_usd` sin el permiso `costs` (`core/api/routers/agent_data.py`).
   - **Si el master fija `model.primary` a mano:** el nivel manda al publicar (punto 3). El master que quiera otro modelo lo agrega al mapa.
6. **Relación con el router de dificultad.** `difficulty_router.py` sigue decidiendo si el turno necesita razonamiento, pero con el `router` del nivel. El nivel elige los modelos y el router, cuándo usar el de razonamiento.
7. **Cobro.**
   - **Con la llave de la plataforma:** se cobra el costo real con el tarifario vigente (ADR-0208 §11–14), sin fee propio del nivel. Son unos 37,4 créditos por conversación en Avanzado contra unos 9,9 en Estándar.
   - **Con llave propia (BYOK, `_usa_llave_propia`, `loop.py:1832`):** el nivel no decide nada. Manda el modelo de la llave del negocio y solo se cobra el fee.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| El negocio elige el modelo por nombre | D-79 lo descarta: el cliente no debe saber qué corre por debajo, y cambiar de proveedor sería visible |
| Campo `nivel` en `AgentModelV3` | Cambia un contrato congelado (invariante 4) solo para guardar una preferencia que el core puede resolver antes |
| Un fee extra por usar Avanzado | Cobrar el costo real ya diferencia el nivel con el mismo margen. Un fee arbitrario rompe la regla «créditos = costo × 3» |
| Subir de nivel según la dificultad del turno | El router ya elige el modelo de razonamiento dentro del nivel. Mezclarlo con el nivel haría impredecible el consumo del negocio |

## Consecuencias

**Positivas:**
- Premium ofrece una mejora visible («más inteligente») con margen garantizado.
- Cambiar o sumar un proveedor queda fuera de la vista del cliente.
- El contrato no cambia.

**Negativas / lo que aceptamos pagar:**
- Un agente en Avanzado gasta unas 4 veces más créditos por conversación. El panel tiene que avisarlo claro antes de que el dueño lo active.
- El modelo se decide en dos momentos: al publicar (el nivel) y en el turno (el techo del plan). La regla del punto 4 da prioridad al plan.
- Los evals del golden set corren en Estándar. Avanzado necesita su propio replay grabado antes de ofrecerlo, y grabarlo usa el LLM real, así que requiere OK del dueño.

**Qué haría que revisáramos esto:**
- Que Avanzado no mejore el gate pass^k del golden set frente a Estándar, medido con replay.
- Que un proveedor nuevo exija parámetros que el mapa no puede expresar.
- Que más del 50 % de los agentes de Premium use Avanzado y agote el saldo antes de la mitad del ciclo.

## Impacto en el código

- **Migración aditiva:** `agents.nivel_ia`, la tercera de SPEC-51 (orden en ADR-0209 §6).
- **`core/llm/niveles.py`** (nuevo).
- **`core/agents/versiones.py`:** `publicar` resuelve `agents.nivel_ia` antes del hash (punto 3).
- **El turno:** `core/orchestrator/loop.py` (`_resolved_model` y los puntos de `router`, `fallback`) y `core/orchestrator/politica_modelo.py` aplican el techo del plan (punto 4).
- **Exposición del modelo:** `core/agents/editor.py`, `core/api/routers/agent_editor.py` y `core/api/routers/settings.py` (punto 5).
- **`core/billing/plans.py`:** `features.nivel_ia`.
- **API del gestor:** el editor del agente, con `nivel_ia`, el 409 nuevo y el modelo oculto.
- **`docs/openapi.json`.**
- **Panel y APK:** selector de nivel (HU-BILL-27).
