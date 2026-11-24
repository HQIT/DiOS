import { t as translateUI } from "../../../lib/i18n";
import { useEffect, useState } from "react";
import type { ConnectorSourcePattern, EventCatalogType, Subscription } from "../../../types";
import { api } from "../../../api/os";

interface Props {
  agentId: string;
}

const EMPTY: Omit<Subscription, "id" | "agent_id" | "created_at"> = {
  source_pattern: "",
  event_types: [],
  filter_rules: {},
  cron_expression: "",
  enabled: true,
};
const SCHEDULE_STATES: Record<string, string> = {
  pending: "等待派发", running: "原任务处理中", dead_letter: "死信待处理",
  approval_required: "等待审批", succeeded: "已完成", denied: "已拒绝",
  discarded: "已忽略", cancelled: "已取消",
};
const formatScheduleTime = (value: string) => new Date(value).toLocaleString(undefined, { timeZoneName: "short" });

export default function SubscriptionEditor({ agentId }: Props) {
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [sourceOptions, setSourceOptions] = useState<ConnectorSourcePattern[]>([]);
  const [allEventTypes, setAllEventTypes] = useState<EventCatalogType[]>([]);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ ...EMPTY });
  const [error, setError] = useState("");

  const inNamespace = (sourcePattern: string, namespacePattern: string): boolean => {
    const ns = namespacePattern.replace(/\/\*$/, "");
    return sourcePattern === namespacePattern || sourcePattern === ns || sourcePattern.startsWith(`${ns}/`);
  };

  const eventOptionsFor = (sourcePattern: string): { value: string; label: string }[] => {
    const matched = sourceOptions.find((o) => inNamespace(sourcePattern, o.source_pattern));
    const allowed = new Set(matched?.event_types || allEventTypes.map((e) => e.type));
    return allEventTypes
      .filter((e) => allowed.has(e.type))
      .map((e) => ({ value: e.type, label: e.description || e.type }));
  };

  const load = () =>
    Promise.all([api.listSubscriptions(agentId), api.listConnectorSourcePatterns(), api.getEventCatalog()]).then(([s, opts, catalog]) => {
      setSubs(s);
      setSourceOptions(opts);
      setAllEventTypes(catalog.event_types || []);
      setDraft((prev) => ({
        ...prev,
        source_pattern: prev.source_pattern || opts[0]?.source_pattern || "",
      }));
    }).catch((err) => setError(err instanceof Error ? err.message : String(err)));
  useEffect(() => { load(); }, [agentId]);

  useEffect(() => {
    if (!adding) return;
    const allowed = new Set(eventOptionsFor(draft.source_pattern).map((o) => o.value));
    setDraft((d) => ({
      ...d,
      event_types: d.event_types.filter((e) => allowed.has(e)),
    }));
  }, [adding, draft.source_pattern, sourceOptions, allEventTypes]);

  const isCron = draft.event_types.includes("cron.tick");

  const toggleType = (t: string) => {
    setDraft((d) => ({
      ...d,
      event_types: d.event_types.includes(t)
        ? d.event_types.filter((x) => x !== t)
        : [...d.event_types, t],
    }));
  };

  const handleAdd = async () => {
    if (draft.event_types.length === 0) return;
    try {
      await api.createSubscription(agentId, draft);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setAdding(false);
    setDraft({ ...EMPTY, source_pattern: sourceOptions[0]?.source_pattern || "" });
    load();
  };

  const handleToggle = async (sub: Subscription) => {
    try {
      await api.updateSubscription(agentId, sub.id, { enabled: !sub.enabled });
      setError(""); load();
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  const handleDelete = async (subId: string) => {
    try {
      await api.deleteSubscription(agentId, subId);
      setError(""); load();
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  return (
    <div className="sub-editor">
      <div className="sub-header">
        <span className="sub-title">{translateUI("事件订阅")}</span>
        <button className="btn-sm btn-secondary" onClick={() => load()}>{translateUI("刷新")}</button>
        {!adding && (
          <button className="btn-sm" onClick={() => setAdding(true)}>{translateUI("+ 添加订阅")}</button>
        )}
      </div>
      {error && <p role="alert">{error}</p>}

      {subs.length === 0 && !adding && (
        <p className="empty-hint">{translateUI("暂未配置事件订阅，该 Agent 不会被事件触发")}</p>
      )}

      {subs.map((s) => (
        <div key={s.id} className={`sub-card ${s.enabled ? "" : "sub-disabled"}`}>
          <div className="sub-card-header">
            <span className="sub-card-source">{translateUI(s.source_pattern)}</span>
            {s.cron_expression && (
              <span className="event-type-badge" title={translateUI("CRON 表达式")}>{translateUI(s.cron_expression)}</span>
            )}
            <label className="sub-toggle">
              <input type="checkbox" checked={s.enabled} onChange={() => handleToggle(s)} />
              <span>{translateUI(s.enabled ? "已启用" : "已停用")}</span>
            </label>
            <button className="btn-sm btn-danger" onClick={() => handleDelete(s.id)}>{translateUI("删除")}</button>
          </div>
          <div className="sub-card-types">
            {s.event_types.map((t) => (
              <span key={t} className="event-type-badge">{translateUI(t)}</span>
            ))}
          </div>
          {s.schedule && <div className="sub-schedule-status">
            <p>{translateUI("定时规则：UTC · 运行中合并后续触发")}</p>
            <p>{translateUI("下次触发")}: {s.schedule.next_due ? formatScheduleTime(s.schedule.next_due) : translateUI("已停用")}</p>
            {s.schedule.active && <p>{translateUI("调度状态")}: {translateUI(SCHEDULE_STATES[s.schedule.active.state] || s.schedule.active.state)} · {s.schedule.active.event_id}</p>}
            {s.schedule.buffered && <p>{translateUI("待补触发次数")}: {s.schedule.buffered.trigger_count}</p>}
            {(s.schedule.error || s.schedule.active?.error) && <p role="status">{s.schedule.error || s.schedule.active?.error}</p>}
            {s.schedule.active?.next_retry_at && <p>{translateUI("下次重试")}: {formatScheduleTime(s.schedule.active.next_retry_at)}</p>}
            <p>{translateUI("观测时间")}: {formatScheduleTime(s.schedule.observed_at)}</p>
          </div>}
        </div>
      ))}

      {adding && (
        <div className="sub-add-form">
          <label>{translateUI("事件来源")}</label>
          <select
            value={sourceOptions.some((o) => o.source_pattern === draft.source_pattern) ? draft.source_pattern : "__custom"}
            onChange={(e) => {
              if (e.target.value !== "__custom") setDraft({ ...draft, source_pattern: e.target.value });
            }}
          >
            {sourceOptions.map((o) => (
              <option key={o.source_pattern} value={o.source_pattern}>{translateUI(o.label)}</option>
            ))}
            <option value="__custom">{translateUI("自定义 pattern...")}</option>
          </select>
          {!sourceOptions.some((o) => o.source_pattern === draft.source_pattern) && (
            <input
              placeholder={translateUI("如 github/my-org/my-repo")}
              value={draft.source_pattern}
              onChange={(e) => setDraft({ ...draft, source_pattern: e.target.value })}
            />
          )}

          <label>{translateUI("监听的事件类型（至少选一个）")}</label>
          <div className="event-type-grid">
            {eventOptionsFor(draft.source_pattern).map((o) => (
              <label key={o.value} className="event-type-option">
                <input
                  type="checkbox"
                  checked={draft.event_types.includes(o.value)}
                  onChange={() => toggleType(o.value)}
                />
                <span>{translateUI(o.label)}</span>
              </label>
            ))}
          </div>

          {isCron && (
            <>
              <label>{translateUI("CRON 表达式 *")}</label>
              <input
                placeholder={translateUI("如 0 9 * * *（每天 9 点）")}
                value={draft.cron_expression}
                onChange={(e) => setDraft({ ...draft, cron_expression: e.target.value })}
              />
            </>
          )}

          <div className="drawer-actions">
            <button onClick={handleAdd}>{translateUI("确认添加")}</button>
            <button className="btn-secondary" onClick={() => { setAdding(false); setDraft({ ...EMPTY, source_pattern: sourceOptions[0]?.source_pattern || "" }); }}>{translateUI("取消")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
