import { t as translateUI, languageTag } from "../../../lib/i18n";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { runtimeApi, RuntimeApiError } from "../../../api/runtime";
import type { Profile, Sample, RunSummary, RunDetail, Preview } from "../../../api/runtime";
import "./task-runs.css";
import WorkerStatusPanel from "./WorkerStatusPanel";
import ToolActions from "./ToolActions";

const LABELS: Record<string, string> = {
  queued: "排队中", executing: "执行中", claimed: "已领取", preparing: "准备中", creating_session: "创建会话",
  waiting_model: "等待模型", streaming: "输出中", validating: "校验成果", succeeded: "执行完成", completed: "执行完成",
  failed: "执行失败", cancelled: "已取消", reconciliation_required: "等待恢复对账", result_unknown: "结果待确认",
  retry_wait: "等待重试", abandoned: "尝试已结束",
};
const AUTH_STATES: Record<string, string> = { active: "有效", expired: "已过期", revoked: "已撤销", closed: "已结束" };
const ERRORS: Record<string, string> = {
  AUTH_PROVIDER_UNAVAILABLE: "身份服务暂不可用，请稍后重试。",
  AUTH_PRINCIPAL_REVOKED: "来源授权已失效，请重新获取授权。",
  AUTH_PRINCIPAL_EXPIRED: "来源授权已失效，请重新获取授权。",
  INVALID_WORK_INPUT: "任务输入不符合登记的输入要求，请检查字段、大小和内容摘要。",
  DYNAMIC_INPUT_NOT_ALLOWED: "此执行配置只接受已登记的输入。",
  RUNTIME_CONSOLE_DISABLED: "任务入口尚未启用。管理员完成运行环境配置后即可使用。",
  RUNTIME_OPERATOR_TOKEN_REQUIRED: "任务入口需要配置操作员访问令牌。",
  RUNTIME_CONFIGURATION_REQUIRED: "运行数据库、执行配置或成果目录尚未配置。",
  RUNTIME_UNAVAILABLE: "运行服务暂不可用，请稍后刷新。",
  PROFILE_NOT_AVAILABLE: "所选 Agent 配置已停用，请刷新配置列表。",
  VERSION_CONFLICT: "任务状态已更新，请确认最新状态后再次操作。",
  PREVIEW_TOO_LARGE: "文件超过预览大小，请下载查看。",
  PREVIEW_NOT_TEXT: "该文件不支持文本预览，请下载查看。",
  ARTIFACT_PROVIDER_UNAVAILABLE: "成果存储当前不可用。",
  ARTIFACT_DIGEST_MISMATCH: "成果校验未通过，请联系管理员。",
  IDEMPOTENCY_CONFLICT: "提交内容与原请求不一致，请刷新后核对任务。",
  WORK_POLICY_REQUIRED: "该 Agent 尚未配置工作授权策略。",
  WORK_PROFILE_CHANGED: "执行配置与获准版本不一致，请更新策略后重试。",
  WORK_PERMISSION_DENIED: "当前授权范围不包含这项工作。",
  WORK_POLICY_CHANGED: "授权策略已变化，需要重新确认任务授权。",
  WORK_AUTH_MIGRATION_REQUIRED: "工作授权数据结构尚未初始化，请联系管理员。",
  RUNTIME_DRAINING: "运行环境正在排空，恢复服务后可发起新任务。",
};
function errorText(error: unknown) {
  if (error instanceof RuntimeApiError) return ERRORS[error.code] || (error.status === 401 ? "访问令牌失效，请重新登录。" : translateUI("操作未完成：{0}", [error.code]));
  return "连接中断，已保留页面信息。请重试。";
}
function canonicalInput(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalInput);
  if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, canonicalInput(v)]));
  return value;
}
const terminal = (state: string) => ["succeeded", "failed", "cancelled"].includes(state);
const time = (stamp?: number | null) => stamp ? new Date(stamp * 1000).toLocaleString(languageTag(), { hour12: false }) : "暂无";
function Badge({ state }: { state: string }) { return <span className={`tr-badge tr-${state}`}>{translateUI(LABELS[state] || state)}</span>; }

export default function TaskRunsPage({ runId }: { runId: string }) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [profile, setProfile] = useState(""); const [sample, setSample] = useState("");
  const [customMode, setCustomMode] = useState(false);
  const [customDrafts, setCustomDrafts] = useState<Record<string, string>>({});
  const [goal, setGoal] = useState(() => translateUI("根据所选输入完成任务，返回可核验的结果与依据。"));
  const [catalogError, setCatalogError] = useState(""); const [listError, setListError] = useState("");
  const [actionError, setActionError] = useState(""); const [detailError, setDetailError] = useState("");
  const [items, setItems] = useState<RunSummary[]>([]); const [cursor, setCursor] = useState<string>();
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail>(); const [busy, setBusy] = useState(false);
  const [operatorMode, setOperatorMode] = useState(false);
  const [principalName, setPrincipalName] = useState("");
  const [refresh, setRefresh] = useState(0); const [artifact, setArtifact] = useState("");
  const [preview, setPreview] = useState<Preview>(); const [previewError, setPreviewError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const keyRef = useRef<{ content: string; key: string } | null>(null);
  const selectedId = runId || "";

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([runtimeApi.profiles(controller.signal), runtimeApi.samples(controller.signal), runtimeApi.identity(controller.signal)]).then(([p, s, identity]) => {
      setOperatorMode(identity.mode === "operator"); setPrincipalName(identity.principal?.subject || "");
      setProfiles(p.items); setSamples(s.items); setCatalogError("");
      setProfile(current => p.items.some(x => x.id === current) ? current : p.items[0]?.id || "");
      setSample(current => s.items.some(x => x.id === current) ? current : s.items[0]?.id || "");
    }).catch(error => { if (!controller.signal.aborted) setCatalogError(errorText(error)); });
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await runtimeApi.list(cursor, controller.signal);
        if (controller.signal.aborted) return;
        setItems(current => value.items.map(item => {
          const prior = current.find(x => x.id === item.id);
          return prior && prior.version > item.version ? prior : item;
        })); setNextCursor(value.next_cursor); setListError("");
      } catch (error) { if (!controller.signal.aborted) setListError(errorText(error)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [cursor, refresh]);

  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setDetail(undefined); setDetailError(""); setArtifact(""); setPreview(undefined); setActionError("");
    if (!selectedId) return () => controller.abort();
    async function poll() {
      try {
        const value = await runtimeApi.detail(selectedId, controller.signal);
        if (controller.signal.aborted) return;
        setDetail(current => current?.id === value.id && current.version > value.version ? current : value); setDetailError("");
        setItems(current => current.map(item => item.id === value.id && value.version >= item.version
          ? { ...item, state: value.state, version: value.version, updated_at: value.updated_at,
              attempt_id: value.attempt_id, cancel_requested: value.cancel_requested } : item));
        setArtifact(current => value.access && !value.access.includes("artifact.read") ? "" : current || value.artifacts.find(a => a.kind === "result")?.id || "");
        if (terminal(value.state)) return;
      } catch (error) { if (!controller.signal.aborted) setDetailError(errorText(error)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [selectedId, refresh]);

  useEffect(() => {
    const controller = new AbortController(); setPreview(undefined); setPreviewError("");
    if (selectedId && artifact) runtimeApi.preview(selectedId, artifact, controller.signal).then(value => {
      if (!controller.signal.aborted) setPreview(value);
    }).catch(error => { if (!controller.signal.aborted) setPreviewError(errorText(error)); });
    return () => controller.abort();
  }, [selectedId, artifact]);

  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    setBusy(true); setActionError("");
    let supplied: Record<string, unknown> | undefined;
    if (customInput) {
      try {
        const value = JSON.parse(customDrafts[profile] || "{}");
        if (value === null || Array.isArray(value) || typeof value !== "object") throw new Error();
        supplied = canonicalInput(value) as Record<string, unknown>;
      } catch { setActionError("任务输入必须是有效的 JSON 对象。"); setBusy(false); return; }
    }
    const input = { profile_id: profile, ...(customInput ? {input_payload: supplied} : {sample_id: sample}), goal: goal.trim() };
    const content = JSON.stringify(input);
    if (!keyRef.current) {
      try { keyRef.current = JSON.parse(sessionStorage.getItem("dios_pending_task") || "null"); } catch { /* storage unavailable */ }
    }
    if (keyRef.current?.content !== content) keyRef.current = { content, key: crypto.randomUUID() };
    try { sessionStorage.setItem("dios_pending_task", JSON.stringify(keyRef.current)); } catch { /* in-memory retry still works */ }
    try {
      const value = await runtimeApi.submit({ ...input, idempotency_key: keyRef.current.key });
      keyRef.current = null;
      try { sessionStorage.removeItem("dios_pending_task"); } catch { /* storage unavailable */ }
      setCursor(undefined); setRefresh(x => x + 1);
      window.location.hash = `console/tasks/${value.id}`;
    } catch (error) { setActionError(errorText(error)); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (!detail || busy) return;
    setBusy(true); setActionError("");
    try { const updated = await runtimeApi.cancel(detail.id, detail.version); setDetail(current => current?.id === updated.id && current.version > updated.version ? current : updated); }
    catch (error) { setActionError(errorText(error)); if (error instanceof RuntimeApiError && error.code === "VERSION_CONFLICT") setRefresh(x => x + 1); }
    finally { setBusy(false); }
  }
  async function revoke() {
    if (!detail || busy) return;
    setBusy(true); setActionError("");
    try {
      const updated = await runtimeApi.revoke(detail.id, detail.version);
      setDetail(current => current?.id === updated.id && current.version > updated.version ? current : updated);
    } catch (error) { setActionError(errorText(error)); }
    finally { setBusy(false); }
  }
  async function download(markdown: boolean) {
    if (!selectedId || !artifact || downloading) return;
    setDownloading(true); setPreviewError("");
    try { await runtimeApi.download(selectedId, artifact, markdown); }
    catch (error) { setPreviewError(errorText(error)); }
    finally { setDownloading(false); }
  }
  const chosenProfile = profiles.find(p => p.id === profile);
  const availableSamples = samples.filter(s => !chosenProfile?.input_ids || chosenProfile.input_ids.includes(s.id));
  useEffect(() => {
    if (!availableSamples.some(s => s.id === sample)) setSample(availableSamples[0]?.id || "");
  }, [profile, samples, sample, profiles]);
  const can = (action: string) => !detail?.access || detail.access.includes(action);
  const customInput = !!chosenProfile?.dynamic_inputs && (customMode || availableSamples.length === 0);
  const policyMissing = chosenProfile?.governance === "e2ag" && !chosenProfile.policy_configured;
  const source = samples.find(s => s.id === sample);
  const active = detail?.attempts.find(a => a.id === detail.attempt_id);

  return <section className="task-runs">
    <header className="tr-heading"><div><p className="tr-eyebrow">{translateUI("DiOS · 任务与成果")}</p><h2>{translateUI("交给 Agent，带回成果")}</h2>
      <p>{translateUI("选择已登记的工作类型与执行配置，跟踪进展并查看有来源的成果。")}</p></div>
      <button onClick={() => setRefresh(x => x + 1)}>{translateUI("刷新")}</button></header>
    {catalogError && <p className="tr-notice" role="status">{translateUI(catalogError)}</p>}
    {actionError && <p className="tr-error" role="alert">{translateUI(actionError)}</p>}
    <div className="tr-layout"><aside className="tr-sidebar">
      <form className="tr-card" onSubmit={submit}><h3>{translateUI("发起任务")}</h3>
        <label>{translateUI("执行 Agent")}<select value={profile} onChange={e => setProfile(e.target.value)} disabled={busy || !profiles.length} required>
          {!profiles.length && <option value="">{translateUI("暂无已启用的执行配置")}</option>}
          {profiles.map(p => <option key={p.id} value={p.id} disabled={p.governance === "e2ag" && !p.policy_configured}>{p.name}{translateUI(p.synthetic ? " · 合成端点" : "")}{translateUI(p.governance === "e2ag" && !p.policy_configured ? " · 待配置授权" : "")}</option>)}
        </select></label>
        {profiles.find(p => p.id === profile)?.description && <p className="tr-muted">{profiles.find(p => p.id === profile)?.description}</p>}
        {chosenProfile?.dynamic_inputs && availableSamples.length > 0 && <label><input type="checkbox" checked={customMode} onChange={e => setCustomMode(e.target.checked)} disabled={busy}/>{translateUI("使用自定义输入")}</label>}
        {customInput ? <>
          <label>{translateUI("自定义任务输入（JSON）")}<textarea aria-label={translateUI("自定义任务输入（JSON）")} rows={8} maxLength={12288} value={customDrafts[profile] || ""} onChange={e => setCustomDrafts(current => ({...current, [profile]: e.target.value}))} disabled={busy} required /></label>
          <details><summary>{translateUI("输入要求")}</summary><pre className="tr-source">{JSON.stringify(chosenProfile?.input_schema, null, 2)}</pre></details>
        </> : <>
          <label>{translateUI("任务输入")}<select value={sample} onChange={e => setSample(e.target.value)} disabled={busy} required>
            {availableSamples.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select></label>
          {source && <pre className="tr-source"><code>{typeof source.source === "string" ? source.source : JSON.stringify(source, null, 2)}</code></pre>}
        </>}
        <label>{translateUI("工作目标")}<textarea aria-label={translateUI("工作目标")} value={goal} onChange={e => setGoal(e.target.value)} maxLength={2000} rows={4} required disabled={busy} /></label>
        <button className="tr-primary" type="submit" disabled={busy || !profile || (!customInput && !sample) || (customInput && !customDrafts[profile]?.trim()) || !goal.trim() || !!catalogError || policyMissing}>
          {translateUI(busy ? "提交中…" : "发起任务")}</button>
        <p className="tr-muted">{translateUI("按登记的输入与执行配置运行，保留任务状态和成果依据。")}</p>
      </form>
      <div className="tr-card"><div className="tr-section-title"><h3>{translateUI("任务记录")}</h3><span className="tr-muted">{translateUI(items.length)}{translateUI("项")}</span></div>
        {listError && <p className="tr-error" role="alert">{translateUI(listError)}</p>}
        {!items.length && !listError && <p className="tr-empty-small">{translateUI("发起任务后，这里会保留执行记录与成果。")}</p>}
        <div className="tr-run-list">{items.map(item => <button key={item.id} className={`tr-run ${selectedId === item.id ? "selected" : ""}`}
          onClick={() => { window.location.hash = `console/tasks/${item.id}`; }}>
          <span className="tr-run-title">{item.goal || translateUI("工作任务")}</span><Badge state={item.state} />
          <small>{item.display.name || item.profile_id} · {translateUI(time(item.created_at))}</small>
        </button>)}</div>
        <div className="tr-actions">{cursor && <button onClick={() => setCursor(undefined)}>{translateUI("返回最新")}</button>}
          {nextCursor && <button onClick={() => setCursor(nextCursor)}>{translateUI("更早的任务")}</button>}</div>
      </div>
    </aside>
    <main className="tr-detail">
      {principalName && <p className="tr-muted">{translateUI("当前主体：{0}", [principalName])}</p>}
      {operatorMode && <WorkerStatusPanel />}
      {!selectedId && <div className="tr-card tr-welcome"><span className="tr-symbol">↗</span><h3>{translateUI("从一件具体工作开始")}</h3>
        <p>{translateUI("在左侧发起任务，或选择已有任务查看执行过程与成果。")}</p><div className="tr-steps"><span>{translateUI("① 发起任务")}</span><span>{translateUI("② 跟踪执行")}</span><span>{translateUI("③ 查看成果")}</span></div></div>}
      {detailError && <p className="tr-error" role="alert">{translateUI(detailError)}{translateUI("当前保留的是上次取得的信息。")}</p>}
      {selectedId && !detail && !detailError && <p className="tr-empty-small" role="status">{translateUI("正在读取任务…")}</p>}
      {detail && <>
        <section className="tr-card"><div className="tr-section-title"><h3>{detail.goal}</h3><Badge state={detail.state} /></div>
          <p className="tr-muted">{detail.display.name || detail.profile_id} · {translateUI(detail.display.synthetic ? "合成端点" : detail.display.implementation)}</p>
          <div className="tr-metrics"><div><span>{translateUI("当前阶段")}</span><strong>{translateUI(detail.cancel_requested && !terminal(detail.state) ? "取消待确认" : detail.state === "retry_wait" ? "冷却待重试" : active?.runtime_state === "paused" && !terminal(detail.state) ? "执行已暂停" : LABELS[active?.state || detail.state] || active?.state)}</strong></div>
            <div><span>{translateUI("进程观测")}</span><strong>{translateUI(detailError ? "更新中断" : !active ? "等待 Worker" : active.freshness === "final" ? "已结束" : active.freshness === "stale" ? "观测过期" : active.runtime_state === "paused" ? "执行已暂停" : active.confirmed_running ? "确认运行中" : "尚未确认运行")}</strong></div>
            <div><span>{translateUI("最近有效输出")}</span><strong>{translateUI(time(active?.last_output_at))}</strong></div>
          </div>
          {detail.state === "queued" && <p className="tr-notice">{translateUI("任务已入队，等待对应 Agent 的 Worker 领取。")}</p>}
          {detail.state === "reconciliation_required" && <p className="tr-notice">{translateUI("正在等待原执行结果对账，任务身份与已有输出已保留。")}</p>}
          {active?.error && <p className="tr-error">{translateUI("失败原因：")}{translateUI(active.error.code)}</p>}
          {detail.recovery && <p className="tr-notice" role="status">{translateUI("故障分类：")}{translateUI(({ network: "网络故障", rate_limit: "服务限流", quota: "额度不足", session: "会话创建失败", process: "进程退出", format: "结果格式", evidence: "证据不足", authorization: "授权限制" } as Record<string,string>)[detail.recovery.category] || detail.recovery.category)}
            {detail.state === "retry_wait" && detail.recovery.retry_at && <>{translateUI("· 下次重试")}{translateUI(time(Math.max(detail.recovery.retry_at, detail.recovery.circuit?.retry_at || 0)))}{translateUI("，继续原任务")}</>}
            {detail.recovery.reason && <>{translateUI("· 自动重试受限：")}{translateUI(detail.recovery.reason)}</>}
          </p>}
          <div className="tr-actions"><span className="tr-muted">{translateUI("最近更新：")}{translateUI(time(detail.observed_at))}</span>
            {can("run.cancel") && !terminal(detail.state) && <button disabled={busy || detail.cancel_requested || !!detailError} onClick={() => void cancel()}>{translateUI(detail.cancel_requested ? "取消待确认" : "取消任务")}</button>}</div>
        </section>
        <section className="tr-card"><div className="tr-section-title"><h3>{translateUI("执行授权")}</h3>
          <span className="tr-badge">{translateUI(detail.authorization.mode === "e2ag" ? "E2AG" : "受信本地配置")}</span></div>
          {detail.authorization.mode === "e2ag" ? <>
            {active?.runtime_state === "paused" && !terminal(detail.state) && <p className="tr-notice" data-testid="execution-paused">{translateUI("执行已暂停，身份恢复并通过授权复核后继续。")}</p>}
            <div className="tr-metrics"><div><span>{translateUI("工作授权")}</span><strong>{translateUI(AUTH_STATES[detail.authorization.state || ""] || "待确认")}</strong></div>
              <div><span>{translateUI("策略")}</span><strong>{translateUI(detail.authorization.policy_id)}</strong></div><div><span>{translateUI("工作期限")}</span><strong>{translateUI(time(detail.authorization.expires_at))}</strong></div></div>
            {detail.authorization.grant && <p className="tr-muted">{translateUI("执行授权版本")}{translateUI(detail.authorization.grant.version)}{translateUI("· 到期")}{translateUI(time(detail.authorization.grant.expires_at))} · {translateUI(detail.authorization.grant.session_id ? "已绑定会话" : "等待会话绑定")}</p>}
            {detail.authorization.state === "revoked" && !terminal(detail.state) && <p className="tr-notice">{translateUI("工作授权已撤销，等待执行端退出确认。")}</p>}
            {detail.authorization.source_check && <p className="tr-notice" data-testid="source-check">
              {translateUI("身份对账：")}{translateUI(({pending:"等待检查",valid:"来源有效",unavailable:"身份服务暂不可用",revoked:"来源已失效"})[detail.authorization.source_check.outcome])}
              {detail.authorization.source_check.checking && <> · {translateUI("检查中")}</>}
              {detail.authorization.source_check.stale && !terminal(detail.state) && detail.authorization.state === "active" && <> · {translateUI("观测过期")}</>}
              <> · {translateUI("最近检查：")}{translateUI(time(detail.authorization.source_check.checked_at))}</>
              {!terminal(detail.state) && detail.authorization.state === "active" && <> · {translateUI("下次检查：")}{translateUI(time(detail.authorization.source_check.next_check_at))}</>}
              {detail.authorization.source_check.outcome === "unavailable" && !terminal(detail.state) && detail.authorization.state === "active" && <> · {translateUI("保留原任务与审批，等待自动复查。")}</>}
            </p>}
            {!!detail.authorization.secret_refs?.length && <p className="tr-muted">{translateUI("凭据引用：")}{translateUI(detail.authorization.secret_refs.map(s => `${s.ref} / ${s.version}`).join("、"))}</p>}
            <details className="tr-auth-decisions"><summary>{translateUI("授权记录")}</summary><ol className="tr-timeline">
              {detail.authorization.decisions?.map(d => <li key={d.id}><time>{translateUI(time(d.created_at))}</time><span>{translateUI(d.outcome === "allow" ? "允许" : "拒绝")}</span><code>{d.reason_codes.join("、")}</code></li>)}
            </ol></details>
            {can("run.revoke") && !terminal(detail.state) && detail.authorization.state === "active" && <div className="tr-actions"><button disabled={busy || !!detailError} onClick={() => void revoke()}>{translateUI("撤销工作授权")}</button></div>}
          </> : <p className="tr-muted">{translateUI("此任务沿用受信操作员配置。企业工作授权需使用已配置 E2AG 的执行入口。")}</p>}
        </section>
        {can("tool.read") && <ToolActions key={detail.id} runId={detail.id} canApprove={can("tool.approve")} operator={operatorMode} active={!terminal(detail.state) && detail.authorization.state === "active" && !detail.cancel_requested} />}
        <section className="tr-card"><div className="tr-section-title"><h3>{translateUI("最近输出")}</h3><span className="tr-muted">{translateUI("保留最近片段")}</span></div>
          <pre className="tr-output">{active?.output_excerpt || "等待 Agent 的有效输出。"}</pre></section>
        <section className="tr-card"><div className="tr-section-title"><h3>{translateUI("成果与附件")}</h3>{detail.state === "succeeded" && <span className="tr-badge">{translateUI("待人工复核")}</span>}</div>
          {!detail.artifacts.length && <p className="tr-empty-small">{translateUI("执行结束后，报告和原始输出将在这里归档。")}</p>}
          <div className="tr-artifacts">{detail.artifacts.map(a => <button key={a.id} className={artifact === a.id ? "selected" : ""} disabled={!can("artifact.read")} onClick={() => setArtifact(a.id)}>
            {translateUI(a.kind === "result" ? "任务成果" : a.kind === "stdout" ? "原始输出" : a.kind === "stderr" ? "诊断记录" : "附件")}<small>{translateUI((a.size / 1024).toFixed(1))} KB</small></button>)}</div>
          {previewError && <p className="tr-error" role="alert">{translateUI(previewError)}</p>}
          {artifact && <div className="tr-actions"><button disabled={downloading} onClick={() => void download(false)}>{translateUI("下载原文件")}</button>
            {preview?.markdown && <button disabled={downloading} onClick={() => void download(true)}>{translateUI("下载 Markdown 报告")}</button>}</div>}
          {preview?.markdown ? <article className="tr-report"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
            components={{ img: ({ alt }) => <span>{alt || "图片"}</span>, a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{preview.markdown}</ReactMarkdown></article>
            : preview && <pre className="tr-output">{preview.text}</pre>}
        </section>
        <details className="tr-card"><summary>{translateUI("执行记录与来源")}</summary>
          <p className="tr-muted">{translateUI("任务")}{translateUI(detail.id)}</p><p className="tr-muted">{translateUI("输入")}{detail.sample.name} · SHA256 {translateUI(detail.sample.sha256)}</p>
          {detail.attempts.map(a => <div className="tr-attempt" key={a.id}><Badge state={a.state} /><span>{translateUI("尝试")}{translateUI(a.epoch)}</span><code>{a.id}</code><small>{translateUI("会话")}{translateUI(a.session_id || "尚未创建")}{translateUI("· 进程")}{translateUI(a.process_id || "未知")}</small></div>)}
          {detail.attempts_has_more && <p className="tr-muted">{translateUI("此处显示最近 20 次尝试。")}</p>}
          <ol className="tr-timeline">{detail.events.map(e => <li key={e.id}><time>{translateUI(time(e.created_at))}</time><span>{translateUI(LABELS[e.state] || e.state)}</span><code>{e.type}</code></li>)}</ol>
        </details>
      </>}
    </main></div>
  </section>;
}
