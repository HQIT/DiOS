import { t as translateUI, languageTag } from "../../../lib/i18n";
import { useEffect, useState } from "react";
import { toolApi, RuntimeApiError } from "../../../api/runtime";
import type { ToolInvocation } from "../../../api/runtime";

const states: Record<string, string> = {
  awaiting_approval: "待审批", prepared: "等待派发", dispatched: "已派发，等待结果",
  succeeded: "调用完成", failed: "工具返回失败", denied: "已拒绝", result_unknown: "结果待对账",
};
const errors: Record<string, string> = {
  AUTH_PROVIDER_UNAVAILABLE: "身份服务暂不可用，请稍后重试。",
  AUTH_PRINCIPAL_REVOKED: "来源授权已失效，请重新获取授权。",
  AUTH_PRINCIPAL_EXPIRED: "来源授权已失效，请重新获取授权。",
  APPROVER_UNAUTHENTICATED: "审批凭据无效。", APPROVER_FORBIDDEN: "此审批人没有该工具的审批权限。",
  APPROVER_MUST_BE_INDEPENDENT: "需要由独立于任务发起者的审批人处理。",
  APPROVER_SEPARATE_CREDENTIAL_REQUIRED: "请输入独立审批凭据。",
  APPROVER_PROVIDER_REQUIRED: "尚未配置审批人。", VERSION_CONFLICT: "状态已更新，请刷新后确认。",
  AUTH_REVOKED: "工作授权已撤销。", STALE_OWNER: "原执行实例已失去所有权。",
  TOOL_AUTHORIZATION_EXPIRED: "本次调用授权已过期。", WORK_POLICY_CHANGED: "策略已变化，需要重新申请授权。",
  TOOL_RECEIPT_NOT_FOUND: "尚未收到本地回执，请保留原调用等待结果。",
  TOOL_RECEIPT_BINDING_MISMATCH: "回执与原调用身份不匹配，需人工核对。",
  TOOL_RECEIPT_CONFLICT: "回执与已记录结果冲突，需人工核对。",
  TOOL_DISPATCH_EVIDENCE_REQUIRED: "原调用派发证据不完整，暂不能恢复结果。",
  TOOL_REGISTRATION_CHANGED: "工具配置已变化，需要重新申请授权。",
};

export default function ToolActions({ runId, active, canApprove = true, operator = true }: { runId: string; active: boolean; canApprove?: boolean; operator?: boolean }) {
  const [items, setItems] = useState<ToolInvocation[]>([]);
  const [message, setMessage] = useState(""); const [failure, setFailure] = useState("");
  const [token, setToken] = useState(""); const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(""); const [refresh, setRefresh] = useState(0);
  const [limited, setLimited] = useState(false); const [fresh, setFresh] = useState(false);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setItems([]); setToken(""); setReason(""); setFresh(false);
    async function poll() {
      try {
        const result = await toolApi.list(runId, controller.signal);
        if (controller.signal.aborted) return;
        setItems(previous => result.items.map(item => {
          const saved = previous.find(x => x.id === item.id);
          return saved && saved.version > item.version ? saved : item;
        }));
        setLimited(result.at_limit); setFresh(true);
        setMessage(result.migration_required ? "工具调用账本待初始化。" : "");
      } catch (error) {
        if (controller.signal.aborted) return;
        setFresh(false);
        setMessage(error instanceof RuntimeApiError && error.code === "TOOL_GATE_NOT_CONFIGURED"
          ? "工具授权入口尚未启用。" : "工具调用状态更新中断，请刷新后操作。");
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 3000);
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, refresh]);

  async function act(item: ToolInvocation, action: "approved" | "denied" | "dispatch" | "reconcile" | "recover") {
    if (busy) return;
    setBusy(item.id); setFailure("");
    const credential = token; setToken("");
    try {
      const result = action === "dispatch" ? await toolApi.dispatch(runId, item)
        : action === "reconcile" ? await toolApi.reconcile(runId, item)
        : action === "recover" ? await toolApi.recover(runId, item)
        : await toolApi.approve(runId, item, action, credential, reason);
      setItems(previous => previous.map(x => x.id === result.id && result.version >= x.version ? result : x));
    } catch (error) {
      setFailure(error instanceof RuntimeApiError ? errors[error.code] || translateUI("操作未完成：{0}", [error.code])
        : "连接中断。派发结果请刷新查询。");
    } finally { setBusy(""); }
  }
  const pending = items.some(x => x.state === "awaiting_approval" && !x.expired);
  return <section className="tr-card tr-tools">
    <div className="tr-section-title"><h3>{translateUI("工具调用与审批")}</h3><button onClick={() => setRefresh(x => x + 1)} disabled={!!busy}>{translateUI("刷新")}</button></div>
    {message && <p className="tr-notice" role="status">{translateUI(message)}</p>}
    {failure && <p className="tr-error" role="alert">{translateUI(failure)}</p>}
    {!items.length && !message && <p className="tr-muted">{translateUI("本任务暂无工具调用申请。")}</p>}
    {canApprove && pending && active && <div className="tr-approval-credentials">
      <label>{translateUI("独立审批凭据")}<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} disabled={!!busy} /></label>
      <label>{translateUI("审批说明")}<textarea value={reason} onChange={e => setReason(e.target.value)} maxLength={2000} rows={2} disabled={!!busy} /></label>
    </div>}
    {items.map(item => <article key={item.id} className="tr-tool-item">
      <div className="tr-section-title"><strong>{translateUI(item.tool_id)}</strong><span className="tr-badge">{translateUI(states[item.state] || item.state)}</span></div>
      <p>{translateUI(item.permission.action)} · {translateUI(item.permission.resource_id)} · {translateUI(item.permission.resource_version)}</p>
      <details><summary>{translateUI("查看调用参数")}</summary><pre className="tr-output">{JSON.stringify(item.arguments, null, 2)}</pre></details>
      <p className="tr-muted">{translateUI("有效至")}{translateUI(new Date(item.expires_at * 1000).toLocaleString(languageTag()))}{translateUI(item.expired ? " · 已过期" : "")}
        {translateUI(item.approver ? translateUI(" · 审批人 {0}", [item.approver]) : "")}</p>
      {item.approval_reason && <p>{translateUI(item.approval_reason)}</p>}
      {item.reason && <p className="tr-muted">{translateUI(item.reason)}</p>}
      {item.receipt_available && ["dispatched", "result_unknown"].includes(item.state) && <p className="tr-muted">{translateUI("已保留本地回执，可校验并恢复原调用结果。")}</p>}
      {(item.state === "result_unknown" || item.dispatch_overdue) && <p className="tr-notice">{translateUI("原调用结果待确认，请保留原调用记录并核对上游结果。")}</p>}
      <div className="tr-actions">
        {canApprove && active && !item.expired && item.state === "awaiting_approval" && <>
          <button disabled={!!busy || !token || !fresh} onClick={() => void act(item, "approved")}>{translateUI("批准本次调用")}</button>
          <button disabled={!!busy || !token || !fresh} onClick={() => void act(item, "denied")}>{translateUI("拒绝")}</button>
        </>}
        {operator && active && !item.expired && item.state === "prepared" && <button disabled={!!busy || !fresh} onClick={() => void act(item, "dispatch")}>{translateUI("派发本次调用")}</button>}
        {operator && item.receipt_available && ["dispatched", "result_unknown"].includes(item.state) && <button disabled={!!busy || !fresh} onClick={() => void act(item, "recover")}>{translateUI("校验并恢复回执")}</button>}
        {operator && item.dispatch_overdue && <button disabled={!!busy || !fresh} onClick={() => void act(item, "reconcile")}>{translateUI("登记为待对账")}</button>}
      </div>
    </article>)}
    {limited && <p className="tr-muted">{translateUI("显示最近 50 次调用。可按调用 ID 查询历史详情。")}</p>}
  </section>;
}
