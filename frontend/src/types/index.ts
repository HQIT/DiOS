export interface LLMModel {
  id: string;
  name: string;
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
  display_name: string;
  description: string;
  context_length?: number;
  created_at: string;
}

export interface Agent {
  id: string;
  name: string;
  mode: string;  // "service" | "task"
  group: string;
  role: string;
  description: string;
  model: string;
  system_prompt: string;
  skills: string[];
  mcp_config_path: string;
  mcp_server_ids?: string[];
  workspace_path: string;
  capabilities?: Record<string, unknown>;
  env?: Record<string, string>;
  created_at: string;
}

export interface Connector {
  id: string;
  type: string;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
  created_at: string;
}

export interface ConnectorSourcePattern {
  source_pattern: string;
  label: string;
  event_types: string[];
  connector_id: string | null;
  connector_name: string;
  connector_type: string;
  kind: "connector" | "internal";
}

export interface McpServer {
  id: string;
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  created_at: string;
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  source_url: string;
  content: string;
  created_at: string;
}

export interface EventCatalogSource {
  id: string;
  name: string;
  description: string;
}

export interface EventCatalogType {
  type: string;
  category: string;
  description: string;
}

export interface EventCatalog {
  sources: EventCatalogSource[];
  event_types: EventCatalogType[];
  connector_status?: Record<string, boolean>;
}

export interface Subscription {
  id: string;
  agent_id: string;
  source_pattern: string;
  event_types: string[];
  filter_rules: Record<string, string>;
  cron_expression: string;
  enabled: boolean;
  created_at: string;
}

export interface EventLog {
  id: string;
  source: string;
  event_type: string;
  subject: string;
  cloud_event: Record<string, unknown>;
  matched_agent_ids: string[];
  status: string;
  created_at: string;
  retry_count?: number;
  max_retries?: number;
  next_retry_at?: string;
  error_message?: string;
  dedup_hash?: string;
}

export interface EventActivityItem {
  task_id: string;
  agent_id: string;
  agent_name: string;
  status: string;
  started_at: string;
  ended_at?: string;
  duration_ms: number;
  error?: string;
  artifacts_count?: number;
}

export interface EventActivityOverview {
  event_id: string;
  event_type: string;
  source: string;
  status: string;
  timeline_start: string;
  timeline_end: string;
  items: EventActivityItem[];
}

export interface ActivityBehaviorPoint {
  type: string;
  at: string;
  label: string;
}

export interface ActivityGanttBar {
  task_id: string;
  agent_id: string;
  agent_name: string;
  status: string;
  start_at: string;
  end_at?: string | null;
  effective_end_at: string;
  duration_ms: number;
  event_id?: string;
  event_type?: string;
  source?: string;
  error?: string;
  artifacts_count?: number;
  behaviors: ActivityBehaviorPoint[];
}

export interface ActivityGanttResponse {
  timeline_start: string;
  timeline_end: string;
  since_minutes: number;
  date?: string;
  agents: { agent_id: string; agent_name: string; task_count: number }[];
  bars: ActivityGanttBar[];
}
