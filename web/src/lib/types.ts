// Shapes of the console API (raiden/console/api/serial.py).

export type Role = "admin" | "operator" | "viewer";
export type Env = "production" | "staging" | "development";
export type Severity = "sev1" | "sev2" | "sev3" | "sev4";
export type Stage = "detected" | "triage" | "diagnose" | "remediate" | "verify" | "harden" | "resolved";
export type Mode = "auto" | "human";
export type ServerStatus = "healthy" | "warning" | "critical" | "unreachable" | "paused" | "unknown";

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  disabled: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface AuthStatus {
  needs_setup: boolean;
  demo: boolean;
  version: string;
  demo_login?: { email: string; password: string };
  /** Demo only: a simulated server that is not in the estate yet, for trying onboarding. */
  demo_onboarding?: {
    name: string; host: string; port: number; dbname: string; username: string; platform: string; environment: Env; version: string;
    description: string; owner: string; tags: string; maintenance_window: string; ram_gb: number; vcpus: number; storage: string; workload: string;
  };
}

export interface Server {
  id: string;
  name: string;
  environment: Env;
  platform: string;
  platform_label: string;
  host: string;
  port: number;
  dbname: string;
  username: string;
  has_password: boolean;
  sslmode: string;
  databases: string[];
  azure_resource_id: string | null;
  tags: Record<string, string>;
  owner: string;
  description: string;
  maintenance_window: string;
  collect_interval_s: number;
  enabled: boolean;
  autonomy: Partial<Record<Stage, Mode>>;
  status: ServerStatus;
  status_reason: string;
  last_seen_at: string | null;
  last_error: string;
  color_slot: number;
  version: string;
  in_recovery: boolean;
  simulated: boolean;
  region: string;
  sku: string;
  sources: string[];
  host_agent: boolean;
  agent_last_seen_at: string | null;
  has_agent_token: boolean;
  capabilities: Record<string, boolean>;
  host_info: { ram_gb?: number; vcpus?: number; storage?: string; workload?: string };
  replica_of: string | null;
  uptime_s: number | null;
  extensions: string[];
  created_at: string;
}

export interface Chip {
  title: string;
  category: string;
  source: string;
  id?: number;
  severity?: Severity;
}

export interface EstateRow extends Server {
  metrics: Record<string, number>;
  spark: Record<string, number[]>;
  chips: { issues: Chip[]; suggestions: Chip[] };
  incidents: { open: number; worst: Severity | null; ids: string[] };
  supervised_stages: Stage[];
  assessment: { score: number; finished_at: string | null } | null;
}

export interface Suggestion {
  id: number;
  server_id: string;
  kind: "issue" | "suggestion";
  category: string;
  title: string;
  detail: string;
  source: string;
  status: string;
  data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Approval {
  id: number;
  incident_id: string | null;
  conversation_id: string | null;
  server_id: string | null;
  kind: "gate" | "action" | "hardening";
  stage: string;
  title: string;
  summary: string;
  payload: Record<string, any>;
  risk: "low" | "medium" | "high";
  status: "pending" | "approved" | "rejected" | "cancelled";
  requested_at: string;
  decided_at: string | null;
  decided_by: string | null;
  comment: string;
  server_name?: string;
  incident_title?: string | null;
  severity?: Severity | null;
  incident_stage?: Stage | null;
  learning?: ApprovalLearning;
}

export interface PlanItem {
  id: number;
  action: string;
  args: Record<string, unknown>;
  rationale: string;
  risk: "low" | "medium" | "high";
  title: string;
  status: string;
  approved_by?: string;
  result?: string;
}

export interface HardenItem {
  id: number;
  kind: "monitor" | "action" | "routine" | "lesson" | "recommendation";
  title: string;
  detail?: string;
  spec?: Record<string, unknown>;
  risk?: string;
  status: string;
  approved_by?: string;
  result?: string;
}

export interface IncidentCard {
  id: string;
  number: number;
  server_id: string | null;
  server_name: string | null;
  title: string;
  severity: Severity;
  category: string;
  source: string;
  source_ref: string;
  stage: Stage;
  step: string;
  state: "queued" | "running" | "waiting" | "idle" | "escalated" | "done";
  assignee: string;
  waiting_reason: string;
  detected_at: string;
  updated_at: string;
  resolved_at: string | null;
  mitigated_at: string | null;
  acknowledged_at: string | null;
  verify_until: string | null;
  tokens: number;
  cost_usd: number;
  tool_calls: number;
  model: string;
  attempts: number;
  hypothesis: string;
  root_cause: string;
  evidence: Record<string, any>;
  pending_approvals: Approval[];
  plan_size: number;
}

export interface IncidentEvent {
  id: number;
  ts: string;
  kind: string;
  actor: string;
  stage: string;
  title: string;
  body: string;
  data: Record<string, any>;
}

export interface IncidentDetail extends IncidentCard {
  summary: string;
  plan: PlanItem[];
  hardening: HardenItem[];
  report: string;
  stage_times: Record<string, string>;
  labels: string[];
  tokens_in: number;
  tokens_out: number;
  tokens_cached: number;
  recurrence_of: string | null;
  events: IncidentEvent[];
  approvals: Approval[];
  session: { turns: number; tool_calls: number; model: string; status: string } | null;
  autonomy?: Record<string, Mode>;
  environment?: Env;
  platform?: string;
}

export interface MetricDef {
  id: string;
  name: string;
  unit: string;
  category: string;
  description: string;
  sources: string[];
  azure: string | null;
  dimension: string | null;
  warn: number | null;
  crit: number | null;
  lower_is_worse: boolean;
  key: boolean;
}

export interface ActionDef {
  id: string;
  risk: "low" | "medium" | "high";
  title: string;
  description: string;
  reversible: boolean;
  args: Record<string, { type: string }>;
  required: string[];
}

export interface Catalog {
  categories: { id: string; name: string }[];
  metrics: MetricDef[];
  platforms: { id: string; name: string }[];
  actions: ActionDef[];
}

export interface Overview {
  range: string;
  start: string;
  end: string;
  kpis: {
    servers: number;
    healthy: number;
    warning: number;
    critical: number;
    unreachable: number;
    paused: number;
    unknown: number;
    open_incidents: number;
    waiting_for_people: number;
    escalated: number;
    resolved_30d: number;
    autopilot_resolved_30d: number;
    mttr_median_s: number | null;
    actions: number;
    tokens: number;
    cost: number;
    cost_month: number;
  };
  needs_you: Approval[];
  attention: { kind: string; title: string; detail: string; servers: string[]; link: string }[];
  timeline: {
    servers: { id: string; name: string; environment: Env; status: ServerStatus; color_slot: number }[];
    incidents: { id: string; server_id: string; title: string; severity: Severity; stage: Stage; state: string; start: string; end: string | null }[];
    decisions: { ts: string; incident_id: string; server_id: string | null; actor: string; kind: string }[];
  };
  fleet: Record<string, { server_id: string; name: string; value: number; color_slot: number }[]>;
  activity: { ts: string; incident_id: string; server_id: string | null; kind: string; title: string; actor: string }[];
}

export interface Series {
  server_id: string;
  range: string;
  start: number;
  end: number;
  series: Record<string, [number, number][]>;
  latest: Record<string, number | null>;
}

export interface FleetSeries {
  metric: string;
  range: string;
  start: number;
  end: number;
  series: { server_id: string; name: string; environment: Env; color_slot: number; points: [number, number][] }[];
}

export interface Table {
  columns: string[];
  rows: any[][];
  note?: string;
  [extra: string]: any;
}

export interface Monitor {
  id: number;
  key: string | null;
  name: string;
  description: string;
  metric: string;
  dimension: string | null;
  aggregation: "avg" | "max" | "min" | "last";
  operator: ">" | ">=" | "<" | "<=";
  threshold: number;
  window_minutes: number;
  severity: Severity;
  category: string;
  scope: { environments?: Env[]; servers?: string[]; tags?: Record<string, string>; role?: string };
  action: "investigate" | "notify" | "suggest";
  runbook: string;
  enabled: boolean;
  builtin: boolean;
  created_by: string;
  firing: string[];
  created_at: string;
}

export interface RoutineRun {
  id: number;
  routine_id: number;
  started_at: string;
  finished_at: string | null;
  status: "running" | "ok" | "warn" | "crit" | "error";
  summary: string;
  triggered_by: string;
  tokens: number;
  results?: { server_id: string; status: string; summary: string; findings: Finding[]; assessment_id?: number }[];
  report?: string;
}

export interface Routine {
  id: number;
  key: string | null;
  name: string;
  description: string;
  kind: "check" | "agent" | "dream";
  checks: string[];
  prompt: string;
  cron: string;
  schedule: string;
  timezone: string;
  scope: { environments?: Env[]; servers?: string[] };
  on_finding: "suggest" | "incident" | "report";
  enabled: boolean;
  builtin: boolean;
  last_run_at: string | null;
  last_status: string;
  next_run_at: string | null;
  recent_runs: RoutineRun[];
}

export interface Endpoint {
  id: number;
  name: string;
  provider: "azure_openai" | "openai" | "anthropic" | "openai_compatible" | "demo";
  base_url: string;
  model: string;
  api_version: string;
  wire_api: "auto" | "chat" | "responses";
  auth: "entra" | "api_key" | "none";
  has_api_key: boolean;
  reasoning_effort: string;
  max_output_tokens: number;
  price_in_per_m: number;
  price_out_per_m: number;
  price_cached_per_m: number;
  daily_token_budget: number | null;
  monthly_cost_budget: number | null;
  purposes: string[];
  priority: number;
  enabled: boolean;
  simulated: boolean;
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_test_message: string;
  last_latency_ms: number | null;
  tokens_today: number;
  cost_month: number;
}

export interface Usage {
  days: number;
  daily: { day: string; endpoint_id: number; endpoint: string; purpose: string; tokens_in: number; tokens_out: number; tokens_cached: number; cost_usd: number; calls: number }[];
  by_server: { server_id: string; tokens: number; cost_usd: number }[];
  totals: { tokens: number; cost_usd: number; calls: number };
}

export interface Finding {
  id: string;
  area: string;
  severity: "issue" | "suggestion" | "info" | "ok";
  title: string;
  detail: string;
  recommendation: string;
  evidence: string;
  fix: { kind?: "action" | "manual" | "provider"; action?: string; args?: Record<string, unknown>; how?: string; sql?: string; restart?: boolean };
}

export interface Assessment {
  id: number;
  server_id: string;
  created_at: string;
  finished_at: string | null;
  status: "running" | "done" | "error";
  trigger: string;
  triggered_by: string;
  score: number;
  scores: Record<string, number>;
  error: string;
  counts: { issue: number; suggestion: number };
  findings?: Finding[];
  tuning?: { name: string; current: string; recommended: string; reason: string; restart: boolean; fix: Finding["fix"] }[];
  facts?: Record<string, any>;
  summary?: string;
  tokens?: number;
}

export interface ServerDetail extends Server {
  chips: { issues: Chip[]; suggestions: Chip[] };
  suggestions: Suggestion[];
  incidents: IncidentCard[];
  assessment: Assessment | null;
  firing: { monitor: string; severity: Severity; since: string; value: number | null }[];
  metrics: Record<string, number>;
}

export interface Policy {
  stages: { id: Stage; title: string; verb: string; auto: string; human: string }[];
  environments: Env[];
  policy: {
    environments: Record<Env, Record<string, Mode>>;
    high_risk_always_human: boolean;
    budget_exhausted: string;
    verify_window_s: number;
    max_auto_actions: number;
    learn_approvals: boolean;
    learn_after: number;
  };
  overrides: { id: string; name: string; environment: Env; autonomy: Record<string, Mode> }[];
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "tool" | "approval" | "error";
  content: string;
  data: Record<string, any>;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  server_id: string | null;
  user: string;
  incident_id: string | null;
  updated_at: string;
  messages?: ChatMessage[];
  approvals?: Record<string, Approval>;
  busy?: boolean;
}

export interface AuditEntry {
  id: number;
  ts: string;
  actor: string;
  actor_type: string;
  action: string;
  target: string;
  decision: string;
  detail: Record<string, unknown>;
}

export interface SettingsPayload {
  general: { organization: string; retention_raw_days: number; retention_rollup_days: number; timezone: string };
  notifications: { webhooks: { name: string; url: string; kind: string; events: string[]; enabled: boolean }[] };
  system: { version: string; demo: boolean; database: string; data_dir: string; base_url: string; max_ai_sessions: number; uptime_s: number; live_listeners: number };
}

// ------------------------------------------------------------------------------------------------ what rAIden learned
export type KnowledgeKind = "rule" | "fact" | "preference" | "skill";
export interface KnowledgeScope { server?: string; environment?: Env; category?: string }
export interface Evidence { type: "decision" | "note" | "chat" | "incident" | "suggestion"; id: string; by?: string | null; text?: string; words?: boolean; chat?: string }

export interface Knowledge {
  id: number;
  kind: KnowledgeKind;
  title: string;
  body: string;
  scope: KnowledgeScope;
  scope_text: string;
  source: "person" | "decision" | "incident" | "dream";
  author: string;
  evidence: Evidence[];
  retention: "permanent" | "durable" | "ephemeral";
  expires_at: string | null;
  status: "active" | "proposed" | "retired" | "expired";
  uses: number;
  last_used_at: string | null;
  history: { kind: KnowledgeKind; title: string; body: string; scope: KnowledgeScope; status: string; retention: string; at: string; by: string; why: string }[];
  created_at: string;
  updated_at: string;
}

export interface LearnedApproval {
  id: number;
  action: string;
  action_title: string;
  risk: "low" | "medium" | "high";
  category: string;
  environment: Env;
  server_id: string | null;
  description: string;
  status: "proposed" | "active" | "declined" | "revoked";
  approvals: number;
  approvers: string[];
  evidence: { approval_id?: number; incident_id?: string; server_id?: string | null; by: string; at?: string; comment?: string; type?: string; id?: string }[];
  proposed_by: string;
  decided_by: string | null;
  decided_at: string | null;
  reason: string;
  uses: number;
  last_used_at: string | null;
  created_at: string;
}

export interface DreamChange {
  op: "add" | "update" | "retire" | "propose";
  type: "knowledge" | "approval";
  id: number;
  kind: string;
  title: string;
  status: string;
  before: { title: string; body: string; scope: KnowledgeScope; status: string } | null;
  after: { title: string; body: string; scope: KnowledgeScope; status: string } | null;
}

export interface Dream {
  id: number;
  run_id: number | null;
  started_at: string;
  finished_at: string | null;
  status: "running" | "done" | "error" | "undone";
  triggered_by: string;
  read: Record<string, number>;
  read_text: string;
  changes: DreamChange[];
  summary: string;
  tokens: number;
  cost_usd: number;
  model: string;
  undone_by: string | null;
  undone_at: string | null;
}

export interface KnowledgePage {
  items: Knowledge[];
  learned_approvals: LearnedApproval[];
  dreams: Dream[];
  policy: { learn_approvals: boolean; learn_after: number };
  dreaming: boolean;
  dream_routine: { id: number; enabled: boolean; cron: string; schedule: string; next_run_at: string | null; last_run_at: string | null } | null;
  categories: { id: string; name: string }[];
}

export interface ApprovalLearning {
  category: string;
  environment: Env;
  learn_after: number;
  actions: { action: string; title: string; high_risk: boolean; approved: number; approvers: string[]; learned: { id: number; status: LearnedApproval["status"] } | null }[];
}
