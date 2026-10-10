# Journeys

Conversaciones completas contra el **loop real** (`process_conversation`), con herramientas,
pagos simulados, eventos y reloj. Complementan el golden set (`evals/runner.py`), que solo
mira la primera respuesta del modelo (HU-SUB-12).

```bash
python evals/journeys.py                      # set suscripciones completo
python evals/journeys.py 03,05 --detalle      # algunos, con la conversación y las tools
python evals/journeys.py --set otro --umbral 0.95 --reporte evals/reportes/journeys.json
python evals/journeys.py 10,11 --k 4           # cada journey 4 veces: pass@1, pass^4 e inestables
python evals/journeys.py --tenant solvershub    # set de un negocio contra una réplica de sus artefactos
```

**Solo en bases de prueba** (CI, nightly o un stack efímero local): cada corrida deja un negocio
`journeys-*` y reemplaza la pasarela dentro del proceso. Con `APP_ENV=production` no arranca. Los
jobs corren **solo sobre el negocio del journey**, nunca sobre otros negocios de la base.

Cada corrida crea **un negocio de prueba** (`negocio.json` del set) y **un cliente nuevo por
journey**. Los turnos usan el canal `test`: nada sale por WhatsApp. Como el consumidor, el runner
guarda cada mensaje del cliente en `messages` antes de correr el loop. La corrida se guarda en
`eval_runs` del negocio (`journeys:<set>@<versión del spec>`) y, con `--reporte`, en un JSON.

## Un set

`evals/journeys/<set>/`:

- `negocio.json`:
  - `preset` y `plan` del negocio (o `packs` sueltos);
  - `catalogo` de commerce, que reemplaza los ejemplos del preset;
  - `config` por pack (sin secretos reales);
  - `pagos_simulados.monto_minimo`, el mínimo de la pasarela real;
  - `documentos_legales`: versiones de prueba de `terminos`, `privacidad` y `dpa`
    (`[{"tipo", "version", "url"}]`), publicadas como desde Plataforma (`legal.publicar`). En un
    vendedor, con los tres el pedido de un plan pide aceptarlos antes de crearse (ADR-0125).
- `NN-nombre.json`: un journey con `nombre`, `descripcion`, `cliente` (opcional) y `pasos`.

## Set de un negocio (`--tenant`)

Los escenarios de un cliente viven con sus artefactos, nunca en el framework (regla de
separación): `TENANTS_ROOT/<slug>/evals/journeys/` (`negocio.json` y los journeys). Con
`--tenant <slug>` el runner arma una **réplica** del asistente (HU-SH-08):

- copia `TENANTS_ROOT/<slug>/` (sin `evals/`) a un `TENANTS_ROOT` temporal con el slug del
  negocio de la corrida y apunta el proceso ahí; el directorio real no se toca y la copia se
  borra al terminar;
- crea el negocio con el `preset` y el `plan` de `negocio.json`, **sin ejemplos**, aplica su
  `config` por pack y siembra catálogo, `<pack>.json` y voz de marca como
  `agent-core tenant seed --voz-de-marca` (`core.onboarding.sembrar_artefactos`);
- con `"vendedor": true`, el negocio de la corrida vende negocios **solo en este proceso** (la
  allowlist real `PROVISIONING_SELLER_TENANTS` no se lee): el alta automática corre de verdad.

Lo que el asistente necesita y solo vive en la base del entorno (configuración de packs) tiene que
estar en `negocio.json` **y** en el runbook del negocio; si no, la réplica no es el asistente real.

Los correos de sistema (invitación del alta, avisos) nunca salen: se guardan y el journey los lee
como `{correos.ultimo.para}`, `{correos.ultimo.asunto}`. Para que cada intento sea un cliente nuevo
(un Free por correo, cuentas globales), usa correos únicos: `ana+{cliente.id}@example.com`. Las
reglas del alta también cuentan por documento y teléfono (un Free por persona, ADR-0125): usa
`{cliente.documento}` y `{cliente.celular}`, únicos por intento.

## Pagos simulados

El runner reemplaza el **proveedor** de `pack_payments`, la pieza que habla con Wompi. Corre el
camino real del pack (capacidad, tool `generar_link_pago`, transacciones) sin red: los links son
`https://pagos.simulados/<referencia>` y respetan el mínimo configurado.

## Pasos

| Paso | Qué hace |
|---|---|
| `{"usuario": "texto", "espera": {...}}` | Un turno del cliente. Con `hasta_herramienta` (una o una lista), insiste con `insistir` hasta `max_turnos` mientras no se llame esa herramienta. |
| `{"evento": "payment.confirmed", "datos": {...}}` | Emite el evento en el bus, como la pasarela. |
| `{"reloj": "+30d"}` | Adelanta el reloj del journey (`d`, `h`, `m`). |
| `{"job": "pack.nombre"}` | Corre `<pack>.jobs._<nombre>(now)` con el reloj del journey. |
| `{"verifica": {"tabla": ..., "donde": {...}, "espera": {...}}}` | Consulta filas del negocio (RLS + `tenant_id`). `donde` filtra por igualdad (`campo->>clave` en JSONB); `espera` compara la primera fila (`$no_nulo`, `$en`, `$contiene`); `filas` exige cantidad; `guardar_como` deja la fila para `{nombre.campo}`. |
| `{"outbox": "despachar", "rondas": 5}` | Entrega los eventos pendientes del outbox **del negocio del journey** (RLS y `tenant_id`), como el despachador del worker, hasta `rondas` vueltas: un evento puede generar otro (`payment.confirmed` → activación → `subscription.activated` → alta). Falla si un evento quedó con error. |
| `{"accion": "activar_suscripcion", "datos": {"plan": {...}}}` | Activa una suscripción del cliente con el proveedor del negocio (contrato del core), sin conversación. |
| `{"simulado": {"objetivo": ..., "datos": {...}, "estilo": ..., "max_turnos": 8}, "espera_fin": [...]}` | Un cliente simulado (LLM, prompt en `evals/simulador/cliente.md`) conversa con el loop hasta cumplir su objetivo (`objetivo`), rendirse (`se_rindio`), pasar a un humano (`relevo`) o llegar al tope (`tope`). `espera_fin` lista los finales aceptados. La semilla (`semilla` del journey + número de intento) elige una variante de comportamiento. |

`espera` de un turno:
- `herramientas`: todas, en cualquier orden;
- `alguna_herramienta` y `sin_herramientas`;
- `texto_contiene`, `texto_contiene_alguno` y `texto_no_contiene`;
- `max_caracteres` y `escala`.

Las herramientas salen de `audit_log`. Sus resultados quedan como `{herramientas.<tool>.<campo>}`.

Variables en strings: `{cliente.id}`, `{cliente.documento}`, `{cliente.celular}` (10 dígitos,
derivados del id del cliente), `{negocio.id}`, `{ahora}`, `{pagos.ultimo.reference}`,
`{suscripcion.subscription_id}` y las filas guardadas.

## Consistencia (pass^k)

Con `--k N` cada journey corre N veces, con un cliente nuevo y otra semilla por intento
(HU-ARCH-28, ADR-0119). Por journey se reporta:

- **pass@1**: fracción de intentos que pasaron;
- **pass^k**: si pasaron los N;
- **inestable**: pasó en unos y falló en otros. El reporte dice qué cambió: herramientas que faltaron o
  aparecieron en los fallidos y el motivo más repetido.

Cada `verifica` deja sus aserciones con esperado y real en `eval_results.state_assertions`, y el
paso `simulado` deja su traza en `eval_results.simulator`. Un journey con cliente simulado se
valida por el **estado final** en la base, no por el texto exacto de las respuestas.

`evals/runner.py` (golden) también acepta `--k`, `--umbral` y `--reporte`.

## Umbral

El umbral se mide sobre **pass@1**: con k = 1 es la tasa de siempre. El nightly corre el set
completo con `--k 4 --umbral 0.95` y sube el reporte con pass^4. El gate de CI corre un
subconjunto estable. Un journey que falla imprime la conversación, las herramientas y sus
resultados.
