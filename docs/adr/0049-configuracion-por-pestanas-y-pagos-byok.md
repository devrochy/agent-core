# ADR-0049 — Configuración del agente por pestañas y pagos BYOK en el pack

- **Estado:** aceptado
- **Fecha:** 2026-09-23

## Contexto

La configuración del agente vive dispersa y en inglés de formularios separados:
`/admin/configuracion` (identidad, tono, comportamiento, LLM BYOK, pausa) y
`/admin/spec` (persona, modelo, políticas, `raw_json`). Hay solapamiento de campos
(tono en dos lugares, escalamiento en `escalation_config` y en `spec.policies`) y
campos del contrato que no se editan por UI. Por otro lado, el cobro **BYOK del
tenant** (que el negocio cobre a *sus* clientes con *su* Wompi) ya existe en
`pack-payments` pero **no tiene pantalla de configuración** dentro de Configuración:
las credenciales se cargan por el formulario auto-generado del plugin, sin "probar
y guardar" ni URL de webhook visible.

## Decisión

1. **La sección de Configuración se organiza por pestañas** (Bootstrap, SSR): un
   `GET` arma el contexto y un `POST` por pestaña. La primera pestaña es
   **Identidad** (lo que hoy es `/admin/configuracion`). Las pestañas siguientes se
   agregan en tandas posteriores (Memoria, Caché, Cerebro, Modelos IA, Habilidades,
   Inhabilitar). No se introduce SPA ni JS más allá del componente de tabs.
2. **La pestaña "Pagos" la aporta el pack `pack-payments`** y **solo aparece si el
   pack está habilitado** para el tenant (`AgentSpec.packs`). El core no conoce el
   dominio de pagos: el pack registra su router admin vía el método
   `extra_admin_routers()` del contrato `BaseModule`, y el core los incluye al
   arrancar (solo si el prefijo empieza por `/admin`, si no lo ignora con warning).
   Así se respeta la regla de capas (invariante #5) sin acoplar el core. El acceso
   **directo** a la URL también exige el pack habilitado y la sección `settings`
   (no basta con ocultar la tab): la ruta se mapea a `settings` aunque viva bajo
   `/admin/plugins/<id>`, y el handler revalida permiso y enablement.
3. **La pantalla de pagos es BYOK**: `public_key` + `private_key` + `events_secret`
   por tenant, cifrados en `plugin_configs` (Fernet, ya existente). El ambiente se
   deduce del prefijo de la llave privada (`prv_test_`/`prv_prod_`), como agent-sale.
   Botón "Probar y guardar": crea un link real por el mínimo de Wompi
   (`MIN_AMOUNT_COP`); si falla, **no persiste**. La URL del webhook
   (`/webhooks/payments/wompi/<slug>`) se muestra para registrar en Wompi.
4. **El webhook ya existe** en el pack (`pack-payments/module.py`) y no se toca:
   verifica la firma con el `events_secret` del tenant e idempotencia por
   transición de estado.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Poner la config de pagos y el webhook en el **core** | Duplica el dominio Wompi que ya vive en `pack-payments` y rompe la separación core/pack. |
| Una sola página larga sin pestañas | El objetivo explícito era navegar por secciones; el formulario ya está creciendo. |
| SPA con JS para las tabs | El panel es SSR con Bootstrap; una SPA agrega una dependencia de build y no aporta. |
| Tab "Pagos" siempre visible (aunque falte el pack) | Ruido: si el negocio no tiene el pack, no hay nada que configurar. Se muestra solo con el pack habilitado. |
| Guardar credenciales aunque la prueba falle | Deja una config rota que falla en runtime; agent-sale tampoco lo hace. |

## Consecuencias

**Positivas:** configuración navegable por secciones; sumar una pestaña de pack no
toca el core (nuevo `extra_admin_routers`); el tenant valida sus llaves antes de
guardar; una única fuente de verdad por campo a medida que se migran los solapados.

**Negativas / lo que aceptamos pagar:** `extra_admin_routers` amplía el contrato de
`BaseModule` (aditivo, retrocompatible); las pestañas siguientes (memoria/caché/
cerebro/habilidades) todavía muestran la configuración dispersa hasta las tandas 2-3;
el core y el pack comparten plantillas (`base.html`) mediante un loader adicional.

**Qué haría que revisáramos esto:** que el número de pestañas crezca al punto de
necesitar navegación propia (sub-rutas, deep-links), o que un pack necesite más de un
router admin y `extra_admin_routers` se vuelva insuficiente.

## Impacto en el código

`core/admin/templates/settings.html` (tabs), `core/admin/views.py` (contexto de la
pestaña + `payments_enabled`), `core/mcp/types.py` (`extra_admin_routers`),
`core/main.py` (montaje de routers extra), `packages/pack-payments/pack_payments/`
(`admin.py`, `templates/pagos.html`, `module.py`). Tests
`tests/integration/test_config_pagos.py`. Pendiente (tandas 2-3): inventario del
contrato `AgentSpec` y unificación de fuentes.
