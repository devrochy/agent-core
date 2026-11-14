# ADR-0194 — Importación con IA en producción y cobro de acciones de IA cotizadas

- **Estado:** propuesto (SPEC-45, D-71). El dueño decidió el alcance el 2026-11-14: importar va a
  producción, generar datos de prueba sigue solo en test, el CSV con IA cubre solo productos, una foto es
  un producto y el bloqueo por saldo se activa ya.
- **Fecha:** 2026-11-14
- **Contexto:** SPEC-45 «Carga de datos al crear un agente» en `solvershub-docs` (HU-BILL-16, HU-QA-16,
  HU-COM de CSV y de fotos, HU-AOS-72/73).
- **Enmienda:**
  - ADR-0180 §8 (importar `productos` también en producción) y precisa §3;
  - ADR-0144 §11 (bloqueo por crédito solo para acciones de IA cotizadas);
  - ADR-0171 §2 (el asistente gana los pasos Datos, Lo cargado y Probar).
- **Usa:**
  - ADR-0008 (ledger solo de inserción);
  - ADR-0124 (hooks opcionales de pack con contrato en el core);
  - ADR-0142 §6 (contrato del 409 `plan_limit_<métrica>`);
  - ADR-0146 (datos de prueba por agente, `test_data_records`);
  - ADR-0151 (dispatcher de visión);
  - ADR-0166 (el servidor sirve lo que el cliente pinta);
  - ADR-0184 (registro de retiros);
  - invariantes 3, 5 y 7.

## Contexto

El asistente para crear agentes (ADR-0171) no carga datos: termina en Publicar sin dejar probar. Las piezas
existen sueltas: generar datos de prueba (ADR-0146, solo test, devuelve conteos), importar CSV (ADR-0180,
plantilla fija, solo test), visión (`core/media/vision.py`, solo la usa la ingesta de WhatsApp en sombra) y
créditos que se miden pero nunca se verifican antes de gastar (ADR-0144 §11). Un negocio real necesita cargar
su catálogo desde una hoja con columnas propias o desde fotos, y cada foto cuesta una llamada de visión.

## Decisión

**El core hace el archivo o la imagen, la llamada a la IA, el ledger y la cotización; el pack pone el esquema y
escribe con `importar_filas`.** Un solo lector, un solo paso de mapeo y una sola escritura. Sin tablas nuevas
ni migración.

### 1. Helper de IA estructurada en el core

`core/ia/estructurado.py` es el único camino del core para pedirle a la IA un objeto con forma conocida fuera
del turno:
- `generar_estructurado` (texto) y `reconocer_imagen` (imagen, sobre `describir_imagen`; `vision.py` no gana
  dominio) reciben un **playbook del kernel**, un esquema pydantic, la entrada como datos, el `kind` del ledger
  y el `result` (con `proposito`) que pone quien llama.
- Resuelven proveedor y `key_mode`: texto con la llave propia del negocio si la configuró (misma condición
  que el turno) o la de plataforma, o con un proveedor inyectado; imagen con `resolve_vision_config`.
- Toman el primer objeto JSON y lo validan. La salida es dato no confiable (invariante 7).
- Registran el consumo en `usage_ledger` con `creditos_por_evento` (concepto `llm_turn` o `vision`): se mide
  aunque la salida no valide, porque el proveedor ya cobró. Medir nunca rompe la llamada.
- Errores con motivo estable: `ErrorProveedor` (sin respuesta, sin fila) y `ErrorFormato` (con fila). Quien
  llama decide el respaldo.

Los playbooks del kernel nuevos (`mapear_columnas`, `reconocer_producto`) no nombran comercio (invariante 3):
el dominio llega desde la `PlantillaDeImportacion` del pack (sinónimos de cabecera, esquema de la foto).
`core/qa/datos_generados.py` pasa a usar el helper sin cambiar comportamiento.

### 2. Importar en producción (enmienda ADR-0180 §8; precisa §3)

- **§8:** la plantilla `productos` se puede importar también en producción. Las otras tres (`tipos_de_cita`,
  `clientes_y_prestamos`, `planes`) siguen solo en test. Abrirla es **dato de la plantilla**
  (`PlantillaDeImportacion`), no una condición del cliente: el panel pinta lo que el servidor declara en
  `plantillas_de_importacion` (ADR-0166), nunca «si es Ventas». Generar datos de prueba sigue solo en test
  (`QA_TESTERS_ENABLED`, ADR-0146).
- **§3 se mantiene: sin estado en el servidor.** La precarga vive en el cliente:
  - CSV: el servidor devuelve el mapeo propuesto (heurística con `normalizar_cabecera` y los sinónimos del
    pack; una sola llamada a la IA para las columnas que no reconoce, con las cabeceras y unas 5 filas de
    muestra) y las filas normalizadas. El cliente las edita y las reenvía en JSON; confirmar vuelve a validar
    contra el esquema del pack. La IA solo puede proponer columnas de la plantilla o «ignorar». Ledger:
    `kind='generacion'`, `result.proposito='mapeo_catalogo'`.
  - Fotos: **una foto por petición** (sube la imagen con el perfil `producto`, la pasa por visión y devuelve
    la fila propuesta con su `image_key`). Sin Celery ni polling. Ledger: `kind='vision'`,
    `result.proposito='foto_catalogo'`, `result.image_key`.

### 3. Cobro de acciones de IA cotizadas (enmienda ADR-0144 §11)

ADR-0144 §11 («sin bloqueo») sigue rigiendo para todo lo demás. Se exceptúan las **acciones de IA cotizadas**:
una acción que el dueño pide a mano, cuyo costo se muestra antes y que se puede no hacer. Hoy: la foto de
catálogo.
- **Cotizar** (`cotizar(concepto, unidades, key_mode)`): con llave propia, solo el fee (1,0 crédito por foto);
  con la de plataforma, costo de referencia más margen y fee del tarifario vigente.
- **Verificar** (`alcanza(tenant, creditos)`): bajo `pg_advisory_xact_lock` de la clave `billing:creditos` y
  el negocio. Si no alcanza, 409 `plan_limit_creditos` con `details = {metric, used, limit}`, el contrato de
  ADR-0142 §6 construido por `core/api/plan_limits.py`.
- **Registrar** el consumo real con el helper del §1.
- **Carrera aceptada.** El lock serializa la verificación, no la llamada: dos peticiones concurrentes pueden
  pasar ambas y exceder el saldo en, como máximo, una foto. No hay reserva: el ledger es solo de inserción
  (ADR-0008) y una reserva exigiría filas que luego se anulan.
- **Idempotencia.** Antes de cobrar se busca en el ledger una fila del negocio con el mismo
  `result->>'image_key'`: un reintento de la misma foto no se cobra dos veces.
- Interruptor de configuración, **encendido** por decisión del dueño. No se crea una métrica `fotos_catalogo`:
  el crédito ya limita al plan Free.
- `medir_creditos` desglosa por `kind`, para que la visión no aparezca como «IA» en `/admin/plan`.
- La ingesta de imágenes y audios de WhatsApp **sigue en sombra** (`credits=0`, P-06 abierta).

### 4. El asistente carga datos (enmienda ADR-0171 §2)

Tras «revisar y crear», el asistente suma tres pasos: **Datos** (generar en test, importar CSV, importar
fotos u omitir), **Lo cargado** (la lista de lo creado, con frases sugeridas armadas con esos nombres) y
**Probar** (playground fijado al borrador en modo simulado; depende de HU-ARCH-14, mientras tanto el botón
lleva al «Probar» de la ficha tras publicar). El tope de 10 pasos de §2 se mantiene: el cupo de preguntas por
plantilla baja a unas 3. El servidor sigue siendo la fuente de verdad de lo que se ofrece.

### 5. Hook opcional `describir_registros` en `BaseModule`

`describir_registros(tenant_id, refs) -> list[...]` traduce los `record_ref` de `test_data_records` a filas
legibles (nombre, precio, cuándo) para el paso Lo cargado. Opcional, con contrato en el core como los demás
(ADR-0124): lo implementan commerce, calendar y cobranza; subscriptions no, porque no siembra datos propios.
Ruta: `GET /admin/agents/{id}/datos-de-prueba/registros`, solo en test.

### 6. Retiro futuro de `POST /pack/commerce/products/import`

El importador de productos del panel de Comercio (`name`, `price`, omite en silencio) queda registrado para
retirarse, al estilo de ADR-0184, cuando `/comercio/productos` use la tabla de precarga nueva. Hasta entonces
convive; no gana funciones.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Guardar la precarga en el servidor (tabla o Redis) | Rompe ADR-0180 §3, exige limpieza y RLS nuevas; el cliente ya la tiene. |
| Procesar todas las fotos en una tarea de Celery con polling | Más piezas y un estado que limpiar; una foto por petición da progreso y corte por saldo naturales. |
| Reservar créditos antes de la llamada | El ledger es solo de inserción (ADR-0008); la carrera aceptada cuesta como mucho una foto. |
| Bloquear por crédito todo consumo (turnos incluidos) | Cortaría conversaciones de clientes en vivo; ADR-0144 §11 sigue para lo no cotizado. |
| Mapeo con IA dentro del pack | Duplicaría lector, llamada y ledger por pack; el pack solo aporta datos (sinónimos, esquema). |
| Métrica nueva `fotos_catalogo` | El crédito ya limita al plan Free; otra métrica es otro tope que explicar. |

## Consecuencias

**Positivas:** un negocio real carga su catálogo desde su hoja o sus fotos en el mismo asistente donde crea el
agente; toda llamada de IA fuera del turno queda medida por el mismo camino; el dueño ve el costo antes de
gastar y no puede pasarse del saldo más que una foto.

**Negativas / lo que aceptamos pagar:** una carrera de hasta una foto por encima del saldo; dos importadores
de productos conviviendo hasta el retiro del §6; el asistente con menos preguntas por plantilla; el bloqueo
por crédito ya no es uniforme (solo para lo cotizado).

**Qué haría que revisáramos esto:**
- que el ledger muestre negocios que pasan su saldo en más de una foto (la carrera no es la acotada);
- que un CSV o una tanda de fotos supere los topes de una petición (500 filas, una foto) en uso real y pida
  procesamiento asíncrono;
- que se cierre P-06 y la ingesta de WhatsApp tenga que cobrar: entonces el bloqueo deja de ser solo para lo
  cotizado.

## Impacto en el código

- `core/ia/estructurado.py` (nuevo) y `core/qa/datos_generados.py` (usa el helper).
- `core/playbooks/kernel/` (`mapear_columnas.md`, `reconocer_producto.md`).
- `core/billing/` (`cotizar`, `alcanza`, desglose por `kind` en `medir_creditos`) y `core/api/plan_limits.py`.
- `core/qa/importar_datos.py`, `core/qa/contrato_importacion.py` y `core/api/routers/datos_de_prueba.py`
  (mapear, confirmar con filas en JSON, foto, cotización, registros).
- `core/mcp/types.py` (`BaseModule.describir_registros`).
- `packages/pack-commerce` (sinónimos y esquema de la foto en `importacion.py`), `pack-calendar` y
  `pack-cobranza` (`describir_registros`).
- Panel y APK: pasos del asistente (fuera de este repo).
