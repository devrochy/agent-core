# agentcore-contracts

Contratos congelados v3.1.0 en Python (ADR-0011; major a v2.0.0 por ADR-0050; `procedure/1`
aditivo en v2.1.0 y major a v3.0.0 con `agentspec/3` y `kernelspec/1`, ambos por ADR-0113).
`AgentSpec` es el **v2** (lo leen el loop legacy, los presets y el alta); `AgentSpecV3` y
`KernelSpec` son los contratos del Agent OS. Solo Python: el espejo TS se retiró en 3.0.0.
No importa de ningún otro paquete.
Cualquier cambio de forma requiere un ADR nuevo y un incremento major. Excepción: un campo
**opcional con default** nuevo en un modelo v3 existente es un **minor** (adenda de ADR-0113
del 2026-10-20; primer caso, `AgentSpecV3.knowledge` en 3.1.0 por ADR-0145).
