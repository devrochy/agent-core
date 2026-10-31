Fixtures congeladas del procedimiento `contratar_plan` que tenía `pack_commerce` antes de SPEC-43 (PR-4).
Sirven como forma de ejemplo para las pruebas del motor, los rótulos y la confirmación de datos; sus acciones
(`informar_plan`, `cotizar_plan`...) ya no existen, por eso viven aparte de `tests/fixtures/procedures/`, que el CI
valida con `agent-core procedures lint`.
