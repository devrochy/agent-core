# ADR-0027 — Cifrado de secretos en BD: AES-256-GCM con rotación de claves

- **Estado:** aceptado
- **Fecha:** 2026-09-19

## Contexto

Todo secreto persistido (`channel_connections.credentials_encrypted`,
`plugin_configs.config_encrypted`, `app_settings.llm_api_key_encrypted`) pasa
por `SecretBox` (`core/shared/crypto.py`), que hasta ahora era Fernet con
**una sola clave** (`MASTER_ENCRYPTION_KEY`): sin rotación (cambiar la clave
exige re-cifrar todo o perder acceso a lo viejo) y sin AAD (nada ata un
ciphertext a la fila donde vive). La custodia de tokens de Meta
(`meta_private.meta_tokens`, un token por App Secret/system user/etc.)
necesita las dos cosas: rotar claves sin downtime y atar cada secreto a su
`tenant_id`+`connection_id`+`kind` para que moverlo de fila (bug, migración
de datos) invalide el descifrado en vez de filtrar datos ajenos en silencio.

## Decisión

`SecretBox` pasa a cifrar con AES-256-GCM y rotación de claves por `key_id`
(`TOKEN_MASTER_KEYS` = `{key_id: clave_base64}`, `TOKEN_ACTIVE_KEY_ID` marca
con cuál se cifra contenido nuevo), manteniendo su API pública intacta
(`encrypt`/`decrypt`/`encrypt_json`/`decrypt_json`) para no tocar ningún
call-site existente. La migración es retrocompatible: `unseal()` detecta por
prefijo (`gcm:v1:...` vs sin prefijo) si un valor es del formato nuevo o
Fernet legado, y sigue pudiendo descifrar lo viejo con `MASTER_ENCRYPTION_KEY`
indefinidamente. No hay backfill masivo: cada fila migra sola la próxima vez
que se guarda por su flujo normal.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Backfill masivo (re-cifrar todo en la migración) | Bloqueante, riesgoso sobre datos de producción reales, y AES-GCM con AAD por fila no puede "re-cifrarse en bloque" sin conocer el AAD de cada fila individual. |
| Cifrado nuevo solo para `meta_tokens`, dejar Fernet en todo lo demás | Dos esquemas de cifrado activos a la vez en el mismo repo — más superficie de mantenimiento sin beneficio real (decisión inicial del usuario, revertida a favor de unificar). |
| Mantener Fernet y agregar rotación manual (múltiples `Fernet` con `MultiFernet`) | `MultiFernet` de la propia librería resuelve rotación pero no da AAD — no cubre el requisito de atar el secreto a la fila. |

## Consecuencias

**Positivas:** rotación de claves real sin downtime; AAD como segundo
cinturón de aislamiento en `meta_tokens` (además de RLS); un solo esquema de
cifrado en todo el repo.

**Negativas / lo que aceptamos pagar:** `MASTER_ENCRYPTION_KEY` queda viva
indefinidamente como dependencia de solo-lectura hasta confirmar que no
queda ningún dato en formato Fernet en ningún despliegue; dos rutas de
descifrado activas (nuevo + legado) es más superficie de código que un
esquema único.

**Qué haría que revisáramos esto:** cuando se confirme (auditoría de todas
las filas `*_encrypted` en producción) que ninguna sigue en formato Fernet,
eliminar `_unseal_legacy_fernet()` y `MASTER_ENCRYPTION_KEY` del todo.

## Impacto en el código

`core/shared/crypto.py` (reescrito), `core/config.py` (`TOKEN_MASTER_KEYS`,
`TOKEN_ACTIVE_KEY_ID`), `.env.example`, `cli/commands/init.py` (genera las
claves nuevas en `agent-core init`), `core/gateway/meta_token_store.py`
(único consumidor de `seal_parts`/`unseal_parts`, la variante con AAD).
