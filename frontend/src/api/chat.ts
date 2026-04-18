const BASE = "/api/apps/chat";

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface ChatSession {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface StoredMessage {
  id: string;
  role: string;
  content: string;
  created_at: string;
}

export interface ChatRequest {
  agent_id: string;
  session_id?: string;
  messages: ChatMessage[];
  stream?: boolean;
}

export type ChatStreamEvent =
  | { type: "content"; content: string }
  | { type: "reasoning"; content: string }
  | { type: "tool_call_start"; tool_name: string; tool_call_id?: string; arguments?: unknown }
  | { type: "tool_call_end"; tool_name: string; tool_call_id?: string; duration_ms?: number; result?: string }
  | { type: "tool_call_error"; tool_name: string; error: string }
  | {
      type: "hil_pending";
      token: string;
      method?: string;
      path?: string;
      message?: string;
      next_confirm_command?: string;
      created_at?: string;
      expires_at?: string;
      expires_in_seconds?: number;
    }
  | { type: "status"; message: string }
  | { type: "error"; error: string }
  | { type: "done" };

export async function* streamChat(
  req: ChatRequest,
  onSessionId?: (id: string) => void,
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const res = await fetch(`${BASE}/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...req, stream: true }),
    signal,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  const sid = res.headers.get("X-Session-Id");
  if (sid && onSessionId) onSessionId(sid);

  yield* _readSSE(res);
}

function _parseSseBlock(block: string): { event: string; data: string } | null {
  const lines = block.split("\n").map((x) => x.trimEnd());
  let event = "message";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (!line) continue;
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

async function* _readSSE(res: Response): AsyncGenerator<ChatStreamEvent> {
  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      if (buffer.trim()) {
        const evt = _parseSseBlock(buffer);
        if (evt?.data === "[DONE]") {
          yield { type: "done" };
        }
      }
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const evt = _parseSseBlock(block);
      if (!evt) continue;
      if (evt.data === "[DONE]") {
        yield { type: "done" };
        return;
      }
      if (evt.event === "tool_call") {
        try {
          const p = JSON.parse(evt.data);
          if (p?.type === "tool_call_start" && p?.tool_name) {
            yield {
              type: "tool_call_start",
              tool_name: String(p.tool_name),
              tool_call_id: p.tool_call_id ? String(p.tool_call_id) : undefined,
              arguments: p.arguments,
            };
          } else if (p?.type === "tool_call_end" && p?.tool_name) {
            yield {
              type: "tool_call_end",
              tool_name: String(p.tool_name),
              tool_call_id: p.tool_call_id ? String(p.tool_call_id) : undefined,
              duration_ms: typeof p.duration_ms === "number" ? p.duration_ms : undefined,
              result: typeof p.result === "string" ? p.result : undefined,
            };
          } else if (p?.type === "tool_call_error" && p?.tool_name) {
            yield {
              type: "tool_call_error",
              tool_name: String(p.tool_name),
              error: String(p?.error || "unknown error"),
            };
          }
        } catch {
          // skip invalid event payload
        }
        continue;
      }
      if (evt.event === "reasoning") {
        try {
          const p = JSON.parse(evt.data);
          if (p?.content) {
            yield { type: "reasoning", content: String(p.content) };
          }
        } catch {
          // skip invalid event payload
        }
        continue;
      }
      if (evt.event === "status") {
        try {
          const p = JSON.parse(evt.data);
          if (p?.message) {
            yield { type: "status", message: String(p.message) };
          }
        } catch {
          // skip invalid event payload
        }
        continue;
      }
      if (evt.event === "hil") {
        try {
          const p = JSON.parse(evt.data);
          if (p?.token) {
            yield {
              type: "hil_pending",
              token: String(p.token),
              method: p?.method ? String(p.method) : undefined,
              path: p?.path ? String(p.path) : undefined,
              message: p?.message ? String(p.message) : undefined,
              next_confirm_command: p?.next_confirm_command ? String(p.next_confirm_command) : undefined,
              created_at: p?.created_at ? String(p.created_at) : undefined,
              expires_at: p?.expires_at ? String(p.expires_at) : undefined,
              expires_in_seconds: typeof p?.expires_in_seconds === "number" ? p.expires_in_seconds : undefined,
            };
          }
        } catch {
          // skip invalid event payload
        }
        continue;
      }
      try {
        const parsed = JSON.parse(evt.data);
        if (parsed?.error) {
          yield { type: "error", error: String(parsed.error) };
          continue;
        }
        const delta = parsed?.choices?.[0]?.delta || {};
        const content = delta?.content;
        const reasoningContent = delta?.reasoning_content;
        if (typeof content === "string" && content) {
          yield { type: "content", content };
        }
        if (typeof reasoningContent === "string" && reasoningContent) {
          yield { type: "reasoning", content: reasoningContent };
        }
      } catch {
        // skip invalid payload
      }
    }
  }
}

export const chatApi = {
  listSessions: (agentId: string) =>
    fetch(`${BASE}/agents/${agentId}/sessions`).then((r) => r.json()) as Promise<ChatSession[]>,

  getMessages: (sessionId: string) =>
    fetch(`${BASE}/sessions/${sessionId}/messages`).then((r) => r.json()) as Promise<StoredMessage[]>,

  deleteSession: (sessionId: string) =>
    fetch(`${BASE}/sessions/${sessionId}`, { method: "DELETE" }),
};
