---
name: abrir-pr
description: >
  Abre un PR de agent-core hacia develop con todo verificado: fragmento de changelog,
  números de migración y ADR libres frente a develop y a los PRs abiertos, una sola cabeza de
  Alembic, tests sin nombres repetidos, ruff y pytest en verde, y el cuerpo armado con la
  plantilla del repo. Usar cuando el usuario diga "abre el PR", "sube esto a PR" o "ya está
  listo para PR". No mergea ni despliega.
argument-hint: "[título convencional, p. ej. feat(editor): … (HU-AOS-50)]"
---

Abre el PR de la rama actual hacia **`develop`**. `$ARGUMENTS` es el título si el usuario lo
dio; si no, propónlo (formato `tipo(ámbito): resumen (HU-XXX)`).

Los release a `main` van por la skill `release`, no por esta. Mergear y desplegar a test
va por `desplegar-test-agent-core`.

## Reglas que no se saltan

- **Nunca** `--no-verify`, `git push --force`, ni abrir el PR si el verificador da `ERROR`.
  Se arreglan (o se le explica al usuario por qué no se puede) antes de continuar.
- Si el árbol tiene cambios sin commitear, **pregunta** antes de commitear: no commitees por
  tu cuenta cosas que el usuario no te pidió incluir.
- Crear el PR es publicar: muestra título y cuerpo completos y **espera el OK del usuario**
  antes de `git push` y `gh pr create`.
- No marques casillas de la plantilla que no hayas verificado. Una casilla sin marcar con
  la nota «no aplica» es mejor que una marca falsa.

## Pasos

1. **Verificador.** Desde la raíz del repo o worktree:
   `python3 -I .claude/skills/abrir-pr/verificar_pr.py --titulo "<título>"`
   (hace `git fetch`; con `--sin-fetch` no). El hook `guards.py` lo vuelve a correr antes de
   `gh pr create` hacia `develop` y bloquea el comando si da `ERROR`. Revisa fragmento de changelog, migraciones
   (número libre en develop y en PRs abiertos, una cabeza de Alembic, tenant_id + RLS),
   ADR, nombres de test repetidos, `.env` versionados y el formato del título. Sale con 1
   si hay errores. Si el número de migración o de ADR choca, renumera **solo las líneas de
   tu commit** (`git show <sha> --unified=0`): un reemplazo global ya pisó referencias ajenas.
2. **Lint y tests.** El `.venv/bin` tiene el shebang roto: usa `python -m`.
   - `python -m ruff check` (el `pre-commit` ya lo corre, pero confírmalo).
   - `timeout 900 python -m pytest -q` **completo**, no por carpetas: recoge `tests/` e
     `integration/` juntos y así aparecen los choques de nombres que por separado no se ven.
   - En un worktree, el venv es el del checkout principal: exporta
     `PYTHONPATH=$PWD:$(ls -d $PWD/packages/*/ | tr '\n' :)` y comprueba con
     `python -c "import pack_commerce; print(pack_commerce.__file__)"` que sale del worktree.
3. **Revisiones según lo que tocó el diff** (`git diff --name-only origin/develop...HEAD`):

   | Si toca | Hacer |
   |---|---|
   | `core/db/`, consultas, migraciones, tools, packs o rutas de la API | subagente `auditor-multitenant` (CLAUDE.md lo exige antes de cerrar la tanda) |
   | `docs/openapi.json` o `core/api/` | subagente `contrato-api-reviewer`; si hay cambio incompatible, no abras el PR sin que el usuario lo vea |
   | prompts, playbooks, `AgentSpec` o tools | confirmar que `evals/cassettes/` está actualizado; **no** grabar con LLM real sin OK del usuario |
   | una decisión de arquitectura | skill `adr` (número verificado contra develop y PRs abiertos) |
   | `alembic/versions/` | skill `db-migration` si no se usó al crearla |

4. **Cuerpo del PR.** Lee `.github/pull_request_template.md` y rellénalo con lo que dicen
   los commits y el diff (`git log origin/develop..HEAD`, `git diff --stat`): qué cambia y
   por qué en lenguaje de producto, qué contrato/datos toca, cómo se probó (comandos y
   resultado reales de los pasos 1–3) y los riesgos o el orden de despliegue. Termina el
   cuerpo con la línea `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
   Guárdalo en un archivo temporal, no en el repo.
5. **Confirmar.** Muestra al usuario título + cuerpo + resultado del verificador, `ruff` y
   `pytest`. Con su OK:
   - `git push -u origin <rama>`
   - `gh pr create --base develop --title "<título>" --body-file <archivo>`
6. **Después.** Devuelve el enlace del PR. Si la app tiene las herramientas de PR, vincula el
   PR a la sesión (`get_status` y, si no aparece, `bind_pr`) y ofrece el Auto-fix de CI; no
   sondees el CI a mano. Si el CI sale rojo, el subagente `ci-triage` dice si es real o
   flaky. Mergear y desplegar a test: `desplegar-test-agent-core`, solo con OK del usuario.

## Informe final

Enlace del PR, resultado del verificador (errores resueltos y avisos), `ruff`/`pytest`, qué
revisiones de subagentes se hicieron y qué quedó fuera (por ejemplo, evals con LLM real sin
correr o contrato incompatible pendiente de decisión).
