# Evals

| Pieza | Qué mira | Cómo se corre |
|---|---|---|
| Golden set (`evals/runner.py`) | La primera respuesta del modelo a una conversación armada | `python evals/runner.py [patrón] [--k N] [--tenant <slug>]` |
| Journeys (`evals/journeys.py`) | Conversaciones completas contra el loop real, con tools, pagos simulados y reloj | `python evals/journeys.py [patrón] [--k N] [--set X \| --tenant <slug>]`, ver [`journeys/README.md`](journeys/README.md) |
| Cliente simulado (`evals/simulador.py`) | Un LLM hace de cliente en los pasos `simulado` de un journey (HU-ARCH-28) | Lo usa `journeys.py` |
| Verificador (`evals/verificador.py`) | Falsos positivos del verificador de afirmaciones (sin LLM) | `python evals/verificador.py` |

Con k > 1 se reportan pass@1, pass^k y los escenarios inestables (ADR-0119).

## Grabar y reproducir el LLM

Las evals llaman al LLM real y gastan saldo. Con el modo de grabación, una corrida queda
**grabada** (request → respuesta) y después se puede **reproducir** sin red y sin `LLM_API_KEY`.
Está fijado en la adenda del 2026-10-10 de ADR-0119. El código está en `core/llm/cassette.py` y
el cableado en `evals/grabaciones.py`.

| Modo | Qué hace | Necesita |
|---|---|---|
| `off` (por defecto) | LLM real, como siempre | `LLM_API_KEY` |
| `record` | LLM real y graba cada llamada. **Gasta saldo: solo con OK del dueño** | `LLM_API_KEY` |
| `replay` | Responde desde la grabación. Si falta la grabación o el request cambió, **falla cerrado**: nunca cae al LLM real | Nada (ni red ni clave) |

Se elige con `--grabacion off|record|replay` o con la variable `LLM_CASSETTE`. La raíz de las
grabaciones se puede cambiar con `--grabaciones DIR` o `LLM_CASSETTE_DIR`.

```bash
# Reproducir el gate del CI, sin clave
python evals/runner.py "06,07,08,21,22,23,24,25,26" --grabacion replay
python evals/journeys.py "01,05" --grabacion replay

# Regrabar UN escenario o journey (LLM real: pedir OK al dueño antes)
python evals/runner.py 06 --grabacion record
python evals/journeys.py 10 --k 4 --grabacion record
```

Código de salida: `0` si pasa el umbral, `1` si no lo pasa y `3` si la corrida se cortó por la
grabación (no existe, está desactualizada o el modo no está permitido).

### Dónde viven

- **Framework:** `evals/cassettes/golden/<escenario>.json` y
  `evals/cassettes/journeys/<set>/<journey>.json`, en este repo.
- **Un negocio:** `TENANTS_ROOT/<slug>/evals/cassettes/{golden,journeys}/`, junto a su golden, en
  el repo del negocio (`solvershub-comercial/tenants/<slug>/evals/`). Nunca en el framework: con
  `--tenant` el runner rechaza rutas del repo del framework (salvo `tenants/`, gitignored), y una
  raíz explícita (`--grabaciones` o `LLM_CASSETTE_DIR`) se separa por negocio: `<raíz>/<slug>/`.

Un archivo por escenario o journey, con todos sus intentos. Es JSON ordenado y con sangría para
que los diffs se puedan revisar. Contiene:

- `llamadas`: el request normalizado (modelo, parámetros, mensajes) y sus respuestas, en orden;
- `bloques` y `herramientas`: los bloques del prompt y los esquemas de tools, sin repetir.

No guarda latencias ni fechas de grabación. Antes de escribir rechaza la grabación (sobre el texto
crudo y sobre el JSON final) si aparece:

- la API key usada (la del `.env` o la BYOK del panel);
- cualquier valor de la configuración cuyo nombre sugiere un secreto (`*KEY*`, `*SECRET*`,
  `*TOKEN*`, `*PASSWORD*`, `*DSN*`, o la contraseña de una URL);
- algo con forma de credencial (`sk-…`, tokens de Meta `EAA…`, llaves de Wompi, JWT, `Bearer …`,
  `?access_token=`, URLs con usuario y clave).

Los secretos **por negocio** (llaves de Wompi o tokens de Meta guardados cifrados en la config de
sus packs) no se comparan por valor: solo los frena esa revisión de formas, si una tool llegara a
devolverlos al LLM. Los journeys usan configuración de prueba sin secretos reales. Las grabaciones son **datos de prueba**: nunca se
cargan como prompt ni como configuración (invariante 3). Solo pueden traer datos de clientes de
prueba, como los de los escenarios.

### Clave y qué se normaliza

La clave es un sha256 del request normalizado. Incluye:

- `proveedor`, `modelo`, `thinking`, `max_tokens` y `temperature`;
- el prompt del sistema y las tools (nombre, descripción y parámetros);
- los mensajes (rol, texto, tool_calls con argumentos, tool_call_id);
- la **semilla del intento** en los journeys (la del cliente simulado).

Lo que cambia entre corridas se normaliza:

| Qué | Cómo queda |
|---|---|
| Fecha y hora del bloque dinámico (ADR-0091, `## Fecha y hora actual` y los próximos días) | `<fecha_hora>` |
| UUIDs (negocio, cliente, conversación, pedidos; también dentro de URLs o correos) | `<uuid:1>`, `<uuid:2>`… |
| Hex de 32 caracteres (`uuid4().hex`) | `<hex:n>` |
| Tokens de 16 hex con alguna letra (p. ej. el token de términos del alta) | `<hex16:n>` |
| Slugs de la corrida `journeys-xxxxxxxx` | `<slug:n>` |
| Timestamps ISO 8601 y fechas `AAAA-MM-DD` | `<ts:n>`, `<fecha:n>` |
| Ids de tool_calls | `<call:n>` |

Los marcadores se numeran por orden de aparición, así que el mismo valor siempre lleva el mismo
marcador. Al grabar, si la respuesta repite un valor del request (por ejemplo el `order_id` que
devolvió una tool), se guarda el marcador. Al reproducir, el marcador se cambia por el valor de
la corrida actual, y así la tool siguiente recibe el pedido que existe en esta base.

**Intentos (k):** cada intento de un journey tiene su semilla, que entra en la clave. En el golden
set, que no tiene semilla, los requests idénticos se guardan como una **secuencia** y se
reproducen en el mismo orden. Si se pide un intento más de los grabados, falla: graba con el
mismo k y los mismos escenarios que vas a reproducir.

### Cuando falla el replay

El error dice qué cambió frente a la llamada grabada más parecida. Puede ser el modelo o un
parámetro, el bloque N del prompt (con las líneas del diff), tools agregadas, quitadas o
cambiadas, o el primer mensaje distinto. También trae el comando para regrabar. Ejemplo:

```text
Grabación desactualizada (evals/cassettes/golden/06-camino-feliz-saludo.json): …
  - prompt del sistema, bloque 3 (## Reglas de operación):
      +- Regla nueva.
Para regrabar (llama al LLM real y gasta saldo: requiere OK del dueño): `python evals/runner.py 06-camino-feliz-saludo --k 1 --grabacion record`
```

Si el cambio es intencional (un playbook, una tool o el flujo), hay que regrabar. Si no lo es,
encontraste una regresión del prompt.

### Lo que el replay NO hace

- **No mide la calidad ni la consistencia del modelo.** Repite lo grabado: valida el código
  determinista alrededor del LLM (tools, verificador, aserciones, estados). Por eso una corrida
  en replay **no se guarda en `eval_runs`**. El umbral de ADR-0119 (≥ 95 % tres noches) y pass^k
  se miden siempre en vivo.
- **Fechas escritas en palabras** ("el próximo miércoles 14 de octubre") no se rehidratan: un
  escenario de calendario grabado un día puede no coincidir otro día. Esos escenarios se corren en
  vivo o llevan `now` fijo (el golden lo soporta).
- **Mismo orden y mismas condiciones:** los journeys comparten un negocio por corrida, así que los
  consecutivos (pedidos, referencias) dependen de qué journeys corrieron antes. Reproduce el mismo
  subconjunto que grabaste. Graba en una base **recién migrada y sembrada**, como la del CI: en la
  primera corrida de journeys sobre una base nueva, el flujo no ofrece las tools `sub_*` (se ve en
  el diff si grabas en una base ya usada).
- En `record` y `replay` la memoria de largo plazo corre **sin embeddings**
  (`EMBEDDING_PROVIDER=none`). Con `openai` saldría a la red, con `local` podría bajar un modelo,
  y el recuerdo cambia el prompt.
- No hay streaming: las evals usan `generate`.
- Cambiar un playbook del core invalida las grabaciones de todos los negocios. Por eso, en esta
  versión, el replay apunta a los sets del framework. Las grabaciones de un negocio funcionan, pero
  quedan fuera de cualquier gate.

### Seguridad

- El modo solo lo encienden los runners de evals (`core.llm.cassette.activar`). El servidor no lee
  `LLM_CASSETTE`. `activar` falla con `APP_ENV` distinto de development o test, y el runner sale
  con código 3.
- En `replay` el proveedor real ni siquiera se construye. Una llamada sin grabación, o fuera de un
  escenario o journey, es un error y no una llamada a la red.
- `record` sin `LLM_API_KEY` no graba nada (el escenario se salta o el proveedor falla al
  resolverse).
- La grabación es un dato no confiable (invariante 7). Se valida al cargarla: si está ilegible o
  pesa más de 20 MB, da exit 3. Una respuesta grabada no puede llamar una tool que no se ofreció en
  ese request. El diff no imprime caracteres de control.
- Las tools **declarativas** de un negocio (HTTP con `ScopedHttp`) se ejecutan de verdad también en
  replay. Solo el LLM sale de la grabación. Los pagos y los correos ya son simulados en los journeys.
- Las grabaciones de un negocio no se aceptan dentro del repo del framework (salvo `tenants/`, que
  está en `.gitignore`).

### En el CI y flujo para regrabar

`scripts/gate_evals.sh off|record|replay` tiene los argumentos del gate en un solo lugar: golden
`06,07,08,21,22,23,24,25,26` y journeys `01,05` con umbral 1.0. Es el mismo gate en vivo, grabado
y reproducido. ADR-0120 (adenda del 2026-10-10) lo fija así:

1. **PR a `develop`:** el job `evals-replay` levanta una base nueva, la migra y la siembra, y corre
   `bash scripts/gate_evals.sh replay` **sin** `LLM_API_KEY`. No gasta saldo.
2. **Push a `develop` y nightly:** con el LLM real, como antes. Es la medición de ADR-0119.

**Si cambias un prompt** (un playbook, la descripción de una tool, el flujo o la persona) y el job
`evals-replay` falla con «Grabación desactualizada»:

1. Revisa el diff. Si el cambio no era intencional, es una regresión: arréglala.
2. Si era intencional, pide **OK al dueño** para regrabar. Son unas 9 llamadas, ~0,01 USD.
3. Regraba en una **base recién migrada y sembrada**, igual que el CI:

   ```bash
   # stack efímero propio con las variables de ci.yml, base vacía
   agent-core db migrate && ADMIN_PASSWORD=admin123 python scripts/init_db.py
   LLM_API_KEY=… bash scripts/gate_evals.sh record
   ```

   En `record`, un escenario que falla por la respuesta del modelo igual queda grabado y la
   corrida sigue. Un error de grabación (exit 3) la corta sin más llamadas.
4. Verifica sin red en otra base nueva: `bash scripts/gate_evals.sh replay` con `LLM_API_KEY=`
   vacía. Revisa que los JSON no tengan secretos ni datos reales y commitéalos en la misma PR.

Un replay sobre una base **ya usada** no coincide: en la segunda corrida el flujo ofrece las tools
`sub_*`, que en la primera no estaban. Por eso el CI y la grabación parten siempre de una base
nueva.
