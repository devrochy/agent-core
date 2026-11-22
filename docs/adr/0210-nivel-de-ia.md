# ADR-0210 — Nivel de IA: el negocio elige un nivel y el core lo traduce al modelo

- **Estado:** propuesto (HU-BILL-17, 2026-11-22).
- **Fecha:** 2026-11-22
- **Contexto:** D-79 y SPEC-51 (`solvershub-docs`, #291), HU-BILL-24.
  **Usa:** ADR-0011 (contratos congelados), ADR-0050 (`model.primary`/`fallback`/`router`), HU-AOS-79
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
   - **Dónde se guarda:** una columna `nivel_ia` en `agents`, con valores `estandar` | `avanzado`, por defecto `estandar`, en una migración aditiva.
   - **El contrato no cambia:** `AgentSpecV3` sigue sin cambios (invariante 4). El nivel no entra en el contrato; entra el modelo que resulta de él.
2. **Mapa de niveles en el core.**
   - **Dónde vive:** en `core/llm/` (por ejemplo `core/llm/niveles.py`), como un dict versionado en código:
     - `estandar → deepseek-flash`;
     - `avanzado → deepseek-v4-pro`.
   - **Qué define:** el `primary` y el `fallback` de cada nivel.
   - **Sumar un proveedor** (ya existe `core/llm/anthropic_provider.py`) es cambiar el mapa y publicar versiones nuevas. El negocio no ve ningún cambio.
3. **Cuándo se traduce.**
   - Al publicar una versión, `publicar` (`core/agents/versiones.py:501`) recibe el modelo del nivel por el mismo parámetro `modelo` que ya reemplaza `model.primary` solo en la versión (HU-AOS-79).
   - Una versión publicada queda con su modelo congelado. Cambiar el mapa no altera las versiones vivas: se aplica cuando se publique otra.
4. **Gate de plan.**
   - **Quién puede elegir Avanzado:** solo los planes con `features.nivel_ia = "avanzado"`, que hoy son Premium y Empresarial. Pedirlo desde otro plan responde 409 `plan_nivel_ia_not_included`.
   - **Al bajar de plan:** el turno usa el modelo de Estándar mientras el plan no incluya Avanzado, aunque la versión diga otro. No hace falta republicar, y volver a subir de plan lo restaura.
5. **El negocio no ve el modelo.**
   - **Qué ve el gestor:** la API del gestor y el panel solo devuelven `nivel_ia`, más «≈ N créditos por conversación» del tarifario vigente.
   - **Qué queda restringido:** `model.primary`, `fallback` y `router` quedan solo para el master (`core/api/routers/settings.py`, `cerebro`). Las respuestas de uso ya ocultan `cost_usd` sin el permiso `costs` (`core/api/routers/agent_data.py`), y el modelo se oculta igual.
6. **El router sigue igual.** `difficulty_router.py` sigue eligiendo el modo (con o sin razonamiento) **dentro** del modelo del nivel. El nivel elige el modelo y el router, cómo usarlo.
7. **Cobro.** Se cobra el costo real con el tarifario vigente (ADR-0208 §11–14), sin fee propio del nivel: unos 37,4 créditos por conversación en Avanzado contra unos 9,9 en Estándar. Con BYOK (Empresarial) solo se cobra el fee.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| El negocio elige el modelo por nombre | D-79 lo descarta: el cliente no debe saber qué corre por debajo, y cambiar de proveedor sería visible |
| Campo `nivel` en `AgentModelV3` | Cambia un contrato congelado (invariante 4) solo para guardar una preferencia que el core puede resolver antes |
| Un fee extra por usar Avanzado | Cobrar el costo real ya diferencia el nivel con el mismo margen. Un fee arbitrario rompe la regla «créditos = costo × 3» |
| Subir de nivel según la dificultad del turno | Ya lo hace el router dentro del modelo. Mezclarlo con el nivel haría impredecible el consumo del negocio |

## Consecuencias

**Positivas:**
- Premium ofrece una mejora visible («más inteligente») con margen garantizado.
- Cambiar o sumar un proveedor queda fuera de la vista del cliente.
- El contrato no cambia.

**Negativas / lo que aceptamos pagar:**
- Un agente en Avanzado gasta unas 4 veces más créditos por conversación. El panel tiene que avisarlo claro antes de que el dueño lo active.
- Hay dos lugares que deciden el modelo, el nivel y la versión. La regla del punto 4 da prioridad al plan.
- Los evals del golden set corren en Estándar. Avanzado necesita su propio replay grabado antes de ofrecerlo, y grabarlo usa el LLM real, así que requiere OK del dueño.

**Qué haría que revisáramos esto:**
- Que Avanzado no mejore el gate pass^k del golden set frente a Estándar, medido con replay.
- Que un proveedor nuevo exija parámetros que el mapa no puede expresar.
- Que más del 50 % de los agentes de Premium use Avanzado y agote el saldo antes de la mitad del ciclo.

## Impacto en el código

- **Migración aditiva:** `agents.nivel_ia`.
- **`core/llm/niveles.py`** (nuevo).
- **`core/agents/versiones.py`:** recibe el modelo del nivel al publicar.
- **El turno:** el loop o el ingreso resuelve el nivel con el plan actual (punto 4).
- **`core/billing/plans.py`:** `features.nivel_ia`.
- **API del gestor:** el editor del agente, con `nivel_ia`, el 409 nuevo y el modelo oculto.
- **`docs/openapi.json`.**
- **Panel y APK:** selector de nivel (HU-BILL-27).
