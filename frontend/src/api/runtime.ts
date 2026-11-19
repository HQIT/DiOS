import { apiPrefix } from "../lib/apiBase";
import { authHeaders } from "../lib/auth";

const BASE = `${apiPrefix()}/os/runtime`;
export class RuntimeApiError extends Error {
  code: string;
  status: number;
  constructor(code: string, status: number) { super(code); this.code = code; this.status = status; }
}
export interface Profile { id: string; name: string; description: string; implementation: string; synthetic: boolean; governance: string; policy_configured: boolean; }
export interface Sample { id: string; name: string; filename: string; source: string; sha256: string; }
export interface RunSummary {
  id: string; state: string; version: number; attempt_id: string | null;
  goal: string; profile_id: string; created_at: number; updated_at: number; cancel_requested: boolean;
  display: { name?: string; implementation?: string; synthetic?: boolean };
}
export interface Attempt {
  id: string; epoch: number; state: string; session_id: string | null; worker_id: string;
  process_id: number | null; started_at: number; finished_at: number | null; last_output_at: number | null;
  error: { code: string } | null; freshness: "fresh" | "stale" | "final";
  confirmed_running: boolean; output_excerpt: string;
}
export interface Artifact { id: string; attempt_id: string; kind: string; size: number; digest: string; created_at: number; }
export interface WorkAuthorization {
  mode: string; reference?: string; subject?: string; actor?: string; policy_id?: string;
  state?: string; expires_at?: number; authorization_epoch?: number;
  secret_refs?: { ref: string; version: string }[];
  grant?: { attempt_id: string; session_id: string | null; lease_epoch: number; version: number; state: string; expires_at: number } | null;
  decisions?: { id: string; action: string; created_at: number; outcome: string; reason_codes: string[] }[];
}
export interface RunDetail extends RunSummary {
  authorization: WorkAuthorization;
  observed_at: number; sample: Sample; attempts: Attempt[]; artifacts: Artifact[];
  attempts_has_more: boolean; artifacts_at_limit: boolean;
  events: { id: string; version: number; type: string; state: string; created_at: number }[];
}
export interface Preview { id: string; kind: string; text: string; markdown: string | null; size: number; digest: string; }

async function response(path: string, init?: RequestInit) {
  const result = await fetch(`${BASE}${path}`, { ...init, headers: authHeaders(init?.headers), cache: "no-store" });
  if (!result.ok) {
    const value = await result.json().catch(() => ({}));
    throw new RuntimeApiError(typeof value.detail === "string" ? value.detail : "REQUEST_FAILED", result.status);
  }
  return result;
}
async function request<T>(path: string, init?: RequestInit): Promise<T> { return (await response(path, init)).json(); }
const path = (id: string) => `/runs/${encodeURIComponent(id)}`;
export const runtimeApi = {
  profiles: (signal?: AbortSignal) => request<{ items: Profile[] }>("/profiles", { signal }),
  samples: (signal?: AbortSignal) => request<{ items: Sample[] }>("/samples", { signal }),
  list: (cursor?: string, signal?: AbortSignal) => request<{ items: RunSummary[]; next_cursor: string | null; observed_at: number }>(
    `/runs?limit=30${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { signal }),
  detail: (id: string, signal?: AbortSignal) => request<RunDetail>(path(id), { signal }),
  submit: (data: { profile_id: string; sample_id: string; goal: string; idempotency_key: string }) =>
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
