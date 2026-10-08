# Journeys

Conversaciones completas contra el **loop real** (`process_conversation`), con herramientas,
pagos simulados, eventos y reloj. Complementan el golden set (`evals/runner.py`), que solo
mira la primera respuesta del modelo (HU-SUB-12).

```bash
python evals/journeys.py                      # set suscripciones completo
python evals/journeys.py 03,05 --detalle      # algunos, con la conversación y las tools
python evals/journeys.py --set otro --umbral 0.95 --reporte evals/reportes/journeys.json
```

**Solo en bases de prueba** (CI, nightly o un stack efímero local): cada corrida deja un negocio
`journeys-*` y reemplaza la pasarela dentro del proceso. Con `APP_ENV=production` no arranca. Los
jobs corren **solo sobre el negocio del journey**, nunca sobre otros negocios de la base.

Cada corrida crea **un negocio de prueba** (`negocio.json` del set) y **un cliente nuevo por
journey**. Los turnos usan el canal `test`: nada sale por WhatsApp. La corrida se guarda en
`eval_runs` del negocio (`journeys:<set>@<versión del spec>`) y, con `--reporte`, en un JSON.

## Un set

`evals/journeys/<set>/`:

- `negocio.json`:
  - `preset` y `plan` del negocio (o `packs` sueltos);
  - `catalogo` de commerce, que reemplaza los ejemplos del preset;
  - `config` por pack (sin secretos reales);
  - `pagos_simulados.monto_minimo`, el mínimo de la pasarela real.
- `NN-nombre.json`: un journey con `nombre`, `descripcion`, `cliente` (opcional) y `pasos`.

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
| `{"accion": "activar_suscripcion", "datos": {"plan": {...}}}` | Activa una suscripción del cliente con el proveedor del negocio (contrato del core), sin conversación. |

`espera` de un turno:
- `herramientas`: todas, en cualquier orden;
- `alguna_herramienta` y `sin_herramientas`;
- `texto_contiene`, `texto_contiene_alguno` y `texto_no_contiene`;
- `max_caracteres` y `escala`.

Las herramientas salen de `audit_log`. Sus resultados quedan como `{herramientas.<tool>.<campo>}`.

Variables en strings: `{cliente.id}`, `{negocio.id}`, `{ahora}`, `{pagos.ultimo.reference}`,
`{suscripcion.subscription_id}` y las filas guardadas.

## Umbral

El nightly corre el set completo con `--umbral 0.95` y sube el reporte. El gate de CI corre un
subconjunto estable. Un journey que falla imprime la conversación, las herramientas y sus
resultados.
