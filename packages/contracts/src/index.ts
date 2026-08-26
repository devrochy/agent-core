/**
 * @agentcore/contracts — v1.0.0
 *
 * CONGELADO. Ver docs/adr/0003-contratos-congelados.md
 * Cualquier cambio de forma requiere un ADR nuevo y un incremento major.
 * Este paquete NO importa de ningún otro paquete del monorepo.
 */

// ─────────────────────────────────────────────────────────────
// 1. Evento canónico de entrada
// ─────────────────────────────────────────────────────────────

export type ChannelKind =
  | 'whatsapp' | 'instagram' | 'messenger' | 'telegram' | 'web' | 'email';

export type ContentPart =
  | { type: 'text'; text: string }
  | { type: 'image'; url: string; mimeType: string; caption?: string }
  | { type: 'audio'; url: string; mimeType: string; durationMs?: number }
  | { type: 'document'; url: string; mimeType: string; filename?: string }
  | { type: 'location'; lat: number; lng: number; label?: string }
  | { type: 'interactive'; payload: Record<string, unknown> };

export interface InboundMessage {
  tenantId: string;
  channelId: string;
  channel: ChannelKind;
  /** Clave de idempotencia. Ver ADR-0007. */
  externalMessageId: string;
  /** (channelId, contactExternalId). Clave del lock de conversación. Ver ADR-0006. */
  conversationKey: string;
  contact: {
    externalId: string;
    displayName?: string;
    phone?: string;
    email?: string;
  };
  content: ContentPart[];
  replyTo?: string;
  receivedAt: string;
  /** Payload original del canal, para auditoría. Nunca se pasa al modelo sin sanitizar. */
  raw: unknown;
}

// ─────────────────────────────────────────────────────────────
// 2. Definición del agente — declarativa, versionada, sin código
// ─────────────────────────────────────────────────────────────

export type Tone = 'formal' | 'neutral' | 'cercano' | 'informal';
export type EscalationState =
  | 'none' | 'requested' | 'queued' | 'assigned'
  | 'in_progress' | 'resolved' | 'abandoned' | 'expired';

export interface EscalationPolicy {
  triggers: string[];
  autoEscalateAfterTurns?: number;
  autoEscalateOnLowConfidence?: boolean;
  businessHours?: { tz: string; ranges: Array<{ day: number; from: string; to: string }> };
  outOfHoursMessage?: string;
}

export interface RagConfig {
  collections: string[];
  topK: number;
  minScore: number;
}

export interface ToolBinding {
  tool: string;
  enabled: boolean;
  scopes: string[];
  maxCallsPerConversation?: number;
  requiresConfirmation?: boolean;
}

export interface AgentSpec {
  id: string;
  version: string;
  tenantId: string;
  persona: {
    role: string;
    tone: Tone;
    warmth: 0 | 1 | 2 | 3 | 4 | 5;
    emojis: 'none' | 'sparse' | 'free';
    maxLength: number;
    forbiddenPhrases: string[];
    locale: string;
  };
  model: {
    primary: string;
    fallback?: string;
    router?: string;
    temperature: number;
  };
  memory: {
    shortTermTurns: number;
    summarize: boolean;
    rag?: RagConfig;
  };
  /** Índice de playbooks: nombre + descripción. El contenido se carga con load_playbook. */
  playbooks: Array<{ name: string; description: string }>;
  tools: ToolBinding[];
  policies: {
    escalation: EscalationPolicy;
    confirmBeforeWrite: boolean;
    maxToolCallsPerTurn: number;
    budgetUsdPerConversation?: number;
  };
  packs: string[];
}

// ─────────────────────────────────────────────────────────────
// 3. Herramienta
// ─────────────────────────────────────────────────────────────

export type SideEffect = 'read' | 'write' | 'irreversible';
export type Scope = string;
export type JSONSchema = Record<string, unknown>;

export interface ToolDefinition<I = unknown, O = unknown> {
  name: string;
  version: string;
  /** La lee el modelo en producción. Qué hace, cuándo usarla, cuándo NO. */
  description: string;
  input: JSONSchema;
  output: JSONSchema;
  sideEffect: SideEffect;
  scopes: Scope[];
  timeoutMs: number;
  /** Obligatorio para write e irreversible. */
  idempotencyKey?: (input: I) => string;
  execute(input: I, ctx: ToolContext): Promise<ToolResult<O>>;
}

export type ToolResult<O> =
  | { ok: true; data: O }
  | { ok: false; error: { code: string; message: string; retryable: boolean } };

// ─────────────────────────────────────────────────────────────
// 4. Contexto de ejecución — lo único que un plugin puede tocar
// ─────────────────────────────────────────────────────────────

export interface DomainEvent {
  type: string;
  tenantId: string;
  payload: Record<string, unknown>;
  occurredAt: string;
}

export interface ScopedDb {
  query<T>(sql: string, params?: unknown[]): Promise<T[]>;
  transaction<T>(fn: (tx: ScopedDb) => Promise<T>): Promise<T>;
}

export interface ScopedSecrets {
  get(name: string): Promise<string | null>;
}

export interface ScopedHttp {
  fetch(url: string, init?: RequestInit): Promise<Response>;
}

export interface Logger {
  debug(msg: string, meta?: Record<string, unknown>): void;
  info(msg: string, meta?: Record<string, unknown>): void;
  warn(msg: string, meta?: Record<string, unknown>): void;
  error(msg: string, meta?: Record<string, unknown>): void;
}

export interface ToolContext {
  tenantId: string;
  agentId: string;
  conversationId: string;
  traceId: string;
  /** Ya filtrado por tenant vía RLS. No expone el pool crudo. */
  db: ScopedDb;
  /** Solo los secretos declarados en el manifest del pack. */
  secrets: ScopedSecrets;
  /** Allowlist de dominios del manifest. */
  http: ScopedHttp;
  emit(event: DomainEvent): void;
  logger: Logger;
}

// ─────────────────────────────────────────────────────────────
// 5. Resultado del turno
// ─────────────────────────────────────────────────────────────

export interface OutboundMessage {
  channel: ChannelKind;
  content: ContentPart[];
  replyTo?: string;
}

export interface ToolCallTrace {
  tool: string;
  durationMs: number;
  ok: boolean;
  errorCode?: string;
}

export interface AgentTurnResult {
  messages: OutboundMessage[];
  stateChanges: DomainEvent[];
  handoff?: { reason: string; toState: EscalationState };
  usage: {
    model: string;
    inputTokens: number;
    outputTokens: number;
    cachedTokens: number;
    costUsd: number;
    toolCalls: ToolCallTrace[];
  };
}
