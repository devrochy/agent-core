## Planes por agentes: agentes publicados, colaboradores y crédito mensual (HU-BILL-05, ADR-0142)

- Dos métricas nuevas en `core/billing/metrics.py`, siguiendo D-35:
  - `agentes`: especialistas activos con al menos una instancia viva en una conexión. El General cuenta y el
    Coordinador no.
  - `colaboradores`: vínculos con rol colaborador más las invitaciones pendientes.
- Catálogo (`core/billing/plans.py`) con las cifras de `modelo-de-precios.md §5`:

  | Plan | Agentes | Colaboradores | Crédito mensual |
  |---|---|---|---|
  | Free | 1 | 2 | 2.500 |
  | Basic | 2 | 5 | 20.000 |
  | Premium | 5 | 15 | 75.000 |
  | Empresarial | ∞ | 50 | 240.000 |

- `plugins` («Habilidades activas») deja de ser tope:
  - ya no hay 409 `plan_limit_plugins`;
  - `core/presets/apply.py` deja de rechazar presets por cantidad de packs.
- Al tope, la API responde 409 `plan_limit_agentes` en estas entradas:
  - `POST /admin/connections/{id}/agents`;
  - `POST /admin/agents/{id}/publish`, antes de publicar la versión;
  - la instancia de un preset, que queda con ese código.
- Al tope, la API responde 409 `plan_limit_colaboradores` en estas entradas:
  - `POST /admin/collaborators`;
  - `POST /admin/invitations`;
  - el alta de gestores de `pack_cobranza`;
  - en el panel clásico, el mismo mensaje aparece como aviso.
- El bloqueo solo frena el crecimiento: un negocio que ya supera el tope sigue operando con lo que tiene.
- `GET /admin/plan` cambia en tres puntos:
  - `usage` trae `agentes` y `colaboradores`;
  - nuevo `credit: {limit, used: null, percent: null}`, porque el uso en créditos llega con HU-BILL-04/06;
  - `plans[].monthly_credits`.
- Sin migración: `sync_plans` espeja el crédito en `plans.features.credito_mensual`.
