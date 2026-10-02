import type { Plugin } from "@opencode-ai/plugin"
import { spawnSync } from "node:child_process"
import { join } from "node:path"

// Las reglas viven en UN solo lugar: `.claude/hooks/guards.py` (hook PreToolUse de
// Claude Code). Este plugin traduce el evento de OpenCode al formato de ese hook,
// ejecuta el script y aplica su decisión. Para cambiar una regla, se cambia allá.

const TOOL_NAMES: Record<string, string> = { bash: "Bash", write: "Write", edit: "Edit" }

type HookOutput = {
  hookSpecificOutput?: { permissionDecision?: string; permissionDecisionReason?: string }
}

export const GuardsPlugin: Plugin = async ({ directory }) => {
  const root = directory ?? process.cwd()
  const script = join(root, ".claude", "hooks", "guards.py")

  return {
    "tool.execute.before": async (input, output) => {
      const toolName = TOOL_NAMES[input.tool]
      if (!toolName) return

      const args = output.args ?? {}
      const toolInput =
        toolName === "Bash"
          ? { command: args.command }
          : {
              file_path: args.filePath,
              content: args.content,
              old_string: args.oldString,
              new_string: args.newString,
              replace_all: args.replaceAll,
            }

      const result = spawnSync("python3", [script], {
        input: JSON.stringify({ tool_name: toolName, tool_input: toolInput, cwd: root }),
        encoding: "utf8",
        timeout: 10_000,
      })
      // Fail-open, igual que el hook: si el script no responde, no se bloquea.
      if (result.error || !result.stdout?.trim()) return

      let decision: HookOutput
      try {
        decision = JSON.parse(result.stdout) as HookOutput
      } catch {
        return
      }
      const verdict = decision.hookSpecificOutput
      if (verdict?.permissionDecision === "deny") {
        throw new Error(verdict.permissionDecisionReason ?? "BLOQUEADO por .claude/hooks/guards.py")
      }
      if (verdict?.permissionDecision === "ask") {
        // Un plugin de OpenCode no puede pedir confirmación: se bloquea y lo corre el humano.
        throw new Error(
          `${verdict.permissionDecisionReason ?? "Requiere confirmación humana."}\n` +
            "En OpenCode esta guarda no puede preguntar: si corresponde, ejecútalo a mano.",
        )
      }
    },
  }
}
