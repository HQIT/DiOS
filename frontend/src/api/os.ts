const BASE = "/api/os";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

export const api = {
  // Models
  listModels: () => request<import("../types").LLMModel[]>("/models"),
  createModel: (data: Record<string, unknown>) =>
    request<import("../types").LLMModel>("/models", { method: "POST", body: JSON.stringify(data) }),
  updateModel: (id: string, data: Record<string, unknown>) =>
    request<import("../types").LLMModel>(`/models/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteModel: (id: string) => request<void>(`/models/${id}`, { method: "DELETE" }),

  // Agents
  listAgents: (params?: { group?: string; mode?: string }) => {
    const q = new URLSearchParams();
    if (params?.group) q.set("group", params.group);
    if (params?.mode) q.set("mode", params.mode);
    const qs = q.toString();
    return request<import("../types").Agent[]>(`/agents${qs ? `?${qs}` : ""}`);
  },
  createAgent: (data: Record<string, unknown>) =>
    request<import("../types").Agent>("/agents", { method: "POST", body: JSON.stringify(data) }),
  updateAgent: (agentId: string, data: Record<string, unknown>) =>
    request<import("../types").Agent>(`/agents/${agentId}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteAgent: (agentId: string) =>
    request<void>(`/agents/${agentId}`, { method: "DELETE" }),

  // Subscriptions
  listAllSubscriptions: () =>
    request<import("../types").Subscription[]>("/subscriptions"),
  listSubscriptions: (agentId: string) =>
    request<import("../types").Subscription[]>(`/agents/${agentId}/subscriptions`),
  createSubscription: (agentId: string, data: Record<string, unknown>) =>
    request<import("../types").Subscription>(`/agents/${agentId}/subscriptions`, { method: "POST", body: JSON.stringify(data) }),
  updateSubscription: (agentId: string, subId: string, data: Record<string, unknown>) =>
    request<import("../types").Subscription>(`/agents/${agentId}/subscriptions/${subId}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteSubscription: (agentId: string, subId: string) =>
    request<void>(`/agents/${agentId}/subscriptions/${subId}`, { method: "DELETE" }),

  // Events
  listEvents: (params?: { source?: string; event_type?: string; status?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.source) q.set("source", params.source);
    if (params?.event_type) q.set("event_type", params.event_type);
    if (params?.status) q.set("status", params.status);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset !== undefined) q.set("offset", String(params.offset));
    const qs = q.toString();
    return request<{ items: import("../types").EventLog[]; total: number; limit: number; offset: number }>(`/events${qs ? `?${qs}` : ""}`);
  },
  getEvent: (eventId: string) => request<import("../types").EventLog>(`/events/${eventId}`),
  getEventActivityOverview: (eventId: string) =>
    request<import("../types").EventActivityOverview>(`/events/${eventId}/activity-overview`),
  getActivityGantt: (params?: { since_minutes?: number; date?: string; agent_ids?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.since_minutes) q.set("since_minutes", String(params.since_minutes));
    if (params?.date) q.set("date", params.date);
    if (params?.agent_ids) q.set("agent_ids", params.agent_ids);
    if (params?.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<import("../types").ActivityGanttResponse>(`/events/activity-gantt${qs ? `?${qs}` : ""}`);
  },
  retryEvent: (eventId: string) => 
    request<{ message: string; event_id: string }>(`/events/${eventId}/retry`, { method: "POST" }),

  // Event Catalog & Manual Trigger
  getEventCatalog: () => request<import("../types").EventCatalog>("/events/catalog"),
  triggerManualEvent: (data: { event_type: string; source?: string; subject?: string; data?: Record<string, unknown> }) =>
    request<Record<string, unknown>>("/events/manual", { method: "POST", body: JSON.stringify(data) }),

  // Connectors
  listConnectors: () => request<import("../types").Connector[]>("/connectors"),
  listConnectorSourcePatterns: () =>
    request<import("../types").ConnectorSourcePattern[]>("/connectors/source-patterns"),
  createConnector: (data: Record<string, unknown>) =>
    request<import("../types").Connector>("/connectors", { method: "POST", body: JSON.stringify(data) }),
  updateConnector: (id: string, data: Record<string, unknown>) =>
    request<import("../types").Connector>(`/connectors/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteConnector: (id: string) => request<void>(`/connectors/${id}`, { method: "DELETE" }),

  // Skills
  listSkills: () => request<import("../types").Skill[]>("/skills"),
  createSkill: (data: Record<string, unknown>) =>
    request<import("../types").Skill>("/skills", { method: "POST", body: JSON.stringify(data) }),
  updateSkill: (id: string, data: Record<string, unknown>) =>
    request<import("../types").Skill>(`/skills/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteSkill: (id: string) => request<void>(`/skills/${id}`, { method: "DELETE" }),
  importSkillFromGit: (url: string) =>
    request<import("../types").Skill>("/skills/import-git", { method: "POST", body: JSON.stringify({ url }) }),
  searchSkillRegistry: (q: string) =>
    request<{ repos: { name: string; url: string; description: string }[]; total: number }>(`/skills/registry?q=${encodeURIComponent(q)}`),

  // MCP Registry search
  searchMcpRegistry: (q: string, limit = 20) =>
    request<{ servers: { name: string; description: string; version: string; command: string; args: string[]; env_hints: Record<string, string>; transport: string }[]; total: number }>(`/mcp-registry/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  // MCP Servers
  listMcpServers: () => request<import("../types").McpServer[]>("/mcp-servers"),
  createMcpServer: (data: Record<string, unknown>) =>
    request<import("../types").McpServer>("/mcp-servers", { method: "POST", body: JSON.stringify(data) }),
  updateMcpServer: (id: string, data: Record<string, unknown>) =>
    request<import("../types").McpServer>(`/mcp-servers/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteMcpServer: (id: string) => request<void>(`/mcp-servers/${id}`, { method: "DELETE" }),
};
