## Antiabuso y cumplimiento del alta automática (2026-10-09)

- HU-SH-07 (ADR-0125). Los controles se aplican cuando un negocio vendedor (`PROVISIONING_SELLER_TENANTS`)
  vende un plan de la plataforma. Fuera de los vendedores no cambia nada.
- **Términos aceptados.** `crear_pedido` de un plan no crea el pedido hasta que el cliente acepta términos,
  privacidad y DPA en su versión vigente:
  - responde `terminos_pendientes` con los tres enlaces y un token (`acepta_terminos`): HMAC de la
    conversación y del juego de versiones;
  - con el token en `customer_data.acepta_terminos` **y un mensaje del cliente posterior a los enlaces**,
    crea el pedido y registra en `pedidos_de_alta` quién, cuándo, qué versiones y ese mensaje;
  - el core decide (resultados tipados) y `pack_commerce` arma la respuesta de su tool;
  - el negocio creado guarda la aceptación en `tenants.settings["alta"]["aceptacion"]` y en `audit_log`
    (`negocio_creado`).
- **Documentos legales con versión.** `documentos_legales` guarda las versiones y nunca se edita: una
  versión nueva es una fila nueva, publicada por el master desde Plataforma. **Los textos definitivos dependen
  del abogado (HU-GTM-02):** al llegar se publica una versión nueva, sin tocar código. Mientras falte alguno
  de los tres, toda alta va a revisión.
- **Un Free por persona.** Si el mismo cliente, número de WhatsApp, teléfono o documento pidió un Free en los
  últimos 12 meses, la tool responde `free_no_disponible` y pide ofrecer un plan pago. Free es el plan de la
  línea, no el total de la cotización. El consumidor lo vuelve a comprobar (revisión).
  - Teléfono y documento se guardan como HMAC-SHA256 con una clave derivada de `APP_SECRET_KEY`, nunca en
    claro.
  - El cron diario `core.alta_huellas_retencion` vacía las huellas de más de 12 meses.
- **Límite por origen.** Hasta `ALTA_PEDIDOS_POR_HORA` (3) pedidos de planes por hora desde el mismo
  cliente del chat. Pasado el cupo, `limite_de_altas`. Usa el limitador compartido `core/security/rate_limit.py`.
- **Revisión humana.** El alta queda en revisión, con todos los motivos y un push a los dueños del vendedor,
  en estos casos:
  - el interruptor está pausado;
  - falta la aceptación;
  - un Free repetido;
  - un correo de dominio desechable (`core/provisioning/dominios_desechables.txt`);
  - `ALTA_INTENTOS_REVISION` (3) pedidos de alta de la misma persona en 24 h;
  - datos inconsistentes en el checkout (documento o teléfono inválidos).
  El motivo junta todos los controles que fallaron, también los de ADR-0121 (Free repetido, recompra).
- **Interruptor del master.** Se guarda en `tenants.settings["aprovisionamiento"]` del vendedor y queda
  auditado. Con el alta pausada, las altas nuevas quedan en cola de revisión. El proveedor de suscripciones
  y el preset leen el negocio con `FOR UPDATE` antes de escribir `settings`, así no pisan una pausa.
- **Aprobar un alta en revisión.** Solo una en revisión por un control de riesgo (una por un dato se
  corrige y se reintenta). Descartar toma el mismo candado que el consumidor. Queda `en_curso` y el worker la corre por el outbox
  (`provisioning.alta_aprobada`), sin los controles de riesgo que la frenaron; las validaciones de datos
  siguen. La aprobación es de un solo uso: si vuelve a revisión, o se reintenta, se borra. Queda auditada.
- **La prueba no se edita.** Triggers impiden editar o borrar una versión publicada y la aceptación de un
  pedido de alta (`purge_tenant` no se frena). Los enlaces legales son `https` y solo ASCII.
- Contrato nuevo de import-linter `antiabuso-sin-alta`: lo que usan los packs y la API del aprovisionamiento
  no llega al alta, ni de forma indirecta.
- **API de Plataforma**, solo para el master y solo sobre vendedores:
  `/api/v1/admin/platform/altas-automaticas` (estado, interruptor, documentos, cola, aprobar y descartar).
- **CLI:** `agent-core tenant aprobar-alta` y `agent-core tenant interruptor-altas`.
- Migración `0065` (`documentos_legales` y `pedidos_de_alta`, RLS forzada, triggers de inmutabilidad).
- `test_despachador_entrega_con_el_tenant_del_evento` despacha por lotes hasta encontrar su evento: con la
  suite completa hay más de un lote pendiente.
- `docs/openapi.json`, runbook `docs/runbooks/altas-en-revision.md` y `docs/despliegue-coolify.md` al día.
- ADR-0123 § 10 suma un requisito para encender el alta en producción: los documentos publicados, el
  playbook de ventas al día y los criterios de HU-SH-07 verificados en test.
