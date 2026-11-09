## Qué cambia

<!-- Qué hace el PR, en lenguaje de producto. HU / ADR / issue relacionados en el título o aquí. -->

-

## Por qué

<!-- El problema o la decisión detrás. Si es un fix: qué pasó exactamente y cómo se vio. -->

## Contrato y datos

<!-- Marca lo que aplica y borra el resto. «Ninguno» es una respuesta válida. -->

- [ ] **API:** rutas o schemas de `/api/v1` cambiados → `docs/openapi.json` regenerado. Compatible hacia atrás (la APK instalada no se actualiza con el backend): sí / no, y por qué.
- [ ] **Migración:** número `NNNN` verificado contra `origin/develop` y los PRs abiertos; tablas nuevas con `tenant_id` + RLS forzada + policy.
- [ ] **ADR:** nuevo o enmendado (`docs/adr/NNNN-…`), número verificado.
- [ ] **Variables de entorno / config nuevas** (qué entorno las necesita):
- [ ] **Cambia un prompt, playbook o tool** → replay de evals actualizado (`evals/cassettes/`; regrabar con LLM real solo con OK del dueño).
- [ ] Ninguno de los anteriores.

## Cómo se probó

<!-- Comandos y resultado, no «probado». Tests nuevos, evals, prueba manual en test. -->

- [ ] `ruff check` y `pytest -q` completo
- [ ] `auditor-multitenant` si toca consultas, migraciones, tools, packs o rutas
-

## Riesgos y despliegue

<!-- Orden de despliegue si importa (migración antes que código, panel/APK después), cómo se revierte, qué vigilar. -->

## Checklist

- [ ] Fragmento en `changelog.d/AAAA-MM-DD-<tema>.md`
- [ ] Sin secretos, `.env` ni datos de clientes
- [ ] Base `develop` (los release a `main` van por la skill `release`)
