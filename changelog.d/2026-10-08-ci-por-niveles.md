## CI por niveles: las PRs a develop esperan minutos, no media hora (2026-10-08)

- ADR-0120. Las PRs a develop corren un nivel rápido:
  - lint, tipos, capas, unitarios y gitleaks sobre los commits de la PR;
  - si la PR toca código que cubre la integración, solo el subconjunto crítico de integración (`-m aislamiento`: RLS, aislamiento entre tenants, webhooks, IDOR).
- La integración completa, el gate de evals y los journeys corren en el push a develop y en todo lo que va hacia main. Hacia main también se exige que `docs/openapi.json` no tenga drift.
- El Postgres del CI ahora es la misma imagen de producción (pg17 + pgvector + PostGIS, publicada en GHCR). Ya no se instala PostGIS en cada corrida.
- Los services usan puertos dinámicos, así que se puede sumar un 2º runner en el mismo host (`docs/WORKFLOW.md`).
- La instalación usa `uv` con caché persistente en el runner. `scripts/install_plugins.sh` respeta `$PIP`.
- No se despliega a test hasta que el CI del push a develop esté en verde (skill `desplegar-test-agent-core`).
