import type { Plugin } from "@opencode-ai/plugin"
import { spawnSync } from "node:child_process"
import { join } from "node:path"

// Par del hook PostToolUse de Claude Code (`.claude/hooks/ruff_on_edit.sh`): después de
// editar un `.py`, corre ese mismo script (`ruff check` sobre el archivo) y agrega los
// errores al resultado de la tool, para que el agente los vea y los corrija antes del CI.
// La regla vive en el script; este plugin solo traduce el evento de OpenCode.

export const RuffOnEditPlugin: Plugin = async ({ directory }) => {
  const root = directory ?? process.cwd()
  const script = join(root, ".claude", "hooks", "ruff_on_edit.sh")

  return {
    "tool.execute.after": async (input, output) => {
      if (input.tool !== "edit" && input.tool !== "write") return
      const filePath = input.args?.filePath
      if (typeof filePath !== "string" || !filePath.endsWith(".py")) return

      const result = spawnSync("bash", [script], {
        cwd: root,
        input: JSON.stringify({ tool_input: { file_path: filePath } }),
        encoding: "utf8",
        timeout: 30_000,
      })
      // El script sale con 2 cuando ruff encuentra problemas; cualquier otra salida pasa.
      if (result.status === 2 && result.stderr?.trim()) {
        output.output = `${output.output ?? ""}\n\n${result.stderr.trim()}`
      }
    },
  }
}
