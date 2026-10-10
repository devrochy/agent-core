# ADR-0125 — Antiabuso y cumplimiento del alta automática

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Enmienda:** ADR-0121 § 3.5 (un Free por persona además de por cliente y correo), § 5 (aprobar un
  alta en revisión) y § 6; ADR-0123 § 10 (un requisito más para encender el alta en producción).
- **Contexto:** HU-SH-07 (SPEC-07, REQ-SH-06). Se apoya en ADR-0121 (alta automática), ADR-0123
  (cobro de la plataforma), el limitador compartido de `core/security/rate_limit.py` (PR #343) y
  ADR-0012 (contexto de tenant).

## Contexto

El alta automática (ADR-0121) crea un negocio por cada suscripción activada en un vendedor. Un Free
nace pagado, así que rotar número de WhatsApp y correo da un Free tras otro. El comprador no acepta
términos, privacidad ni DPA: el contrato no es exigible. Ante un ataque, el único freno es vaciar
`PROVISIONING_SELLER_TENANTS` y redesplegar. Los textos legales definitivos dependen del abogado
(HU-GTM-02) y todavía no existen.

## Decisión

El pedido de un plan a un vendedor pasa por tres controles en la tool y el alta por cuatro en el
consumidor; lo que no pasa se frena en el chat o queda en revisión humana. Los documentos legales
son versiones publicadas desde Plataforma, no código.

### 1. Documentos legales con versión (`documentos_legales`)

- Tabla de negocio **del vendedor** (`tenant_id`, RLS forzada). Tipos: `terminos`, `privacidad` y
  `dpa`. Cada fila es una versión con su enlace `https`.
- Las filas no se editan. Publicar es insertar una fila, y la vigente de cada tipo es la de
  `vigente_desde` más reciente. Cuando llegue el texto del abogado, el master publica otra versión
  desde Plataforma, sin tocar código.
- Solo el master publica (`POST /api/v1/admin/platform/altas-automaticas/{vendedor}/documentos`), y
  queda en `audit_log` del vendedor (`documento_legal_publicado`).

### 2. Al pedir el plan: `crear_pedido` llama al core

`core/provisioning/antiabuso.py` lo pueden importar los packs, igual que `fuentes`, porque no da de
alta negocios. El contrato `antiabuso-sin-alta` de `.importlinter` lo garantiza también de forma
indirecta: ni `antiabuso` ni `legal`, `interruptor`, `revision`, `vendedores` o `fuentes` llegan al
consumidor, al owner o a `alta_negocio`.

Si el negocio actual no es un vendedor (allowlist), no hace nada. Si lo es, la tool de commerce, antes
de crear el pedido de un plan, le pide la decisión al core:

- el core devuelve un resultado tipado: `TerminosPendientes`, `LimiteDeAltas`, `FreeNoDisponible` o
  el pedido preparado;
- el pack arma con eso la respuesta de su tool, con un `hint` corto como el resto de sus respuestas;
- el core no conoce el nombre ni los parámetros de la tool.

Los controles son:

1. **Términos aceptados.**
   - Con los tres documentos publicados, el pedido no se crea hasta que el cliente acepta.
   - La tool responde `terminos_pendientes` con los tres enlaces y un token (`acepta_terminos`). El
     asistente comparte los enlaces y, solo si el cliente dice que sí, repite la llamada con
     `customer_data.acepta_terminos` = ese token.
   - El token es un HMAC de la **conversación** y del juego de versiones. No sirve en otra
     conversación, y si entre que se mostró y que se aceptó se publica otra versión, cambia y se
     vuelven a mostrar.
   - Al mostrar los enlaces se anota en Redis (24 h) la hora, en la base, del último mensaje del
     cliente. El token solo vale si después hay un **mensaje del cliente** más nuevo, y ese mensaje es
     la evidencia que se guarda.
   - La huella del juego de versiones incluye la versión y el enlace de cada documento. Así, el modelo no puede
     repetir el token en el mismo turno ni traerlo de otra conversación. Sin Redis, no hay aceptación
     (falla cerrado) y se vuelven a mostrar los enlaces.
   - Sin los tres publicados, el pedido sigue sin aceptación y el alta va a revisión: un olvido del
     master no frena la venta, pero tampoco crea un negocio sin contrato.
2. **Cupo por origen.**
   - Hasta `ALTA_PEDIDOS_POR_HORA` (3 por defecto) pedidos de planes por hora desde el mismo cliente
     del chat (en WhatsApp, el número). Pasado el cupo, `limite_de_altas`.
   - Usa `core/security/rate_limit.py` (Redis, ventana deslizante y *fail-open*). Si Redis se cae, el
     resto de los controles sigue en la base.
   - Sin cliente ni conversación no hay un cupo propio: se frena, en vez de compartir uno entre todos.
   - Con los tres documentos publicados, sin Redis tampoco hay aceptación (falla cerrado). Si falta
     alguno, sin Redis el cupo no aplica, pero esa alta va a revisión de todos modos.
   - El chat web ya tiene su cupo por IP y Turnstile.
3. **Un Free por persona.**
   - Si el mismo cliente, el número de WhatsApp, el teléfono del checkout o el documento pidió un Free
     en los últimos 12 meses, la tool responde `free_no_disponible` y pide ofrecer un plan pago.
   - Free es el **plan** de la línea (el plan de la plataforma de la variante es Free, o el plan cuesta
     0), no el total de la cotización: un Free con un producto pago al lado sigue siendo un Free.
   - Va después del cupo, así probar documentos ajenos también gasta el cupo. El mensaje no dice cuál
     de los datos coincidió.
   - Solo cubre el documento y el teléfono del checkout si el vendedor los pide en
     `checkout_fields.plan` (por defecto pide el documento). Es configuración del vendedor y un
     requisito para encender el alta.

El registro del pedido (`pedidos_de_alta`, del vendedor) se escribe **en la misma transacción que el
pedido**. Si el pedido no se crea, tampoco queda la aceptación. Guarda:

- **Quién:** el cliente y la conversación.
- **Cuándo:** `aceptado_en`.
- **Qué versión:** tipo, versión y enlace de cada documento, y la huella del juego.
- **Evidencia:** el id del mensaje del cliente posterior a los enlaces, que es el que dijo que acepta.
- **Para las reglas:** si es Free, las huellas y las señales.

### 3. Datos mínimos

- El teléfono y el documento se guardan como `HMAC-SHA256(clave, tipo:valor)`. La clave sale de
  `APP_SECRET_KEY` con una etiqueta propia. Nunca se guarda el dato.
- Un SHA-256 a secas de un celular se revierte probando los 10^10 números. Con el HMAC, sin la clave
  no se puede.
- Antes se normaliza:
  - teléfono: los últimos 10 dígitos, así `+57 300…` y `300…` son el mismo;
  - documento: alfanuméricos en mayúsculas, sin el tipo delante (CC, CE, TI, NIT, PP, PA), sin ceros a
    la izquierda y un NIT escrito con guion sin su dígito de verificación. Un NIT con el dígito
    pegado no se distingue de otro número y da otra huella.
- Las señales se guardan como códigos (`documento_invalido`, `telefono_invalido`), sin el dato.
- **Retención:** un cron diario (`core.alta_huellas_retencion`) vacía las huellas de más de 12 meses,
  porque ya no sirven para ninguna regla. La fila se conserva: es la prueba del contrato.
- La finalidad (prevenir el abuso de la prueba gratis) se declara en el aviso de privacidad
  (HU-GTM-02).

### 4. Al dar de alta: revisión humana

El consumidor (ADR-0121 § 3), con los datos del pedido ya leídos, junta **todos** los motivos (también
los de ADR-0121) y, si hay alguno, deja el alta en `revision` con la lista completa y avisa por push a
los dueños del vendedor. Así quien la aprueba ve todo lo que se salta, por ejemplo que además de la
pausa es una recompra. Los motivos son:

- **Interruptor:** el master pausó el alta automática.
- **Aceptación:** no hay aceptación registrada para el pedido. Pasa con un pedido creado a mano o sin
  documentos publicados.
- **Un Free por persona:** el respaldo de la regla de la tool, para un pedido que no pasó por ella.
- **Señales de riesgo:**
  - un correo de un dominio desechable (lista en `core/provisioning/dominios_desechables.txt`, que
    también cuenta los subdominios);
  - muchos intentos: `ALTA_INTENTOS_REVISION` (3 por defecto) pedidos de alta de la misma persona en
    24 h;
  - datos inconsistentes: las señales del checkout.

- **ADR-0121:** Free repetido por cliente o correo, y recompra.

### 5. Interruptor del master

- Vive en `tenants.settings["aprovisionamiento"]` del vendedor. `tenants` es tabla de sistema: la
  escriben el master y el core, nunca un admin del negocio.
- Se escribe con un UPDATE que solo toca esa clave. Los otros que escriben `tenants.settings`
  (proveedor de suscripciones, preset) leen la fila con `FOR UPDATE`, así no pisan una pausa.
- Se pausa con un motivo obligatorio desde Plataforma
  (`PUT /api/v1/admin/platform/altas-automaticas/{vendedor}/interruptor`) o con
  `agent-core tenant interruptor-altas`. Queda en `audit_log` del vendedor.
- Pausado, las suscripciones se siguen registrando y cada alta nueva queda en revisión. Por eso las
  altas quedan "en cola": no se pierde ninguna.
- Al reanudar, las que quedaron en revisión se aprueban o se reintentan una por una.
- Una variable de entorno (como la allowlist) obligaría a redesplegar en medio de un ataque.

### 6. Aprobar un alta en revisión

Solo se aprueba un alta **en revisión por un control de riesgo** (`detalle.revision_por_riesgo`). Una
en revisión por un dato (correo inválido, cliente distinto, pedido sin pagar) se corrige y se
reintenta: aprobarla saltaría controles que nunca se evaluaron. El alta aprobada corre **sin** los
controles de riesgo que la frenaron:

- se saltan el interruptor, la aceptación, un Free por persona, las señales, el Free repetido y la
  recompra;
- siguen aplicando las validaciones de datos: plan, correo, pedido pagado, cliente y owner.

Cómo corre:

- `core/provisioning/revision.py` (`aprobar_alta`) deja la fila `en_curso` con `detalle.aprobacion`
  (un UPDATE condicional sobre `estado = 'revision'`, que suma la clave sin pisar el detalle) y emite
  `provisioning.alta_aprobada` por el outbox, en la misma transacción.
- El consumidor (`on_alta_aprobada`) la corre en el worker, con los reintentos del outbox. Si algo
  transitorio falla tres veces, vuelve a revisión.
- Ninguna ruta de la API llega al consumidor (`tests/test_provisioning_sin_llm.py`), y la API no
  necesita activar los plugins.

La aprobación es **de un solo uso**:

- si el alta vuelve a revisión por otro motivo, se borra y hay que volver a aprobarla mirando el
  motivo nuevo;
- `reintentar_alta`, que sirve para cuando se corrigió un dato, también la borra y vuelve a aplicar
  todos los controles.

Queda en `audit_log` (`aprovisionamiento_aprobado`), con quién aprobó y el motivo por el que estaba en
revisión.

Se aprueba de dos formas:

- desde Plataforma, con `POST …/altas/{referencia}/aprobar` (y `…/descartar`), solo el master:
  `tests/test_provisioning_sin_llm.py` comprueba que cada ruta de `platform_altas` empieza por
  `_require_master`;
- con `agent-core tenant aprobar-alta`, que además la corre en el acto.

### 8. La prueba no se edita

`documentos_legales` y la aceptación de `pedidos_de_alta` son la prueba del contrato. Un trigger
impide:

- editar una versión publicada (solo `deleted_at`);
- editar los datos y la aceptación de un pedido de alta (solo las huellas, `updated_at` y
  `deleted_at`);
- borrar filas.

`purge_tenant` desactiva los triggers, así que la purga de un negocio no se frena.

Los enlaces se validan como `https` con dominio y **solo ASCII**: sin homógrafos ni caracteres que
invierten el texto, porque llegan al chat del comprador.

### 7. Plataforma

`GET /api/v1/admin/platform/altas-automaticas` lista los vendedores de la allowlist, cada uno con:

- el interruptor;
- los documentos vigentes y los que faltan;
- cuántas altas hay en revisión.

Un id fuera de la allowlist responde 404. Ninguna respuesta lleva correo, teléfono ni documento del
comprador.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Guardar el documento o el teléfono en claro (o con SHA-256 a secas) | Datos personales que no hacen falta; un SHA-256 de un celular se revierte por fuerza bruta |
| Tabla de huellas aparte, una fila por dato | Más tablas e índices para decenas de altas por día: un arreglo en la fila del pedido alcanza (el índice `(tenant_id, created_at)` acota la búsqueda a 12 meses de un vendedor) |
| Los textos legales en variables de entorno o en el código | Cambiarlos obliga a redesplegar y no queda historial de versiones aceptadas |
| Interruptor en variable de entorno o en Redis | Redesplegar en medio de un ataque; Redis puede perder el estado y reabrir el alta sola |
| Frenar la venta si no hay documentos publicados | Un olvido del master corta las ventas; la revisión humana cubre el riesgo legal |
| Un control del Free solo en el consumidor | El comprador ya pagó o recibió el aviso; el criterio pide ofrecer un plan pago en la conversación |
| `acepta_terminos: true` sin huella | El modelo podría aceptar una versión que nunca mostró; la huella ata la aceptación al juego de versiones vigente |
| Aprobar un alta sin saltar los controles | Un alta frenada por el interruptor o por un Free repetido volvería a revisión cada vez |
| Correr el alta aprobada dentro de la petición del master | Un fallo transitorio la deja `en_curso` sin nadie que la reintente, la API tiene que activar los plugins y una ruta de la API llega al consumidor |
| Un token igual para todas las conversaciones (solo el juego de versiones) | El modelo podría traerlo de otra conversación o repetirlo en el mismo turno, sin que el cliente haya respondido |
| Que el core arme la respuesta de la tool | El core conocería el nombre y los parámetros de una tool de un pack, y los textos para el modelo quedarían en el core |
| Verificar el correo o el teléfono antes del alta (OTP) | Más fricción y otra integración; el enlace de acceso ya va solo por correo (ADR-0121), y las señales cubren lo demás. Se revisa si las señales no alcanzan |

## Consecuencias

**Positivas:**
- rotar número y correo ya no da un Free tras otro, si el documento o algún teléfono se repite;
- cada negocio creado tiene en el vendedor y en sí mismo (`tenants.settings["alta"]["aceptacion"]`,
  `audit_log`) qué versiones aceptó y cuándo;
- el master frena el alta automática en segundos sin redesplegar, y nada se pierde: queda en cola;
- llega el texto del abogado y se publica sin tocar código.

**Negativas / lo que aceptamos pagar:**
- **Riesgo aceptado: la aceptación pasa por el modelo.** El token exige un mensaje del cliente
  posterior a los enlaces, pero no lee qué dijo: un modelo que se equivoca podría pasar el token
  después de un "¿y cuánto cuesta?". Lo mitiga la evidencia: ese mensaje queda en la aceptación y la
  conversación se puede auditar.
- **Riesgo aceptado: oráculo y "squatting" del Free.** Con un número nuevo, `free_no_disponible`
  revela que un documento o teléfono ajeno ya tuvo un Free (una consulta por víctima, dentro del
  cupo). Quien conoce el documento de otro puede gastarle el Free. Lo mitigan el cupo y que la
  respuesta no dice qué dato coincidió. Si alguien legítimo queda bloqueado, el master crea el negocio
  a mano desde Plataforma.
- Un Free descartado por el master sigue contando para la regla: el motivo más común de descarte es el
  abuso.
- Quien rota número, correo **y** documento sigue sacando un Free. Las señales y el cupo lo hacen más
  caro, pero no lo cierran. Verificar el documento es un trabajo aparte.
- Rotar `APP_SECRET_KEY` cambia las huellas: durante 12 meses un Free anterior deja de reconocerse.
- Los textos que hay hoy (landing `/terminos` y `/privacidad`) son preliminares y no hay DPA
  publicado. Hasta que el master publique las tres versiones, toda alta va a revisión.
- El playbook de ventas (`solvershub-comercial`) tiene que contemplar `terminos_pendientes`,
  `free_no_disponible` y `limite_de_altas`. Es requisito para encender el alta (ADR-0123 § 10), no un
  "mientras tanto": el `hint` de la tool solo dice el siguiente paso.
- Con `APP_SECRET_KEY` en su valor por defecto, las huellas se pueden revertir. Producción ya la exige
  propia (`docs/despliegue-coolify.md`).

**Qué haría que revisáramos esto:**
- altas Free fraudulentas que pasan las reglas (habría que verificar el documento o el teléfono);
- un segundo vendedor con documentos o reglas distintas;
- que el abogado pida la aceptación con firma o con un canal distinto del chat.

## Impacto en el código

- `alembic/versions/0065_antiabuso_del_alta.py` y `core/db/models.py`: `documentos_legales` y
  `pedidos_de_alta`, con RLS forzada.
- `core/provisioning/legal.py`, `interruptor.py`, `antiabuso.py`, `revision.py` y
  `dominios_desechables.txt`. `core/shared/events.py` y `core/events/core_handlers.py`: el topic
  `provisioning.alta_aprobada`.
- `.importlinter`: el contrato `antiabuso-sin-alta`.
- `core/subscriptions/__init__.py` y `core/presets/apply.py`: leen el negocio con `FOR UPDATE` antes de
  escribir `tenants.settings`.
- `core/provisioning/consumer.py`:
  - los controles antes del alta;
  - la aceptación en el negocio nuevo;
  - `on_alta_aprobada` y `aprobar_y_procesar` (CLI);
  - `reintentar_alta` borra la aprobación;
  - los motivos de ADR-0121 se juntan con los nuevos.
- `packages/pack-commerce/pack_commerce/skills.py`: `crear_pedido` llama a los controles y registra
  el pedido de alta.
- `core/api/routers/platform_altas.py` y `docs/openapi.json`.
- `cli/commands/tenant.py`: `aprobar-alta` e `interruptor-altas`.
- `core/tasks/celery_app.py`: el cron `core.alta_huellas_retencion`.
- `core/config.py`: `ALTA_PEDIDOS_POR_HORA` y `ALTA_INTENTOS_REVISION`.
- `docs/runbooks/altas-en-revision.md` y `docs/despliegue-coolify.md`.

## Adenda (2026-10-09): el marcador de los términos es fijo por juego de versiones

Aprobada por el dueño el 2026-10-09 (opción «marcador fijo por versión»). Cambia el § 2.1:

- **Defecto.** Entre turnos el modelo solo ve texto, no el token. En el turno en que el cliente
  acepta, llama `crear_pedido` primero sin el token (o con `true`). Esa llamada volvía a anotar el
  marcador, ahora en el mensaje que acepta, así que el reintento con el token del mismo turno ya no
  tenía un mensaje posterior. Con los documentos publicados, nadie podía contratar por chat (journey
  05: 0/6).
- **Decisión.**
  - La clave en Redis lleva la huella del juego de versiones:
    `alta:terminos:<vendedor>:<conversación>:<huella>`.
  - Se escribe con `SET NX` (24 h): la anota la primera vez que se muestran los enlaces de ese juego,
    y las llamadas siguientes no la mueven.
  - Un juego de versiones nuevo arranca su propio marcador. El «sí» a la versión anterior no acepta
    la nueva: hace falta un mensaje del cliente posterior a los enlaces nuevos.
- **Sigue fallando cerrado.** Sin marcador (vencido o sin Redis), o sin un mensaje del cliente
  posterior, no hay aceptación. `true` o cualquier valor distinto del token no acepta.
- **El token se reemite.** No cambia mientras no cambien la conversación y el juego de versiones:
  cada `terminos_pendientes` devuelve el mismo. El `hint` de la tool lo dice: si el último mensaje
  del cliente ya acepta, repetir `crear_pedido` con el token en ese mismo turno. El token no es
  evidencia de consentimiento: solo ata la aceptación a la conversación y al juego de versiones. La
  evidencia es el mensaje del cliente posterior al marcador.
- **Lo que aceptamos.** Mientras el marcador vive (24 h), cualquier mensaje posterior del cliente
  sirve de evidencia, aunque entre los enlaces y la aceptación haya otros mensajes. Antes pasaba lo
  mismo hasta la siguiente llamada sin token. El riesgo aceptado del § Consecuencias no cambia: el
  modelo decide si el mensaje dice que sí, y ese mensaje queda en la aceptación.
- **Ráfagas.** No hay un candado de turnos por conversación. Un mensaje del cliente que se guarda
  mientras corre el turno que muestra los enlaces es posterior al marcador, aunque el cliente todavía
  no los haya visto. Antes, una llamada sin token de un turno paralelo volvía a anclar el marcador;
  ahora no. Para cerrarlo habría que exigir que el mensaje que acepta sea posterior al primer
  mensaje saliente después del marcador. Queda pendiente de decisión.
- **Un pedido posterior.** El marcador no se borra al crear el pedido. Durante 24 h, otro pedido de un
  plan en la misma conversación, con el mismo juego de versiones, se acepta con un mensaje nuevo del
  cliente: ya aceptó esas versiones en esa conversación.
- **Journeys.** El runner guarda cada mensaje del cliente en `messages`, como el consumidor. Sin eso,
  en un journey nunca había un mensaje posterior a los enlaces.
