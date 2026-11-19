# ADR-0129 — Datos del alta declarados en el schema de `crear_pedido`

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Enmienda:** ADR-0121, alternativa "`business_name` y `vertical` en el schema de `crear_pedido`" (se
  adopta, sin enum), y ADR-0125 § 1 (el token `acepta_terminos` queda declarado en el schema).
- **Contexto:** HU-SH-03 y HU-SH-07. El playbook de ventas del tenant solvershub (comercial PR #14) ya
  pide estos campos.

## Contexto

`crear_pedido` recibe en `customer_data` tres campos que su schema no declara:

- `business_name` y `vertical`, que usa el alta automática (ADR-0121);
- `acepta_terminos`, el token que devuelve `terminos_pendientes` (ADR-0125).

Hoy el modelo los manda solo porque lo dicen el `hint` de la respuesta y el playbook de ventas. Un
modelo que se ciñe al schema los omite, y el pedido de un plan da vueltas en `faltan_datos_cliente` o
en `terminos_pendientes`.

ADR-0121 descartó declararlos por dos razones:

- el costo: unos 65 tokens por turno en la tool de todos los negocios;
- que el schema cambiaría con cada preset (un `enum` de verticales).

## Decisión

1. Los tres campos se declaran en `customer_data.properties` de `crear_pedido`:
   - opcionales, de tipo `string`;
   - con una descripción de una línea (la descripción del parámetro es el único texto para el modelo
     en el código);
   - **sin `enum`**: las opciones de `vertical` siguen llegando en la respuesta (`opciones_vertical` y
     `opciones`). Así el schema es el mismo para todos los negocios y no cambia con los presets.
2. La tool sigue siendo la fuente de verdad. Declarar un campo no lo vuelve válido:
   - `acepta_terminos` solo vale si coincide (`hmac.compare_digest`) con el token de la conversación y
     del juego de versiones vigente, y hay un mensaje posterior del cliente (ADR-0125);
   - `vertical` tiene que ser el id de un preset (`_vertical_invalido`);
   - `business_name` no puede llevar enlaces y se recorta a 120 caracteres;
   - qué campos son obligatorios lo sigue decidiendo `checkout_fields` del negocio.
3. El comportamiento (cuándo pedirlos y cómo explicar los términos) sigue en el playbook de ventas del
   tenant. El `hint` de `terminos_pendientes` no cambia.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejarlos fuera del schema (ADR-0121) | Depende de que el modelo siga el `hint` o el playbook; un modelo ceñido al schema no los manda |
| `enum` de verticales en el schema | Cambia con cada preset y crece en la tool de todos los negocios |
| Una tool aparte para los datos del alta | Una llamada más por venta y otro estado que coordinar con el pedido |
| Schema distinto según el negocio | La tool es la misma para todos (invariante 2); no hay schema por tenant |

## Consecuencias

**Positivas:**

- el pedido de un plan no depende de que el modelo lea el `hint` para saber dónde va cada dato;
- los clientes que validan los argumentos contra el schema no descartan estos campos.

**Negativas:**

- unos 80 tokens más por turno en la tool de todos los negocios que tienen `pack_commerce`, también en
  los que no venden planes. Se revisa si el costo de las tools pesa.

## Impacto en el código

- `packages/pack-commerce/pack_commerce/skills.py`: los tres campos en el schema de `crear_pedido`.
- `tests/test_provisioning_piezas.py`: el test que fijaba que no estaban en el schema ahora fija que están,
  opcionales y sin enum.
- `tests/integration/test_antiabuso_alta.py`: el token de términos de un vendedor no vale en otro
  (`aislamiento`).
