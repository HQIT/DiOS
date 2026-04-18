import { useEffect, useRef, useState, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { api } from "../../api/os";
import { streamChat, chatApi, type ChatSession, type ChatStreamEvent } from "../../api/chat";
import type { Agent } from "../../types";

interface UIMessage {
  role: "user" | "assistant";
  content: string;
}

interface HilPending {
  token: string;
  message?: string;
  method?: string;
  path?: string;
  nextConfirmCommand?: string;
  createdAt?: string;
  expiresAt?: string;
  expiresInSeconds?: number;
}

const LAST_AGENT_KEY = "dios:chat:lastAgentId";

function compactJson(value: unknown, max = 140): string {
  try {
    const text = JSON.stringify(value);
    return text.length > max ? text.slice(0, max) + "..." : text;
  } catch {
    return String(value);
  }
}

function formatSeconds(seconds: number): string {
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

export default function ChatPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);

  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [liveEvents, setLiveEvents] = useState<string[]>([]);
  const [hilPending, setHilPending] = useState<HilPending | null>(null);
  const [hilSubmitting, setHilSubmitting] = useState(false);
  const [hilNotice, setHilNotice] = useState<string>("");
  const [nowMs, setNowMs] = useState<number>(Date.now());
  const [pendingDeleteSessionId, setPendingDeleteSessionId] = useState<string | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const skipNextSessionLoadRef = useRef(false);

  useEffect(() => {
    api.listAgents({ mode: "service" }).then((list) => {
      setAgents(list);
      if (list.length === 0) return;
      const lastId = localStorage.getItem(LAST_AGENT_KEY);
      const restored = lastId ? list.find((a) => a.id === lastId) : null;
      if (!selectedAgent) setSelectedAgent(restored || list[0]);
    });
  }, []);

  const pickAgent = useCallback((a: Agent) => {
    setSelectedAgent(a);
    try { localStorage.setItem(LAST_AGENT_KEY, a.id); } catch {}
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
    // 新会话首轮流式时，本地已有 user+assistant 占位，跳过一次覆盖
    if (skipNextSessionLoadRef.current) {
      skipNextSessionLoadRef.current = false;
      return;
    }
    chatApi.getMessages(sessionId).then((msgs) => {
      setMessages(msgs.map((m) => ({ role: m.role as "user" | "assistant", content: m.content })));
    });
  }, [sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!hilPending) return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hilPending]);

  const refreshSessions = useCallback(() => {
    if (selectedAgent) chatApi.listSessions(selectedAgent.id).then(setSessions);
  }, [selectedAgent]);

  const send = useCallback(async (overrideText?: string) => {
    const text = (overrideText ?? input).trim();
    if (!text || !selectedAgent || loading) return;

    const userMsg: UIMessage = { role: "user", content: text };
    setMessages((prev) => [...prev, userMsg]);
    setLiveEvents([]);
    setHilPending(null);
    setHilSubmitting(false);
    setHilNotice("");
    if (overrideText === undefined) setInput("");
    setLoading(true);

    const assistantMsg: UIMessage = { role: "assistant", content: "" };
    setMessages((prev) => [...prev, assistantMsg]);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      const stream = streamChat(
        {
          agent_id: selectedAgent.id,
          session_id: sessionId || undefined,
          messages: [{ role: "user" as const, content: text }],
        },
        (sid) => {
          if (!sessionId) {
            skipNextSessionLoadRef.current = true;
            setSessionId(sid);
            refreshSessions();
          }
        },
        ctrl.signal,
      );

      const pushLiveEvent = (line: string) => {
        setLiveEvents((prev) => {
          const next = [...prev, line];
          return next.slice(-14);
        });
      };
      const toolInvokeCount = new Map<string, number>();

      for await (const evt of stream) {
        const e = evt as ChatStreamEvent;
        if (e.type === "content") {
          setMessages((prev) => {
            const updated = [...prev];
            const last = updated[updated.length - 1];
            updated[updated.length - 1] = { ...last, content: last.content + e.content };
            return updated;
          });
          continue;
        }
        if (e.type === "reasoning") {
          pushLiveEvent(`思考中: ${e.content.slice(0, 120)}`);
          continue;
        }
        if (e.type === "tool_call_start") {
          const argText = compactJson(e.arguments ?? {});
          const key = `${e.tool_name}|${argText}`;
          const count = (toolInvokeCount.get(key) || 0) + 1;
          toolInvokeCount.set(key, count);
          if (count > 1) {
            pushLiveEvent(`重复调用(${count}) ${e.tool_name} args=${argText}`);
          } else {
            pushLiveEvent(`调用工具 ${e.tool_name} args=${argText}`);
          }
          continue;
        }
        if (e.type === "tool_call_end") {
          const cost = typeof e.duration_ms === "number" ? ` (${Math.round(e.duration_ms)}ms)` : "";
          const result = e.result ? ` result=${e.result.slice(0, 80)}` : "";
          pushLiveEvent(`工具完成 ${e.tool_name}${cost}${result}`);
          continue;
        }
        if (e.type === "tool_call_error") {
          pushLiveEvent(`工具失败: ${e.tool_name} (${e.error})`);
          continue;
        }
        if (e.type === "status") {
          pushLiveEvent(e.message);
          continue;
        }
        if (e.type === "hil_pending") {
          const pending: HilPending = {
            token: e.token,
            method: e.method,
            path: e.path,
            message: e.message,
            nextConfirmCommand: e.next_confirm_command,
            createdAt: e.created_at,
            expiresAt: e.expires_at,
            expiresInSeconds: e.expires_in_seconds,
          };
          setHilPending(pending);
          setHilSubmitting(false);
          setHilNotice("");
          pushLiveEvent(`HIL 待确认: ${pending.method || ""} ${pending.path || ""}`.trim());
          continue;
        }
        if (e.type === "error") {
          pushLiveEvent(`流错误: ${e.error}`);
          continue;
        }
      }
      refreshSessions();
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError";
      setMessages((prev) => {
        const updated = [...prev];
        const last = updated[updated.length - 1];
        updated[updated.length - 1] = {
          role: "assistant",
          content: last.content
            ? last.content + (aborted ? "\n\n[已中断]" : `\n\n[错误：${err instanceof Error ? err.message : String(err)}]`)
            : (aborted ? "[已中断]" : `错误：${err instanceof Error ? err.message : String(err)}`),
        };
        return updated;
      });
    } finally {
      abortRef.current = null;
      setLoading(false);
      setLiveEvents([]);
      inputRef.current?.focus();
    }
  }, [input, selectedAgent, loading, sessionId, refreshSessions]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const newChat = () => {
    setSessionId(null);
    setMessages([]);
    setPendingDeleteSessionId(null);
  };

  const deleteSession = async (sid: string) => {
    await chatApi.deleteSession(sid);
    if (sessionId === sid) newChat();
    setPendingDeleteSessionId(null);
    refreshSessions();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const expiresAtMs = hilPending?.expiresAt ? Date.parse(hilPending.expiresAt) : NaN;
  const hilExpired = Boolean(hilPending) && Number.isFinite(expiresAtMs) && nowMs >= expiresAtMs;
  const hilRemainSeconds = Boolean(hilPending) && Number.isFinite(expiresAtMs)
    ? Math.max(0, Math.floor((expiresAtMs - nowMs) / 1000))
    : null;

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
            onClick={() => pickAgent(a)}
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
                  {pendingDeleteSessionId === s.id ? (
                    <div style={{ display: "flex", gap: 6 }}>
                      <button
                        onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }}
                        style={{
                          height: 28,
                          padding: "0 10px",
                          borderRadius: 6,
                          border: "1px solid #ef4444",
                          background: "#ef4444",
                          color: "#fff",
                          cursor: "pointer",
                          fontSize: 12,
                          fontWeight: 600,
                        }}
                        title="确认删除该会话"
                      >
                        确认删除
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setPendingDeleteSessionId(null); }}
                        style={{
                          height: 28,
                          padding: "0 10px",
                          borderRadius: 6,
                          border: "1px solid var(--border)",
                          background: "var(--bg-surface)",
                          color: "var(--text-secondary)",
                          cursor: "pointer",
                          fontSize: 12,
                        }}
                        title="取消删除"
                      >
                        取消
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={(e) => { e.stopPropagation(); setPendingDeleteSessionId(s.id); }}
                      style={{
                        height: 28,
                        minWidth: 52,
                        padding: "0 10px",
                        borderRadius: 6,
                        border: "1px solid var(--border)",
                        background: "var(--bg-surface)",
                        color: "var(--text-secondary)",
                        cursor: "pointer",
                        fontSize: 12,
                        fontWeight: 500,
                      }}
                      title="删除会话"
                    >
                      删除
                    </button>
                  )}
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
                lineHeight: 1.6, wordBreak: "break-word",
                background: msg.role === "user" ? "var(--color-primary)" : "var(--bg-surface)",
                color: msg.role === "user" ? "#fff" : "var(--text)",
              }}>
                {msg.content ? (
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    rehypePlugins={[rehypeHighlight]}
                    components={{
                      pre: ({ children }) => (
                        <div style={{ position: "relative", margin: "8px 0" }}>
                          <pre style={{ overflowX: "auto", padding: "10px", borderRadius: 8, background: "rgba(0,0,0,0.08)" }}>{children}</pre>
                        </div>
                      ),
                      code: ({ inline, className, children, ...props }: any) => {
                        const text = String(children ?? "");
                        if (inline) {
                          return <code style={{ background: "rgba(0,0,0,0.08)", padding: "1px 4px", borderRadius: 4 }} {...props}>{children}</code>;
                        }
                        return (
                          <div style={{ position: "relative" }}>
                            <button
                              type="button"
                              style={{
                                position: "absolute",
                                right: 8,
                                top: 8,
                                fontSize: 11,
                                border: "1px solid var(--border)",
                                borderRadius: 6,
                                background: "var(--bg-surface)",
                                color: "var(--text-secondary)",
                                cursor: "pointer",
                                padding: "2px 8px",
                              }}
                              onClick={() => { void navigator.clipboard?.writeText(text); }}
                              title="复制代码"
                            >
                              复制
                            </button>
                            <code className={className} {...props}>{children}</code>
                          </div>
                        );
                      },
                    }}
                  >
                    {msg.content}
                  </ReactMarkdown>
                ) : (loading && i === messages.length - 1 ? "..." : "")}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        <div style={{ padding: "12px 24px 20px", borderTop: "1px solid var(--border)" }}>
          {hilPending && (
            <div
              style={{
                marginBottom: 10,
                background: "var(--bg-surface)",
                border: "1px solid #f59e0b",
                borderRadius: 8,
                padding: "10px 12px",
                fontSize: 12,
                color: "var(--text)",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 6 }}>高风险操作待确认</div>
              <div style={{ color: "var(--text-secondary)", marginBottom: 8 }}>
                {(hilPending.method || "") + " " + (hilPending.path || "")}
              </div>
              {hilPending.message && (
                <div style={{ color: "var(--text-secondary)", marginBottom: 8 }}>{hilPending.message}</div>
              )}
              <div style={{ color: hilExpired ? "#ef4444" : "var(--text-secondary)", marginBottom: 8 }}>
                {hilExpired
                  ? "确认已过期，请重新发起操作"
                  : `确认剩余时间：${hilRemainSeconds != null ? formatSeconds(hilRemainSeconds) : "--:--"}`}
              </div>
              {hilNotice && (
                <div style={{ color: "var(--color-primary)", marginBottom: 8 }}>{hilNotice}</div>
              )}
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="btn-sm"
                  disabled={loading || !selectedAgent || hilSubmitting || hilExpired}
                  onClick={() => {
                    if (!hilPending) return;
                    const cmd = hilPending.nextConfirmCommand
                      ? `python /workspace/cli/dios ${hilPending.nextConfirmCommand.replace(/^dios\s+/, "")}`
                      : `python /workspace/cli/dios request --confirm-token ${hilPending.token}`;
                    setHilSubmitting(true);
                    setHilNotice("已发送确认指令，等待执行结果...");
                    setHilPending(null);
                    send(`请只执行以下确认命令，不要修改参数：\n${cmd}`);
                  }}
                >
                  确认执行
                </button>
                <button
                  type="button"
                  className="btn-sm btn-secondary"
                  disabled={loading || !selectedAgent || hilSubmitting}
                  onClick={() => {
                    if (!hilPending) return;
                    const token = hilPending.token;
                    setHilSubmitting(false);
                    setHilNotice("已取消本次高风险操作。");
                    setHilPending(null);
                    send(`取消本次高风险操作，不执行。token=${token}`);
                  }}
                >
                  取消
                </button>
              </div>
            </div>
          )}
          {loading && liveEvents.length > 0 && (
            <div
              style={{
                marginBottom: 10,
                background: "var(--bg-surface)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: "8px 10px",
                fontSize: 12,
                color: "var(--text-secondary)",
                lineHeight: 1.5,
              }}
            >
              {liveEvents.map((line, idx) => (
                <div key={`${idx}-${line}`}>{line}</div>
              ))}
            </div>
          )}
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
            {loading ? (
              <button
                onClick={stop}
                style={{
                  padding: "10px 20px", borderRadius: "var(--radius)",
                  background: "var(--bg-surface)", color: "var(--text)",
                  border: "1px solid var(--border)", cursor: "pointer",
                  fontSize: 14, fontWeight: 500, height: 42,
                }}
                title="中断本次对话"
              >
                Stop
              </button>
            ) : (
              <button
                onClick={() => send()}
                disabled={!input.trim() || !selectedAgent}
                style={{
                  padding: "10px 20px", borderRadius: "var(--radius)",
                  background: "var(--color-primary)",
                  color: "#fff", border: "none", cursor: "pointer",
                  fontSize: 14, fontWeight: 500, height: 42, transition: "background 0.15s",
                }}
              >
                Send
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
