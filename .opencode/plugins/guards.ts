import type { Plugin } from "@opencode-ai/plugin"

const PROD_PATTERNS = ["PROD_DATABASE_URL", "prod.supabase.co", "--force", "drop database", "truncate "]

const isMigrationSql = (p: string) => /\.sql$/i.test(p) && /migrations/i.test(p)

const block = (message: string): never => {
  throw new Error(message)
}

export const GuardsPlugin: Plugin = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool === "bash") {
        const cmd = String(output.args?.command ?? "")
        for (const pattern of PROD_PATTERNS) {
          if (cmd.toLowerCase().includes(pattern.toLowerCase())) {
            block(
              `BLOQUEADO: el comando contiene '${pattern}'.\n` +
                "Las operaciones sobre producción se ejecutan a mano, nunca desde una sesión.",
            )
          }
        }
        return
      }

      if (input.tool === "write" || input.tool === "edit") {
        const filePath = String(output.args?.filePath ?? "")
        if (!isMigrationSql(filePath)) return
        if (/\.down\.sql$/i.test(filePath)) return

        const content = JSON.stringify(output.args ?? {})
        if (!content.toLowerCase().includes("enable row level security")) {
          block(
            `BLOQUEADO: la migración ${filePath} no habilita RLS.\n` +
              "Toda tabla de negocio requiere: alter table <t> enable row level security;\n" +
              "más una policy de aislamiento por tenant_id. Ver .opencode/skills/db-migration.",
          )
        }
        if (!content.toLowerCase().includes("tenant_id")) {
          block(
            `BLOQUEADO: la migración ${filePath} no menciona tenant_id.\n` +
              "Si es intencional (tabla de sistema), documéntalo en un ADR y usa .down.sql.",
          )
        }
      }
    },
  }
}