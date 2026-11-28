## Abono único de referidos en cualquier ciclo (2026-11-27)

- HU-REF-02 (SPEC-55, ADR-0214 §6): `core/billing/bonificacion.py::acreditar_bonificacion_unica` busca el
  `origen` (`referral:<id>:referidor` / `referral:<id>:referido`) en cualquier ciclo antes de llamar a
  `acreditar_bonificacion`. La unicidad de `credit_grants` incluye `ciclo`: sin esto, un reintento tras
  `cerrar_ciclo` duplicaría el regalo, y uno ya gastado ni se arrastra ni lo protege.
- Devuelve `acreditada`, `ya_acreditada` o `sin_ciclo`, con las mismas validaciones y el mismo scope.
  `acreditar_bonificacion` no cambia. Quien llama sostiene `pg_advisory_xact_lock('billing:creditos:<tenant>')`.
- Sin migración ni endpoint nuevo; todavía no la llama nadie (el cron de liberación llega en HU-REF-08).
