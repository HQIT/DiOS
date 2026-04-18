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

export async function* streamChat(
  req: ChatRequest,
  onSessionId?: (id: string) => void,
  signal?: AbortSignal,
): AsyncGenerator<string> {
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

async function* _readSSE(res: Response): AsyncGenerator<string> {
  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const data = line.slice(6);
        if (data === "[DONE]") return;
        try {
          const parsed = JSON.parse(data);
          const content = parsed.choices?.[0]?.delta?.content;
          if (content) yield content;
        } catch {
          // skip
        }
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
