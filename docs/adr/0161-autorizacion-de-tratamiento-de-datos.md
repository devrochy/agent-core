# ADR-0161 — Autorización de tratamiento de datos en `customer_consents`

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-18 (captación), P-29. **Amplía:** ADR-0150 (propósitos del consentimiento; 0150 está propuesto y se acepta antes que este). **Usa:** ADR-0053 (PII).

## Contexto

La Ley 1581 de 2012 exige autorización previa, expresa e informada para tratar datos personales, con prueba y referencia a la política vigente. La captación de un propietario guarda su ubicación, fotos y documento. ADR-0150 ya creó `customer_consents` en el core: append-only, con evidencia y la última fila manda. Pero su CHECK solo admite `proposito = 'promociones'` y no guarda la versión del aviso.

## Decisión

La autorización de tratamiento de datos se registra en `customer_consents`, en el core y por cliente, reutilizable por cualquier habilidad.

1. Se amplía `ck_customer_consents_proposito` a `('promociones', 'tratamiento_datos')`.
2. Se agrega la columna `version_aviso text` (nullable; obligatoria por CHECK cuando el propósito es `tratamiento_datos`), con la versión de la política o aviso de privacidad aceptado.
3. Los orígenes nuevos son `chat` (aceptación en la conversación, después de enviar el enlace de la política) y `formulario` (formulario público con Turnstile). `origen` no tiene CHECK en la base (migración 0100): lo valida la función del core.
4. La evidencia sigue siendo texto acotado (≤ 500 caracteres): id del mensaje o del envío del formulario, nunca el dato personal.
5. Función del core `core/consentimiento.py`: `registrar(customer_id, *, proposito, estado, origen, evidencia, version_aviso=None)` y `vigente(customer_id, proposito)`. Los packs nunca escriben en la tabla directamente. `registrar_consentimiento` de `core/admin/envios_segmento.py` pasa a usarla.
6. **Las bajas se acotan por propósito.** La palabra de baja (`registrar_baja_si_corresponde`) y el 131050 de Meta (`core/tasks/outbound_send.py`) revocan solo `promociones`. Revocar `tratamiento_datos` es una acción explícita y dispara la supresión del titular en los packs (ADR-0164).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla de autorizaciones en el pack inmobiliario | Cada habilidad repetiría la prueba legal y la supresión. La autorización es del titular, no de la vertical |
| Guardarla en hechos `perfil` | Los hechos se reescriben. La prueba legal necesita historia append-only |

## Consecuencias

**Positivas:** una sola fuente de prueba de la autorización, con su versión, para todas las habilidades y para HU-GTM-02.

**Negativas / lo que aceptamos pagar:** una migración del core que amplía un CHECK y agrega una columna.

**Qué haría que revisáramos esto:** que el abogado (HU-GTM-02) exija guardar el texto completo aceptado o una firma, lo que pediría almacenamiento de documentos.

## Impacto en el código

- `alembic/versions/` (migración del core: CHECK de propósito y `version_aviso`).
- `core/db/models.py` (`CustomerConsent`).
- `core/consentimiento.py` (nuevo) y `core/admin/envios_segmento.py` (pasa a usarlo).
- Helper de Turnstile del core, extraído de `pack_commerce/api.py` (`_verify_turnstile`), para el formulario público.
- Captación del pack inmobiliario.
