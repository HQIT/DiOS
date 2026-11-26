import {useEffect, useRef, useState} from "react";
import {deploymentApi, RuntimeApiError, technicalApi} from "../../../api/runtime";
import type {DeploymentView, TechnicalRecord} from "../../../api/runtime";
import {usePreferences} from "../../../lib/preferences";
import "./configuration-studio.css";

const json = (value: unknown) => JSON.stringify(value, null, 2);
const text = (value: unknown) => typeof value === "string" ? value : "";

export default function TechnicalReleases() {
  const {locale} = usePreferences(); const l = (zh: string, en: string) => locale === "zh" ? zh : en;
  const [kind, setKind] = useState("evaluation"), [after, setAfter] = useState("");
  const [rows, setRows] = useState<TechnicalRecord[]>([]), [next, setNext] = useState<string | null>(null);
  const [current, setCurrent] = useState<TechnicalRecord | null>(null);
  const [suites, setSuites] = useState<TechnicalRecord[]>([]), [suiteId, setSuiteId] = useState("");
  const [revision, setRevision] = useState(""); const [baseline, setBaseline] = useState("");
  const [comparison, setComparison] = useState<Record<string, unknown> | null>(null);
  const [editorKind, setEditorKind] = useState<"suites" | "environments">("suites");
  const [editorId, setEditorId] = useState(""), [editorVersion, setEditorVersion] = useState(0), [document, setDocument] = useState("{}");
  const [bindingId, setBindingId] = useState(""), [environment, setEnvironment] = useState(""), [definition, setDefinition] = useState("");
  const [deployment, setDeployment] = useState(""), [version, setVersion] = useState(0), [source, setSource] = useState("");
  const [evaluation, setEvaluation] = useState(""), [external, setExternal] = useState(""); const [rollback, setRollback] = useState(false);
  const [token, setToken] = useState(""), [reason, setReason] = useState("");
  const [deployed, setDeployed] = useState<DeploymentView | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [stale, setStale] = useState(false), [refresh, setRefresh] = useState(0);
  const keys = useRef(new Map<string, string>()); const seq = useRef(0);
  useEffect(() => () => {seq.current++;}, []);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([technicalApi.list(kind, after, controller.signal), technicalApi.list("suite", "", controller.signal)]).then(([page, registered]) => {
      if (!controller.signal.aborted) {setRows(page.items); setNext(page.next_after); setSuites(registered.items);}
    }).catch(err => {if (!controller.signal.aborted) setError(err instanceof RuntimeApiError ? err.code : "TECHNICAL_LIST_UNAVAILABLE");});
    return () => controller.abort();
  }, [kind, after, refresh]);
  const currentId = current?.id, currentKind = current?.kind, currentState = text(current?.document.state);
  const deploymentId = currentKind === "promotion" && currentState === "published" ? text(current?.document.deployment_id) : "";
  useEffect(() => {
    if (!currentId || (currentKind !== "evaluation" && !deploymentId)) return;
    if (currentKind === "evaluation" && ["passed", "failed"].includes(currentState)) return;
    let stopped = false; let timer: ReturnType<typeof setTimeout>; const controller = new AbortController();
    async function poll() {
      try {
        if (deploymentId) {const value = await deploymentApi.detail(deploymentId, controller.signal); if (!stopped) setDeployed(value);}
        else {const value = await technicalApi.detail("evaluation", currentId!); if (!stopped) setCurrent(value);}
        if (!stopped) setStale(false);
      } catch {if (!stopped) setStale(true);}
      if (!stopped) timer = setTimeout(poll, 3000);
    }
    void poll(); return () => {stopped = true; controller.abort(); clearTimeout(timer);};
  }, [currentId, currentKind, currentState, deploymentId]);
  function key(input: unknown) {const identity = json(input); let value = keys.current.get(identity); if (!value) {value = crypto.randomUUID(); keys.current.set(identity, value);} return value;}
  async function act(fn: () => Promise<TechnicalRecord>) {
    const operation = ++seq.current; setBusy(true); setError("");
    try {const value = await fn(); if (seq.current === operation) {setCurrent(value); setDeployed(null); setToken(""); setRefresh(x => x + 1); if (value.kind === "evaluation") setEvaluation(value.id);}}
    catch (err) {if (seq.current === operation) setError(err instanceof RuntimeApiError ? err.code : "TECHNICAL_REQUEST_FAILED");}
    finally {if (seq.current === operation) setBusy(false);}
  }
  async function save() {try {const value: Record<string, unknown> = JSON.parse(document); await act(() => technicalApi.put(editorKind, editorId, value, editorVersion));} catch {setError("INVALID_JSON");}}
  async function evaluate() {
    const suite = suites.find(x => x.id === suiteId); if (!suite?.ref) return;
    await act(() => technicalApi.evaluate(revision, suite.ref!, key([revision, suite.ref])));
  }
  async function promote() {
    try {const body = {deployment_id: deployment, evaluation_id: evaluation, deployment_version: version, source_deployment: source || null, external_approval: external.trim() ? JSON.parse(external) : null, rollback}; await act(() => technicalApi.promote({...body, idempotency_key: key(body)}));}
    catch {setError("INVALID_EXTERNAL_APPROVAL_REFERENCE");}
  }
  async function compare() {
    setBusy(true); setError("");
    try {setComparison(await technicalApi.compare(baseline, evaluation));}
    catch (err) {setError(err instanceof RuntimeApiError ? err.code : "TECHNICAL_REQUEST_FAILED");}
    finally {setBusy(false);}
  }
  const selectedState = text(current?.document.state);
  return <section className="config-studio studio-release technical-releases">
    <header><div><h2>{l("技术评测与环境晋级", "Technical evaluation and promotion")}</h2><p>{l("固定评测条件与版本，通过技术门禁和独立审批后进入目标环境。", "Pin evaluation conditions and revisions, then promote through technical gates and independent approval.")}</p></div></header>
    {error && <p role="alert" className="studio-error"><code>{error}</code></p>}
    <section className="studio-assistant"><h3>{l("执行版本评测", "Evaluate a revision")}</h3><div className="studio-toolbar">
      <label>Revision<input value={revision} onChange={e => setRevision(e.target.value)} disabled={busy} /></label>
      <label>{l("评测模板", "Evaluation suite")}<select value={suiteId} onChange={e => setSuiteId(e.target.value)} disabled={busy}><option value="">{l("选择模板", "Select suite")}</option>{suites.map(x => <option key={x.id} value={x.id}>{x.id} · v{x.version}</option>)}</select></label>
      <button disabled={busy || !revision || !suiteId} onClick={() => void evaluate()}>{l("启动评测", "Start evaluation")}</button>
    </div><p>{l("每个样例进入持久队列，由独立 Worker 执行，结果与授权证据保留。", "Each case enters the durable queue for a Worker; results and authorization evidence are retained.")}</p></section>
    <section className="studio-assistant"><h3>{l("晋级与回滚申请", "Promotion and rollback request")}</h3><div className="studio-toolbar">
      <label>{l("目标部署", "Target deployment")}<input value={deployment} onChange={e => setDeployment(e.target.value)} disabled={busy} /></label>
      <label>{l("预期部署版本", "Expected deployment version")}<input type="number" min={0} value={version} onChange={e => setVersion(Number(e.target.value))} disabled={busy} /></label>
      <label>{l("来源部署", "Source deployment")}<input value={source} onChange={e => setSource(e.target.value)} disabled={busy} /></label>
      <label>{l("评测记录 ID", "Evaluation ID")}<input value={evaluation} onChange={e => setEvaluation(e.target.value)} disabled={busy} /></label>
    </div><label>{l("外部业务审批引用（URI、版本、摘要）", "External approval reference (URI, version, digest)")}<textarea value={external} onChange={e => setExternal(e.target.value)} placeholder='{"uri":"https://…","version":"1","digest":"sha256:…"}' disabled={busy} /></label>
      <label><span><input type="checkbox" checked={rollback} onChange={e => setRollback(e.target.checked)} disabled={busy} /> {l("回滚到上一已知版本", "Roll back to the previous known revision")}</span></label>
      <button disabled={busy || !deployment || !evaluation} onClick={() => void promote()}>{l("申请晋级或回滚", "Request promotion or rollback")}</button>
    </section>
    <section className="studio-assistant"><h3>{l("证据与记录", "Evidence and records")}</h3><div className="studio-toolbar"><label>{l("记录类型", "Record type")}<select value={kind} onChange={e => {setKind(e.target.value); setAfter("");}} disabled={busy}>{[["evaluation",l("评测", "Evaluations")],["promotion",l("晋级", "Promotions")],["suite",l("评测模板", "Suites")],["environment",l("环境", "Environments")],["binding",l("部署绑定", "Bindings")]].map(([k,v]) => <option value={k} key={k}>{v}</option>)}</select></label><button disabled={busy} onClick={() => setRefresh(x => x + 1)}>{l("刷新记录", "Refresh records")}</button><button disabled={busy || !next} onClick={() => setAfter(next!)}>{l("下一页", "Next page")}</button><button disabled={busy || !after} onClick={() => setAfter("")}>{l("首页", "First page")}</button></div>
      {!rows.length && <p>{l("当前没有记录。", "No records on this page.")}</p>}
      <div className="technical-records">{rows.map(row => <button key={row.id} disabled={busy} onClick={() => void act(() => technicalApi.detail(row.kind, row.id))}><code>{row.id}</code> · v{row.version} · {text(row.document.state)}</button>)}</div>
      {current && <article className="studio-release-detail"><h4>{current.id}</h4><p>{l("状态", "State")}: {selectedState || current.kind} · v{current.version}</p>
        {current.kind === "evaluation" && <div className="studio-toolbar"><button disabled={busy} onClick={() => void act(() => technicalApi.reconcile(current.id))}>{l("核验评测结果", "Verify evaluation results")}</button><button onClick={() => setBaseline(current.id)}>{l("设为比较基准", "Use as baseline")}</button></div>}
        {current.kind === "promotion" && selectedState === "pending" && <div className="studio-toolbar"><label>{l("审批凭据", "Approver credential")}<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} disabled={busy} /></label><label>{l("审批意见", "Review comment")}<input value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} disabled={busy} /></label><button disabled={busy || !token} onClick={() => void act(() => technicalApi.decide(current, "approved", token, reason))}>{l("批准晋级", "Approve promotion")}</button><button disabled={busy || !token} onClick={() => void act(() => technicalApi.decide(current, "denied", token, reason))}>{l("拒绝晋级", "Deny promotion")}</button></div>}
        {current.kind === "promotion" && selectedState === "approved" && <button disabled={busy} onClick={() => void act(() => technicalApi.publish(current))}>{l("执行已批准的晋级", "Execute approved promotion")}</button>}
        {deployed && <p>{l("目标部署状态", "Target deployment state")}: {deployed.deployment.state} · <code>{deployed.status.error || deployed.status.active_revision_id || "—"}</code></p>}
        {stale && <p role="status">{l("观测过期，正在重连。", "Observation stale; reconnecting.")}</p>}
        <details open><summary>{l("固定配置与原始证据", "Pinned configuration and evidence")}</summary><pre>{json(current.document)}</pre></details>
        {(current.kind === "suite" || current.kind === "environment") && <button disabled={busy} onClick={() => {setEditorKind(current.kind === "suite" ? "suites" : "environments"); setEditorId(current.id); setEditorVersion(current.version); setDocument(json(current.document));}}>{l("载入配置修订", "Load for revision")}</button>}
      </article>}
    </section>
    <section className="studio-assistant"><h3>{l("比较相同条件下的版本", "Compare revisions under the same conditions")}</h3><div className="studio-toolbar"><label>{l("基准评测 ID", "Baseline evaluation ID")}<input value={baseline} onChange={e => setBaseline(e.target.value)} disabled={busy} /></label><label>{l("候选评测 ID", "Candidate evaluation ID")}<input value={evaluation} onChange={e => setEvaluation(e.target.value)} disabled={busy} /></label><button disabled={busy || !baseline || !evaluation} onClick={() => void compare()}>{l("比较评测", "Compare evaluations")}</button></div>{comparison && <pre>{json(comparison)}</pre>}</section>
    <details className="studio-assistant"><summary>{l("管理评测模板、环境和绑定", "Manage suites, environments and bindings")}</summary><p>{l("配置采用显式版本；环境绑定固定到部署，发布与激活均执行门禁。", "Configuration is explicitly versioned. Deployment bindings enforce gates at publication and activation.")}</p><div className="studio-toolbar"><label>{l("配置类型", "Configuration type")}<select value={editorKind} onChange={e => setEditorKind(e.target.value as "suites" | "environments")} disabled={busy}><option value="suites">{l("评测模板", "Suite")}</option><option value="environments">{l("环境", "Environment")}</option></select></label><label>{l("配置 ID", "Configuration ID")}<input value={editorId} onChange={e => setEditorId(e.target.value)} disabled={busy} /></label><label>{l("预期配置版本", "Expected configuration version")}<input type="number" value={editorVersion} min={0} onChange={e => setEditorVersion(Number(e.target.value))} disabled={busy} /></label></div><label>JSON<textarea value={document} onChange={e => setDocument(e.target.value)} spellCheck={false} disabled={busy} /></label><button disabled={busy || !editorId} onClick={() => void save()}>{l("保存配置版本", "Save configuration version")}</button>
      <div className="studio-toolbar"><label>{l("绑定部署", "Bind deployment")}<input value={bindingId} onChange={e => setBindingId(e.target.value)} disabled={busy} /></label><label>{l("环境 ID", "Environment ID")}<input value={environment} onChange={e => setEnvironment(e.target.value)} disabled={busy} /></label><label>{l("能力定义 ID", "Definition ID")}<input value={definition} onChange={e => setDefinition(e.target.value)} disabled={busy} /></label><button disabled={busy || !bindingId || !environment || !definition} onClick={() => void act(() => technicalApi.bind(bindingId, environment, definition))}>{l("绑定环境", "Bind environment")}</button></div>
    </details>
  </section>;
}
