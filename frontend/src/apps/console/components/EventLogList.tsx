/** 事件目录、投递日志和任务活动总览共用一个页面，后台轮询保留当前查询的视图。 */
import { useEffect, useState, useCallback, useRef } from "react";
import type { EventCatalog, EventCatalogType, ActivityGanttResponse } from "../../../types";
import { api } from "../../../api/os";
import Drawer from "../../../components/Drawer";
import TaskArtifacts from "./TaskArtifacts";

const STATUS_COLORS: Record<string, string> = {
  received: "var(--color-muted)",
  dispatching: "var(--color-warning)",
  dispatched: "var(--color-success)",
  failed: "var(--color-warning)",
  dead_letter: "var(--color-danger)",
};

const TASK_STATUS_COLORS: Record<string, string> = {
  submitted: "var(--color-muted)",
  working: "var(--color-warning)",
  completed: "var(--color-success)",
  failed: "var(--color-danger)",
  canceled: "var(--color-muted)",
};

type ActivitySubTab = "logs" | "catalog" | "activity";
const EVENT_LOG_AUTO_REFRESH_KEY = "dios:eventlog:autoRefresh";
// 事件查询只读，限制单次等待时间，避免连接挂起后阻塞后续轮询。
const EVENT_QUERY_TIMEOUT_MS = 20_000;

/** 以稳定的 load 回调区分查询，只允许当前查询有一个请求在途。 */
function useEventQuery<T>(load: (signal: AbortSignal) => Promise<T>, isVisible: boolean, intervalMs: number | null) {
  const [result, setResult] = useState<{ load: typeof load; data?: T; hasError: boolean }>();
  const activeLoad = useRef<typeof load | null>(null);
  const pendingRequest = useRef<AbortController | null>(null);

  const refresh = useCallback(async (options?: { replacePending?: boolean }) => {
    const load = activeLoad.current;
    if (!load) return;
    if (pendingRequest.current && !options?.replacePending) return;
    pendingRequest.current?.abort();
    const request = new AbortController();
    pendingRequest.current = request;
    try {
      const signal = AbortSignal.any([request.signal, AbortSignal.timeout(EVENT_QUERY_TIMEOUT_MS)]);
      const data = await load(signal);
      if (!request.signal.aborted) setResult({ load, data, hasError: false });
    } catch (error) {
      if (!request.signal.aborted) {
        console.error("Failed to refresh event view:", error);
        setResult(previous => ({
          load,
          data: previous?.load === load ? previous.data : undefined,
          hasError: true,
        }));
      }
    } finally {
      if (pendingRequest.current === request) pendingRequest.current = null;
    }
  }, []);

  useEffect(() => {
    if (!isVisible) return;
    activeLoad.current = load;
    void refresh();
    return () => {
      activeLoad.current = null;
      pendingRequest.current?.abort();
      pendingRequest.current = null;
    };
  }, [isVisible, load, refresh]);

  useEffect(() => {
    if (!isVisible || intervalMs === null) return;
    const timer = window.setInterval(refresh, intervalMs);
    return () => window.clearInterval(timer);
  }, [isVisible, intervalMs, refresh]);

  return { result: result?.load === load ? result : undefined, refresh };
}

/** 展示事件投递与任务活动，并从任务活动打开产物抽屉。 */
export default function EventLogList({ subTab, onSubTabChange }: { subTab: ActivitySubTab; onSubTabChange: (t: ActivitySubTab) => void }) {
  const [page, setPage] = useState(1);
  const pageSize = 50;
  
  const [sourceFilter, setSourceFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(() => {
    try {
      const raw = localStorage.getItem(EVENT_LOG_AUTO_REFRESH_KEY);
      return raw == null ? true : raw === "1";
    } catch {
      return true;
    }
  });

  const [catalog, setCatalog] = useState<EventCatalog | null>(null);
  const [artifactTask, setArtifactTask] = useState<{ id: string; agent: string } | null>(null);
  const [selectedDate, setSelectedDate] = useState<string>(() => {
    const d = new Date();
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  });
  const [agentFilter, setAgentFilter] = useState("");
  const [filterCategory, setFilterCategory] = useState<string | null>(null);
  const [triggerType, setTriggerType] = useState<EventCatalogType | null>(null);
  const [triggerSource, setTriggerSource] = useState("manual/test");
  const [triggerSubject, setTriggerSubject] = useState("");
  const [triggerData, setTriggerData] = useState("{}");
  const [triggerLoading, setTriggerLoading] = useState(false);

  const fetchLogs = useCallback((signal: AbortSignal) => api.listEvents({
    source: sourceFilter || undefined,
    status: statusFilter || undefined,
    limit: pageSize,
    offset: (page - 1) * pageSize,
  }, signal), [sourceFilter, statusFilter, page]);
  const fetchGantt = useCallback((signal: AbortSignal) => api.getActivityGantt({
    date: selectedDate,
    agent_ids: agentFilter || undefined,
    limit: 1000,
  }, signal), [selectedDate, agentFilter]);
  const { result: logs, refresh: refreshLogs } = useEventQuery(fetchLogs, subTab === "logs", autoRefresh ? 3000 : null);
  const { result: activity, refresh: refreshGantt } = useEventQuery(fetchGantt, subTab === "activity", autoRefresh ? 5000 : null);
  const events = logs?.data?.items ?? [];
  const total = logs?.data?.total ?? 0;
  const gantt = activity?.data;

  useEffect(() => { api.getEventCatalog().then(setCatalog); }, []);

  useEffect(() => {
    try {
      localStorage.setItem(EVENT_LOG_AUTO_REFRESH_KEY, autoRefresh ? "1" : "0");
    } catch {
      // ignore storage errors
    }
  }, [autoRefresh]);

  const fmtTime = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleTimeString("zh-CN", { hour12: false });
  };

  const handleRetry = async (eventId: string) => {
    if (!confirm('确认手动重试此事件？')) return;
    try {
      await api.retryEvent(eventId);
      alert('已重新加入重试队列');
      void refreshLogs({ replacePending: true });
    } catch (error) {
      alert('重试失败: ' + (error instanceof Error ? error.message : '未知错误'));
    }
  };

  const openTrigger = (et: EventCatalogType) => {
    setTriggerType(et);
    setTriggerSource("manual/test");
    setTriggerSubject("");
    setTriggerData("{}");
  };

  const doTrigger = async () => {
    if (!triggerType) return;
    setTriggerLoading(true);
    try {
      let parsed: Record<string, unknown> = {};
      try { parsed = JSON.parse(triggerData); } catch { /* keep empty */ }
      await api.triggerManualEvent({
        event_type: triggerType.type,
        source: triggerSource,
        subject: triggerSubject,
        data: parsed,
      });
      setTriggerType(null);
      onSubTabChange("logs");
    } finally {
      setTriggerLoading(false);
    }
  };

  // 通过配置文件声明的事件没有 Connector 状态，也应出现在目录中。
  const filteredTypes = catalog
    ? catalog.event_types.filter((et) =>
        catalog.connector_status?.[et.category] !== false && (!filterCategory || et.category === filterCategory),
      )
    : [];
  const grouped = Object.entries(
    filteredTypes.reduce<Record<string, EventCatalogType[]>>((acc, et) => {
      (acc[et.category] ??= []).push(et);
      return acc;
    }, {}),
  );

  const totalPages = Math.ceil(total / pageSize);
  const renderGlobalGantt = (data: ActivityGanttResponse) => {
    const startMs = new Date(data.timeline_start).getTime();
    const endMs = new Date(data.timeline_end).getTime();
    const total = Math.max(1, endMs - startMs);
    const barsByAgent = data.bars.reduce<Record<string, typeof data.bars>>((acc, b) => {
      (acc[b.agent_id] ||= []).push(b);
      return acc;
    }, {});
    return (
      <div style={{ marginTop: 10, border: "1px solid var(--border)", borderRadius: 6, padding: 10 }}>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 8 }}>
          智能体活动总览（{data.date || selectedDate}）· {data.bars.length} 个任务
        </div>
        {data.agents.length === 0 && (
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>暂无关联任务记录</div>
        )}
        {data.agents.map((agent) => {
          const bars = barsByAgent[agent.agent_id] || [];
          return (
            <div key={agent.agent_id} style={{ display: "grid", gridTemplateColumns: "180px 1fr", gap: 8, marginBottom: 10 }}>
              <div style={{ fontSize: 12 }}>
                <div>{agent.agent_name}</div>
                <div style={{ color: "var(--text-secondary)" }}>{agent.task_count} 个任务</div>
              </div>
              <div style={{ position: "relative", minHeight: 24, background: "var(--bg-muted)", borderRadius: 4 }}>
                {bars.map((it) => {
                  const s = new Date(it.start_at).getTime();
                  const e = new Date(it.effective_end_at).getTime();
                  const left = ((s - startMs) / total) * 100;
                  const width = (Math.max(1, e - s) / total) * 100;
                  const color = TASK_STATUS_COLORS[it.status] || "var(--color-primary)";
                  return (
                    <button
                      key={it.task_id}
                      type="button"
                      aria-label={`查看任务产物 ${it.agent_name} ${it.task_id}`}
                      onClick={() => setArtifactTask({ id: it.task_id, agent: it.agent_name })}
                      style={{
                        position: "absolute",
                        left: `${Math.max(0, left)}%`,
                        width: `${Math.min(100, Math.max(1.5, width))}%`,
                        top: 3,
                        height: 18,
                        borderRadius: 3,
                        background: color,
                        border: 0,
                        padding: 0,
                        minWidth: 0,
                      }}
                      title={`${it.agent_name} | ${it.status} | ${it.event_type || "no-event"} | ${Math.max(0, Math.round(it.duration_ms / 1000))}s`}
                    >
                      {it.behaviors.map((b, idx) => {
                        const bm = new Date(b.at).getTime();
                        const bl = ((bm - s) / Math.max(1, e - s)) * 100;
                        return (
                          <span
                            key={`${it.task_id}-${idx}`}
                            title={b.label}
                            style={{
                              position: "absolute",
                              left: `${Math.max(0, Math.min(100, bl))}%`,
                              top: 4,
                              width: 6,
                              height: 6,
                              borderRadius: "50%",
                              background: "#fff",
                              border: "1px solid rgba(0,0,0,0.25)",
                              transform: "translateX(-3px)",
                            }}
                          />
                        );
                      })}
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="panel">
      <div className="tabs">
        <button className={`tab ${subTab === "logs" ? "active" : ""}`} onClick={() => onSubTabChange("logs")}>
          事件日志
        </button>
        <button className={`tab ${subTab === "catalog" ? "active" : ""}`} onClick={() => onSubTabChange("catalog")}>
          事件目录
        </button>
        <button className={`tab ${subTab === "activity" ? "active" : ""}`} onClick={() => onSubTabChange("activity")}>
          活动总览
        </button>
      </div>

      {subTab === "logs" && (
        <>
          <div className="event-filters">
            <input
              placeholder="按来源过滤 (如 github)"
              value={sourceFilter}
              onChange={(e) => { setSourceFilter(e.target.value); setPage(1); }}
            />
            <select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}>
              <option value="">全部状态</option>
              <option value="received">received</option>
              <option value="dispatching">dispatching</option>
              <option value="dispatched">dispatched</option>
              <option value="failed">failed</option>
              <option value="dead_letter">dead_letter</option>
            </select>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '13px' }}>
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
              />
              <span>自动刷新</span>
            </label>
            <button className="btn-sm btn-secondary" onClick={() => void refreshLogs({ replacePending: true })}>
              刷新
            </button>
          </div>

          <div className="webhook-hint">
            Webhook URL: <code>POST /api/events/webhook/&#123;source&#125;</code>
            &nbsp; 支持 GitHub / GitLab / Gitea
          </div>

          {logs?.hasError && <p role="alert">事件日志刷新失败，请重试。</p>}

          {!logs && (
            <div style={{ textAlign: 'center', padding: '20px', color: 'var(--text-secondary)' }}>
              <div className="spinner"></div>
              <span>加载中...</span>
            </div>
          )}

          {logs?.data && events.length === 0 && (
            <p className="empty-hint">暂无事件记录</p>
          )}

          {events.length > 0 && (
            <ul className="item-list">
              {events.map((ev) => (
                <li key={ev.id} className="event-item">
                  <div className="event-row" onClick={() => setExpanded(expanded === ev.id ? null : ev.id)}>
                    <span className="event-time">{fmtTime(ev.created_at)}</span>
                    <span className="event-source">{ev.source}</span>
                    <span className="event-type-badge">{ev.event_type}</span>
                    <span className="event-match">{ev.matched_agent_ids.length} 匹配</span>
                    <span
                      className="event-status"
                      style={{ color: STATUS_COLORS[ev.status] || "var(--text-secondary)" }}
                    >
                      {ev.status}
                      {ev.status === 'failed' && ev.retry_count !== undefined && ` (${ev.retry_count}/${ev.max_retries})`}
                    </span>
                  </div>
                  {expanded === ev.id && (
                    <div className="event-detail">
                      <div className="event-detail-row">
                        <strong>Subject:</strong> {ev.subject || "-"}
                      </div>
                      <div className="event-detail-row">
                        <strong>匹配 Agent:</strong>{" "}
                        {ev.matched_agent_ids.length > 0 ? ev.matched_agent_ids.join(", ") : "无匹配"}
                      </div>
                      
                      {ev.status === 'failed' && (
                        <div style={{ background: '#fff3cd', padding: '8px', borderRadius: '4px', marginTop: '8px' }}>
                          <div><strong>状态:</strong> 投递失败，等待重试</div>
                          <div><strong>重试:</strong> {ev.retry_count || 0} / {ev.max_retries || 3}</div>
                          {ev.next_retry_at && (
                            <div><strong>下次重试:</strong> {new Date(ev.next_retry_at).toLocaleString()}</div>
                          )}
                        </div>
                      )}
                      
                      {ev.status === 'dead_letter' && (
                        <div style={{ background: '#f8d7da', padding: '8px', borderRadius: '4px', marginTop: '8px' }}>
                          <div><strong>状态:</strong> 投递失败，已超过最大重试次数</div>
                          <button 
                            className="btn-sm" 
                            style={{ marginTop: '8px' }}
                            onClick={(e) => { e.stopPropagation(); handleRetry(ev.id); }}
                          >
                            手动重试
                          </button>
                        </div>
                      )}
                      
                      {ev.error_message && (
                        <div style={{ marginTop: '8px' }}>
                          <strong>错误详情:</strong>
                          <pre style={{ 
                            background: '#f5f5f5', 
                            padding: '8px', 
                            borderRadius: '4px',
                            fontSize: '12px',
                            overflow: 'auto',
                            maxHeight: '150px'
                          }}>
                            {ev.error_message}
                          </pre>
                        </div>
                      )}
                      <details style={{ marginTop: '8px' }}>
                        <summary>CloudEvent 详情</summary>
                        <pre className="log-box">{JSON.stringify(ev.cloud_event, null, 2)}</pre>
                      </details>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}

          {totalPages > 1 && (
            <div style={{
              display: 'flex',
              justifyContent: 'center',
              alignItems: 'center',
              gap: '8px',
              padding: '16px',
            }}>
              <button
                className="btn-sm"
                disabled={page === 1}
                onClick={() => setPage(page - 1)}
              >
                上一页
              </button>
              
              <span style={{ fontSize: '13px' }}>
                第 {page} / {totalPages} 页（共 {total} 条）
              </span>
              
              <button
                className="btn-sm"
                disabled={page === totalPages}
                onClick={() => setPage(page + 1)}
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}

      {subTab === "catalog" && (
        <div className="catalog-view">
          {catalog && (
            <>
              <div className="catalog-sources">
                <h3 className="catalog-section-title">事件源</h3>
                <div className="catalog-source-grid">
                  {catalog.sources
                    .filter((s) => catalog.connector_status?.[s.id] !== false)
                    .map((s) => (
                      <div
                        key={s.id}
                        className={`catalog-source-card ${filterCategory === s.id ? "catalog-source-active" : ""}`}
                        onClick={() => setFilterCategory(filterCategory === s.id ? null : s.id)}
                      >
                        <span className="catalog-source-name">{s.name}</span>
                        <span className="catalog-source-desc">{s.description}</span>
                      </div>
                    ))}
                </div>
              </div>

              <h3 className="catalog-section-title" style={{ marginTop: 20 }}>
                事件类型
                {filterCategory && (
                  <button className="btn-sm btn-secondary" style={{ marginLeft: 8, fontSize: 11 }} onClick={() => setFilterCategory(null)}>
                    清除过滤
                  </button>
                )}
              </h3>
              {grouped.map(([category, types]) => (
                <div key={category} className="catalog-category">
                  <h4 className="catalog-category-title">{category}</h4>
                  <div className="catalog-type-list">
                    {types.map((et) => (
                      <div key={et.type} className="catalog-type-row">
                        <span className="event-type-badge">{et.type}</span>
                        <span className="catalog-type-desc">{et.description}</span>
                        <button
                          className="btn-sm"
                          onClick={() => openTrigger(et)}
                        >
                          触发
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      )}
      {subTab === "activity" && (
        <>
          <div className="event-filters">
            <input type="date" value={selectedDate} onChange={(e) => setSelectedDate(e.target.value)} />
            <input
              placeholder="按 agent_id 过滤（逗号分隔）"
              value={agentFilter}
              onChange={(e) => setAgentFilter(e.target.value)}
            />
            <button className="btn-sm btn-secondary" onClick={() => void refreshGantt({ replacePending: true })}>
              刷新
            </button>
          </div>
          {activity?.hasError && <p role="alert">活动总览刷新失败，请重试。</p>}
          {!activity && (
            <div style={{ textAlign: "center", padding: "20px", color: "var(--text-secondary)" }}>
              <div className="spinner"></div>
              <span>加载中...</span>
            </div>
          )}
          {gantt && renderGlobalGantt(gantt)}
        </>
      )}

      {artifactTask && (
        <Drawer open title={`${artifactTask.agent} · 任务产物`} onClose={() => setArtifactTask(null)}>
          <TaskArtifacts key={artifactTask.id} taskId={artifactTask.id} />
        </Drawer>
      )}

      <Drawer
        open={triggerType !== null}
        title={`手动触发 - ${triggerType?.type ?? ""}`}
        onClose={() => setTriggerType(null)}
      >
        {triggerType && (
          <div className="drawer-form">
            <label>事件类型</label>
            <input value={triggerType.type} disabled />

            <label>来源 (source)</label>
            <input
              placeholder="manual/test"
              value={triggerSource}
              onChange={(e) => setTriggerSource(e.target.value)}
            />

            <label>主题 (subject)</label>
            <input
              placeholder="如 refs/heads/main"
              value={triggerSubject}
              onChange={(e) => setTriggerSubject(e.target.value)}
            />

            <label>数据 (JSON)</label>
            <textarea
              rows={6}
              value={triggerData}
              onChange={(e) => setTriggerData(e.target.value)}
              style={{ fontFamily: "monospace", fontSize: 13 }}
            />

            <div className="drawer-actions">
              <button onClick={doTrigger} disabled={triggerLoading}>
                {triggerLoading ? "发送中..." : "触发事件"}
              </button>
              <button className="btn-secondary" onClick={() => setTriggerType(null)}>取消</button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
