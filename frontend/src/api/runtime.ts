import { apiPrefix } from "../lib/apiBase";
import { authHeaders, sessionExpired } from "../lib/auth";

const BASE = `${apiPrefix()}/os/runtime`;
export class RuntimeApiError extends Error {
  code: string;
  status: number;
  constructor(code: string, status: number) { super(code); this.code = code; this.status = status; }
}
export interface PackageEntry {
  digest: string; name: string; version: string; kind: string; registered_at: number;
  dependencies: Array<{name: string; specifier: string}>;
  declaration: {source: {uri: string; revision: string}; license: string; capabilities: string[]} | null;
  admission: {state: string; version: number; evidence_valid: boolean | null; reason: string | null; reviewed_at: number | null};
  limitations: string[]; file_count: number; size: number;
  files?: Array<{path: string; digest: string; size: number; mediaType: string; mode: number}>;
  checks?: Record<string, {result: string; evidence: ObjectReference; digest: string}>;
}
export interface PackagePage {schema_version: number; items: PackageEntry[]; next_after: string | null; filters: Record<string, string | number>;}
export interface PackageReleases {items: Array<{id: string; definition_id: string; sequence: number; content_digest: string; created_at: number}>; next_after: string | null;}
export type PackageFilters = {q: string; kind: string; version: string; admission: string};
export const packageApi = {
  list: (filters: PackageFilters, after = "", signal?: AbortSignal) => request<PackagePage>(`/packages?${new URLSearchParams({...filters, after, limit: "20"})}`, {signal}),
  detail: (digest: string, signal?: AbortSignal) => request<PackageEntry>(`/packages/${encodeURIComponent(digest)}`, {signal}),
  releases: (digest: string, after = "", signal?: AbortSignal) => request<PackageReleases>(`/packages/${encodeURIComponent(digest)}/releases?${new URLSearchParams({after, limit: "20"})}`, {signal}),
};
export interface Profile { dynamic_inputs?: boolean; input_schema?: Record<string, unknown> | boolean | null; input_kind?: string; input_ids?: string[]; id: string; name: string; description: string; implementation: string; synthetic: boolean; governance: string; policy_configured: boolean; isolation?: {version: number; network: string; workspace: string} | null; }
export interface Sample { [key: string]: unknown; id: string; name: string; filename?: string; source?: string; sha256: string; }
export interface RunSummary {
  revision_ref?: string | null; deployment_id?: string | null;
  id: string; state: string; version: number; attempt_id: string | null;
  goal: string; profile_id: string; created_at: number; updated_at: number; cancel_requested: boolean;
  display: { name?: string; implementation?: string; synthetic?: boolean };
}
export interface ObjectReference { kind: "file" | "session" | "artifact"; id: string; version: number; }
export interface EvidenceRecord {
  id?: string; attempt_id?: string; worker_id?: string; worker_instance?: string; state?: string;
  tool_id?: string; approval_state?: string; reason?: string | null; result_evidence?: string;
  evidence_level: string; epoch?: number; sequence?: number; kind?: string;
  session?: {session_id: string} | null; object_ref?: ObjectReference; source?: ObjectReference;
  target?: ObjectReference | {namespace: string; key: string; version: string}; relation?: string;
  descriptor?: {digest: string; size: number} | null; values?: Record<string, unknown>;
}
export interface EvidencePage {
  root: {id: string; state: string; execution: {status: string; profile_id: string; profile_digest: string;
    revision_ref: string | null; digest?: string; dependencies: Record<string, Array<{name: string; version: string | null; digest: string | null; visibility: string}>>}};
  section: string; items: EvidenceRecord[]; next_cursor: string | null; access: string; observed_at: number;
}
export const evidenceApi = {
  page: (run: string, section: string, cursor: string, signal?: AbortSignal) => request<EvidencePage>(`/runs/${encodeURIComponent(run)}/evidence?${new URLSearchParams({section,limit:"20",...(cursor?{cursor}:{})})}`,{signal}),
  sources: (ref: ObjectReference, after: string, signal?: AbortSignal) => request<{items: Array<{id: string; relation: string; target: EvidenceRecord["target"]}>; next_after: string | null}>(`/objects/${ref.kind}/${encodeURIComponent(ref.id)}/versions/${ref.version}/relations?${new URLSearchParams({after,limit:"20"})}`,{signal}),
};
export interface Attempt {
  object_ref?: ObjectReference | null;
  id: string; epoch: number; state: string; session_id: string | null; worker_id: string;
  process_id: number | null; started_at: number; finished_at: number | null; last_output_at: number | null;
  error: { code: string } | null; freshness: "fresh" | "stale" | "final";
  confirmed_running: boolean; output_excerpt: string;
  runtime_state?: string | null;
  remote_agent?: { protocol: string; control_scope: string; stage: string; task_id: string | null;
    context_id: string | null; observed_state: string | null; observed_at: number | null;
    cancel_requested_at: number | null; freshness: "fresh" | "stale" | "final" };
}
export interface Artifact { id: string; attempt_id: string; kind: string; size: number; digest: string; created_at: number; object_ref?: ObjectReference; }
export interface WorkAuthorization {
  delegation?: { reference: string; captured_at: number; chain: { id: string; parent_ref: string | null; subject: string; actor: string; depth: number; state: string; expires_at: number; authorization_epoch: number; captured_epoch: number; source_check?: WorkAuthorization["source_check"]; source_event?: WorkAuthorization["source_event"]; revocation?: { id: string; operation: string; created_at: number; details: Record<string, unknown> } | null }[] } | null;
  source_check?: { outcome: "pending" | "valid" | "unavailable" | "revoked"; reason: string | null; checked_at: number | null; last_success_at: number | null; next_check_at: number; failures: number; checking: boolean; stale: boolean } | null;
  source_event?: { id: string; issued_at: number; received_at: number; revoked_work_count: number; first_received_at?: number; last_received_at?: number; event_count?: number } | null;
  mode: string; reference?: string; subject?: string; actor?: string; policy_id?: string;
  state?: string; expires_at?: number; authorization_epoch?: number;
  secret_refs?: { ref: string; version: string }[];
  grant?: { attempt_id: string; session_id: string | null; lease_epoch: number; version: number; state: string; expires_at: number } | null;
  decisions?: { id: string; action: string; created_at: number; outcome: string; reason_codes: string[] }[];
}
export interface RunDetail extends RunSummary {
  diagnostics?: RunDiagnostics;
  capacity?: {blocked: boolean; reason: string | null; available_at?: number; limits: {id: string; reason?: string; available_at?: number; max_concurrent: number; reserved_attempts: number; unresolved_attempts: number}[]} | null;
  model_usage?: {evidence_level: string; settlement: string; at_limit: boolean; items: Array<{attempt_id: string; session_id: string; sequence: number; kind: string; quality: string; values: Record<string, number | {amount: string; unit: string}>; recorded_at: number}>};
  tool_budget?: {maximum: number | null; committed: number; succeeded: number; failed: number; unresolved: number; remaining: number | null; exhausted: boolean; inherited?: boolean; accounting_scope?: 'run_tree'} | null;
  consumption_budget?: {provider_hard_cap: string; blocked_reasons: string[]; at_limit: boolean; items: Array<{account_id: string; attempt_id: string; unit: string; reserved: number; actual: number | null; state: string; account_revision: number; evidence: unknown}>};
  execution_budget?: {started_at: number; deadline_at: number; remaining_seconds: number; exhausted: boolean} | null;
  family?: { parent: { parent_run_id: string; parent_attempt_id: string; parent_session_id: string; parent_epoch: number; root_run_id: string; depth: number; root_limit: number; depth_limit: number } | null;
    children: { run_id: string; parent_attempt_id: string; state: string; cancel_requested: boolean }[]; parent_close_policy: "request_cancel" } | null;
  access?: string[];
  recovery?: { category: string; code: string; state: string; retry_at: number | null; reason: string | null; circuit: Circuit | null } | null;
  authorization: WorkAuthorization;
  observed_at: number; sample: Sample; attempts: Attempt[]; artifacts: Artifact[];
  attempts_has_more: boolean; artifacts_at_limit: boolean;
  events: { id: string; version: number; type: string; state: string; created_at: number }[];
}
export interface RuntimeOverview {
  observed_at: number; period: "hour" | "day"; timezone: string; profile_id: string | null;
  series: Array<{start: number; end: number; succeeded: number; failed: number; cancelled: number}>;
  history_totals: {succeeded: number; failed: number; cancelled: number};
  window_totals: {succeeded: number; failed: number; cancelled: number}; states: Record<string, number>;
  coverage: {complete: boolean; missing_terminal_outcomes: number};
  active: {at_limit: boolean; confirmed_sessions: number; actual_concurrency: number | null; items: Array<{id: string; profile_id: string; state: string; phase: string | null; diagnosis: string; last_output_at: number | null; retry_at: number | null}>};
}
export interface RunDiagnostics {
  time_basis: string; observed_at: number; last_event_at: number; last_output_at: number | null;
  freshness: string; attempts_has_more: boolean; governance_has_more: boolean;
  stages: Array<{attempt_id: string; epoch: number; phase: string; event_id: string | null; started_at: number; ended_at: number | null; duration_seconds: number | null; elapsed_seconds: number | null; clock_anomaly: boolean; end_reason: string}>;
  governance: Array<{id: string; kind: string; attempt_id: string | null; action: string; outcome: string; reason_codes: string[]; created_at: number; timing: ({clock: string} & Record<string, number | string>) | null}>;
}
export interface Preview { id: string; kind: string; text: string; markdown: string | null; size: number; digest: string; }
export interface RevisionCompatibility {
  revision_ref: string; state: string; configuration_rollback: string;
  implementation_visibility: string; implementation_rollback: string; implementation_digest: string | null;
  model_implementation_visibility: string; model_implementation_rollback: string;
  qualification: {state: string; error: string | null; observed_at: number; observed_stale: boolean;
    snapshot: {reported_version?: string; protocol_version?: string}; snapshot_digest: string} | null;
}
export const revisionApi = {
  compatibility: (ref: string, signal?: AbortSignal) => request<RevisionCompatibility>(`/revisions/${encodeURIComponent(ref)}/compatibility`, {signal}),
};
export interface ConfigProblem {code: string; path: string; keyword?: string; expected?: unknown; line?: number; column?: number;}
export interface ConfigReview {valid: boolean; errors: ConfigProblem[]; base_errors: ConfigProblem[]; base_digest: string | null; draft_digest: string | null; patch: unknown[]; changes: Array<{op: string; path: string; from?: string; before?: unknown; after?: unknown}>;}
export const studioApi = {
  source: (id: string) => request<{text: string; digest: string; revision_ref: string | null}>(`/studio/profiles/${encodeURIComponent(id)}`),
  compare: (base: string, draft: string) => request<ConfigReview>("/studio/compare", {method: "POST", body: JSON.stringify({base, draft})}),
  suggest: (profile_id: string, text: string, instruction: string, idempotency_key: string) => request<RunDetail>("/studio/suggestions", {method: "POST", body: JSON.stringify({profile_id, text, instruction, idempotency_key})}),
  proposal: (id: string, text: string) => request<{text: string | null; review: ConfigReview}>(`/studio/suggestions/${encodeURIComponent(id)}/review`, {method: "POST", body: JSON.stringify({text})}),
};
export type CapacitySelector = {kind: "scope"} | {kind: "subject"; subject: string} | {kind: "profiles"; profiles: string[]};
export interface CapacityLimit {
  id: string; selector: CapacitySelector; max_concurrent: number; enabled: boolean; revision: number;
  reserved_attempts: number; unresolved_attempts: number; queued_runs: number; available: number | null;
  budget?: ExecutionBudgetPolicy;
  dispatch_rate?: {used: number; maximum: number; window_seconds: number; available_at: number | null} | null;
}
export interface ExecutionBudgetPolicy {max_runtime_seconds: number | null; max_dispatches: number | null; dispatch_window_seconds: number | null; max_tool_calls: number | null;}
export const capacityApi = {
  status: (signal?: AbortSignal) => request<{items: CapacityLimit[]; observed_at: number}>("/capacity", {signal}),
  save: (row: {id: string; selector: CapacitySelector; max_concurrent: number; enabled: boolean; expected_revision: number; budget?: ExecutionBudgetPolicy}) =>
    request<{id: string; revision: number}>("/capacity", {method: "PUT", body: JSON.stringify(row)}),
};

async function response(path: string, init?: RequestInit) {
  const result = await fetch(`${BASE}${path}`, { ...init, headers: authHeaders(init?.headers), cache: "no-store" });
  if (!result.ok) {
    if (result.status === 401) sessionExpired();
    const value = await result.json().catch(() => ({}));
    throw new RuntimeApiError(typeof value.detail === "string" ? value.detail : "REQUEST_FAILED", result.status);
  }
  return result;
}
async function request<T>(path: string, init?: RequestInit): Promise<T> { return (await response(path, init)).json(); }
export interface ScheduledOccurrence {
  id: string; state: string; first_due: number; last_due: number; trigger_count: number | null;
  run_id: string | null; error: string; retry_at: number | null; failures: number;
}
export interface RunSchedule {
  id: string; version: number; enabled: boolean; expression: string; next_due: number;
  template: {profile_id: string; goal: string};
  binding: {subject: string; actor: string; policy_id: string; policy_digest: string};
  active: ScheduledOccurrence | null; buffered: ScheduledOccurrence | null;
}
const schedulePath = (id: string) => `/run-schedules/${encodeURIComponent(id)}`;
export const scheduleApi = {
  list: (before?: string, signal?: AbortSignal) => request<{items: RunSchedule[]; next_cursor: string | null; observed_at: number; scheduler_enabled: boolean}>(`/run-schedules?limit=20${before ? `&before=${encodeURIComponent(before)}` : ""}`, {signal}),
  create: (data: {profile_id: string; sample_id?: string; input_payload?: Record<string, unknown>; goal: string; cron_expression: string; idempotency_key: string}) => request<RunSchedule>("/run-schedules", {method: "POST", body: JSON.stringify(data)}),
  control: (row: RunSchedule, action: "pause" | "resume" | "reauthorize", reason: string) => request<RunSchedule>(`${schedulePath(row.id)}/${action === "reauthorize" ? "reauthorize" : "control"}`, {method: "POST", body: JSON.stringify({expected_version: row.version, actor: "console-operator", reason, ...(action === "reauthorize" ? {} : {enabled: action === "resume"})})}),
  history: (id: string, before?: string, signal?: AbortSignal) => request<{items: ScheduledOccurrence[]; next_cursor: string | null}>(`${schedulePath(id)}/occurrences?limit=20${before ? `&before=${encodeURIComponent(before)}` : ""}`, {signal}),
  resolve: (row: RunSchedule, item: ScheduledOccurrence, action: "retry" | "discard", reason: string, key: string) => request<RunSchedule>(`${schedulePath(row.id)}/occurrences/${encodeURIComponent(item.id)}/resolve`, {method: "POST", body: JSON.stringify({action, reason, actor: "console-operator", idempotency_key: key})}),
};
const path = (id: string) => `/runs/${encodeURIComponent(id)}`;
export interface WorkerStatus {
  circuits?: Circuit[];
  observed_at: number; actual_concurrency: number | null; unknown_attempts: number;
  confirmed_running_processes: number; confirmed_running_sessions: number;
  confirmed_paused_processes?: number;
  workers: { id: string; host: string; pid: number; draining: boolean; heartbeat_at: number }[];
  attempts: { attempt_id: string; worker_id: string; run_id: string; phase: string; freshness: string; runtime?: {state?: string} }[];
}

export interface SchedulerHealth {
  observed_at: number; freshness_seconds: number; slow_seconds: number;
  services: { service: string; state: string; fresh_instances: number; at_limit: boolean;
    evidence: { heartbeat_at: number; completed_at: number|null; succeeded_at: number|null; consecutive_errors: number; duration_seconds: number|null; error: string|null }|null;
    instances: unknown[];
  }[];
}
export interface Circuit { resource: string; state: string; retry_at: number; reason: string | null; probe_attempt: string | null; }
export interface DeploymentView {
  deployment: {id: string; version: number; revision_id: string; state: string; resource_scope_ref: string};
  status: {definition_id: string; active_revision_id: string | null; previous_revision_id: string | null; rollout_id: string; error: string | null; updated_at: number};
  probe?: {id: string; state: string; attempt_id: string | null; updated_at: number};
  rollout?: {state: string; operation: string; deadline: number};
  events?: Array<{version: number; type: string; actor: string; created_at: number}>;
}
export const deploymentApi = {
  list: (after: string, signal?: AbortSignal) => request<{items: DeploymentView[]}>(`/deployments?limit=50&after=${encodeURIComponent(after)}`, {signal}),
  detail: (id: string, signal?: AbortSignal) => request<DeploymentView>(`/deployments/${encodeURIComponent(id)}`, {signal}),
  reconcile: (id: string) => request<DeploymentView>(`/deployments/${encodeURIComponent(id)}/reconcile`, {method: "POST"}),
};
export const runtimeApi = {
  identity: (signal?: AbortSignal) => request<{ mode: "operator" | "principal"; principal: { id: string; subject: string } | null }>("/identity", { signal }),
  workers: (signal?: AbortSignal) => request<WorkerStatus>("/workers", { signal }),
  schedulerHealth: (signal?: AbortSignal) => request<SchedulerHealth>("/scheduler-health", { signal }),
  profiles: (signal?: AbortSignal) => request<{ items: Profile[] }>("/profiles", { signal }),
  samples: (signal?: AbortSignal) => request<{ items: Sample[] }>("/samples", { signal }),
  list: (cursor?: string, signal?: AbortSignal, profileId?: string) => request<{ items: RunSummary[]; next_cursor: string | null; observed_at: number }>(
    `/runs?limit=30${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}${profileId ? `&profile_id=${encodeURIComponent(profileId)}` : ""}`, { signal }),
  overview: (period: "hour" | "day", profileId?: string, signal?: AbortSignal) => request<RuntimeOverview>(
    `/overview?period=${period}&buckets=${period === "hour" ? 24 : 30}${profileId ? `&profile_id=${encodeURIComponent(profileId)}` : ""}`, { signal }),
  detail: (id: string, signal?: AbortSignal) => request<RunDetail>(path(id), { signal }),
  submit: (data: { profile_id: string; sample_id?: string; input_payload?: Record<string, unknown>; goal: string; idempotency_key: string; delegation_ref?: string; parent_run_id?: string }) =>
    request<RunDetail>("/runs", { method: "POST", body: JSON.stringify(data) }),
  cancel: (id: string, version: number) => request<RunDetail>(`${path(id)}/cancel`, {
    method: "POST", body: JSON.stringify({ expected_version: version }),
  }),
  revoke: (id: string, version: number) => request<RunDetail>(`${path(id)}/revoke`, {
    method: "POST", body: JSON.stringify({ expected_version: version }),
  }),
  preview: (id: string, artifact: string, signal?: AbortSignal) =>
    request<Preview>(`${path(id)}/artifacts/${encodeURIComponent(artifact)}/preview`, { signal }),
  download: async (id: string, artifact: string, markdown = false) => {
    const result = await response(`${path(id)}/artifacts/${encodeURIComponent(artifact)}/download?format=${markdown ? "markdown" : "raw"}`);
    const url = URL.createObjectURL(await result.blob());
    const a = document.createElement("a"); a.href = url; a.download = `${markdown ? "report" : "artifact"}-${artifact}.${markdown ? "md" : "bin"}`;
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  diagnosticExport: async (id: string) => {
    const result=await response(`${path(id)}/diagnostic-export`,{signal:AbortSignal.timeout(30000)});
    const blob=await result.blob();
    const url=URL.createObjectURL(blob);const link=document.createElement("a");
    link.href=url;link.download=`dios-diagnostic-${id}.zip`;document.body.appendChild(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  },
};


export interface ToolInvocation {
  id: string; run_id: string; attempt_id: string; session_id: string; tool_id: string;
  permission: { action: string; resource_kind: string; resource_id: string; resource_version: string; constraint_revision: string };
  arguments: Record<string, unknown>; arguments_digest: string; state: string; version: number;
  approval_state: string; approver: string | null; approval_reason: string | null;
  expires_at: number; updated_at: number; expired: boolean; dispatch_overdue: boolean;
  reason: string | null; result_key: string | null; receipt_available?: boolean;
}
const toolPath = (run: string, id: string) => `${path(run)}/tools/${encodeURIComponent(id)}`;
export const toolApi = {
  list: (run: string, signal?: AbortSignal) => request<{ items: ToolInvocation[]; at_limit: boolean; migration_required?: boolean }>(`${path(run)}/tools`, { signal }),
  approve: (run: string, item: ToolInvocation, decision: "approved" | "denied", token: string, reason: string) =>
    request<ToolInvocation>(`${toolPath(run, item.id)}/approval`, { method: "POST", headers: { "X-DiOS-Approver-Token": token },
      body: JSON.stringify({ expected_version: item.version, decision, reason }) }),
  dispatch: (run: string, item: ToolInvocation) => request<ToolInvocation>(`${toolPath(run, item.id)}/dispatch`, {
    method: "POST", body: JSON.stringify({ expected_version: item.version }) }),
  recover: (run: string, item: ToolInvocation) => request<ToolInvocation>(`${toolPath(run, item.id)}/receipt-recover`, {
    method: "POST", body: JSON.stringify({ expected_version: item.version }) }),
  reconcile: (run: string, item: ToolInvocation) => request<ToolInvocation>(`${toolPath(run, item.id)}/reconcile`, {
    method: "POST", body: JSON.stringify({ expected_version: item.version }) }),
};
