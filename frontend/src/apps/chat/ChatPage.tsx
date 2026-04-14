import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "../../api/os";
import { streamChat, chatApi, type ChatMessage, type ChatSession, type StoredMessage } from "../../api/chat";
import type { Agent } from "../../types";

interface UIMessage {
  role: "user" | "assistant";
  content: string;
}

export default function ChatPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);

  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    api.listAgents({ mode: "service" }).then((list) => {
      setAgents(list);
      if (list.length > 0 && !selectedAgent) setSelectedAgent(list[0]);
    });
  }, []);

  // 切换 agent 时加载会话列表
  useEffect(() => {
    if (!selectedAgent) return;
    chatApi.listSessions(selectedAgent.id).then(setSessions);
    setSessionId(null);
    setMessages([]);
  }, [selectedAgent?.id]);

  // 切换 session 时加载历史消息
  useEffect(() => {
    if (!sessionId) return;
    chatApi.getMessages(sessionId).then((msgs) => {
      setMessages(msgs.map((m) => ({ role: m.role as "user" | "assistant", content: m.content })));
    });
  }, [sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const refreshSessions = useCallback(() => {
    if (selectedAgent) chatApi.listSessions(selectedAgent.id).then(setSessions);
  }, [selectedAgent]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || !selectedAgent || loading) return;

    const userMsg: UIMessage = { role: "user", content: text };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    const assistantMsg: UIMessage = { role: "assistant", content: "" };
    setMessages((prev) => [...prev, assistantMsg]);

    try {
      const stream = streamChat(
        {
          agent_id: selectedAgent.id,
          session_id: sessionId || undefined,
          messages: [{ role: "user" as const, content: text }],
        },
        (sid) => {
          if (!sessionId) {
            setSessionId(sid);
            refreshSessions();
          }
        },
      );

      for await (const chunk of stream) {
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          updated[updated.length - 1] = { ...last, content: last.content + chunk };
          return updated;
        });
      }
      refreshSessions();
    } catch (err) {
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          role: "assistant",
          content: `Error: ${err instanceof Error ? err.message : String(err)}`,
        };
        return updated;
      });
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }, [input, selectedAgent, loading, sessionId, refreshSessions]);

  const newChat = () => {
    setSessionId(null);
    setMessages([]);
  };

  const deleteSession = async (sid: string) => {
    await chatApi.deleteSession(sid);
    if (sessionId === sid) newChat();
    refreshSessions();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div style={{ display: "flex", height: "100%" }}>
      {/* Sidebar: Agents + Sessions */}
      <div className="sidebar" style={{ width: 260, minWidth: 220, padding: "16px 12px", display: "flex", flexDirection: "column" }}>
        <h3 style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12, textTransform: "uppercase", letterSpacing: 1 }}>
          Agents
        </h3>
        {agents.map((a) => (
          <button
            key={a.id}
            onClick={() => setSelectedAgent(a)}
            style={{
              display: "block", width: "100%", textAlign: "left",
              padding: "10px 12px", marginBottom: 4, borderRadius: "var(--radius)",
              background: selectedAgent?.id === a.id ? "var(--bg-hover)" : "transparent",
              color: selectedAgent?.id === a.id ? "var(--text)" : "var(--text-secondary)",
              border: "none", cursor: "pointer", fontSize: 14, transition: "background 0.15s",
            }}
          >
            <div style={{ fontWeight: 500 }}>{a.name}</div>
            {a.description && (
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {a.description}
              </div>
            )}
          </button>
        ))}

        {/* Sessions */}
        {selectedAgent && (
          <>
            <div style={{ display: "flex", alignItems: "center", marginTop: 20, marginBottom: 8 }}>
              <h3 style={{ fontSize: 13, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 1, flex: 1, margin: 0 }}>
                Sessions
              </h3>
              <button onClick={newChat} style={{ background: "none", border: "none", color: "var(--color-primary)", cursor: "pointer", fontSize: 18, lineHeight: 1, padding: "0 4px" }} title="New chat">
                +
              </button>
            </div>
            <div style={{ flex: 1, overflowY: "auto" }}>
              {sessions.map((s) => (
                <div
                  key={s.id}
                  style={{
                    display: "flex", alignItems: "center",
                    padding: "8px 12px", marginBottom: 2, borderRadius: "var(--radius)",
                    background: sessionId === s.id ? "var(--bg-hover)" : "transparent",
                    cursor: "pointer", fontSize: 13, transition: "background 0.15s",
                  }}
                  onClick={() => setSessionId(s.id)}
                >
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text)" }}>
                    {s.title || "Untitled"}
                  </span>
                  <button
                    onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }}
                    style={{ background: "none", border: "none", color: "var(--text-secondary)", cursor: "pointer", fontSize: 12, padding: "0 4px", opacity: 0.5 }}
                    title="Delete"
                  >
                    ×
                  </button>
                </div>
              ))}
              {sessions.length === 0 && (
                <div style={{ color: "var(--text-secondary)", fontSize: 12, padding: "8px 12px" }}>No sessions yet</div>
              )}
            </div>
          </>
        )}
      </div>

      {/* Chat area */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div style={{ flex: 1, overflowY: "auto", padding: "24px 24px 8px" }}>
          {messages.length === 0 && selectedAgent && (
            <div style={{ textAlign: "center", color: "var(--text-secondary)", marginTop: 80 }}>
              <div style={{ fontSize: 18, marginBottom: 8 }}>Chat with {selectedAgent.name}</div>
              <div style={{ fontSize: 13 }}>{selectedAgent.description || "Send a message to start"}</div>
            </div>
          )}
          {messages.map((msg, i) => (
            <div key={i} style={{ display: "flex", justifyContent: msg.role === "user" ? "flex-end" : "flex-start", marginBottom: 16 }}>
              <div style={{
                maxWidth: "70%", padding: "12px 16px", borderRadius: 12, fontSize: 14,
                lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word",
                background: msg.role === "user" ? "var(--color-primary)" : "var(--bg-surface)",
                color: msg.role === "user" ? "#fff" : "var(--text)",
              }}>
                {msg.content || (loading && i === messages.length - 1 ? "..." : "")}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        <div style={{ padding: "12px 24px 20px", borderTop: "1px solid var(--border)" }}>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={selectedAgent ? `Message ${selectedAgent.name}...` : "Select an agent"}
              disabled={!selectedAgent || loading}
              rows={1}
              style={{
                flex: 1, resize: "none", padding: "10px 14px", borderRadius: "var(--radius)",
                border: "1px solid var(--border)", background: "var(--bg-surface)", color: "var(--text)",
                fontSize: 14, lineHeight: 1.5, outline: "none", minHeight: 42, maxHeight: 160, overflow: "auto",
              }}
            />
            <button
              onClick={send}
              disabled={!input.trim() || !selectedAgent || loading}
              style={{
                padding: "10px 20px", borderRadius: "var(--radius)",
                background: loading ? "var(--border)" : "var(--color-primary)",
                color: "#fff", border: "none", cursor: loading ? "not-allowed" : "pointer",
                fontSize: 14, fontWeight: 500, height: 42, transition: "background 0.15s",
              }}
            >
              {loading ? "..." : "Send"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
