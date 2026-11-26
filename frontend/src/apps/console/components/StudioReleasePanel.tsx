import {useEffect, useRef, useState} from "react";
import {deploymentApi, RuntimeApiError, studioReleaseApi} from "../../../api/runtime";
import type {DeploymentView, StudioRelease, StudioReleaseRequest, StudioProbe} from "../../../api/runtime";
import {usePreferences} from "../../../lib/preferences";

export default function StudioReleasePanel({base, draft, restore}: {base: string; draft: string; restore: (base: string, draft: string) => void}) {
  const {locale} = usePreferences();
  const l = (zh: string, en: string) => locale === "zh" ? zh : en;
  const [deployment, setDeployment] = useState("");
  const [version, setVersion] = useState(0);
  const [sample, setSample] = useState("");
  const [payload, setPayload] = useState("");
  const [release, setRelease] = useState<StudioRelease | null>(null);
  const [items, setItems] = useState<Array<{id: string; state: string; deployment_id: string}>>([]);
  const [after, setAfter] = useState("");
  const [selected, setSelected] = useState("");
  const [deployed, setDeployed] = useState<DeploymentView | null>(null);
  const [probe, setProbe] = useState<StudioProbe | null>(null);
  const [token, setToken] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const key = useRef({input: "", key: ""});
  const op = useRef(0);
  useEffect(() => () => {op.current++;}, []);
  useEffect(() => {
    const controller = new AbortController();
    studioReleaseApi.list(after, controller.signal).then(value => {if (!controller.signal.aborted) setItems(value.items);})
      .catch(() => {if (!controller.signal.aborted) setError("STUDIO_RELEASE_LIST_UNAVAILABLE");});
    return () => controller.abort();
  }, [after]);
  const publishedId = release?.state === "published" ? release.detail.deployment_id : null;
  const releaseId = release?.state === "published" ? release.id : null;
  useEffect(() => {
    if (!publishedId || !releaseId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {const [value, execution] = await Promise.all([deploymentApi.detail(publishedId!, controller.signal), studioReleaseApi.probe(releaseId!, controller.signal)]); if (!controller.signal.aborted) {setDeployed(value); setProbe(execution); setStale(false);}}
      catch {if (!controller.signal.aborted) setStale(true);}
      if (!controller.signal.aborted) timer = setTimeout(poll, 3000);
    }
    void poll(); return () => {controller.abort(); clearTimeout(timer);};
  }, [publishedId, releaseId]);
  async function action(fn: () => Promise<StudioRelease>) {
    const seq = ++op.current; setBusy(true); setError("");
    try {const value = await fn(); if (seq === op.current) {setRelease(value); setSelected(value.id); setDeployed(null); setProbe(null); setToken(""); setDeployment(value.snapshot.deployment_id); setVersion(value.snapshot.deployment_version); setItems(rows => rows.map(row => row.id === value.id ? {...row, state: value.state} : row));}}
    catch (err) {if (seq === op.current) setError(err instanceof RuntimeApiError ? err.code : "STUDIO_REQUEST_FAILED");}
    finally {if (seq === op.current) setBusy(false);}
  }
  async function prepare() {
    let input: Record<string, unknown> | undefined;
    try {if (payload.trim()) input = JSON.parse(payload);} catch {setError("INVALID_WORK_INPUT"); return;}
    const body = {base, draft, deployment_id: deployment, deployment_version: version, ...(input ? {input_payload: input} : {sample_id: sample})};
    const identity = JSON.stringify(body);
    if (identity !== key.current.input) key.current = {input: identity, key: crypto.randomUUID()};
    await action(() => studioReleaseApi.prepare({...body, idempotency_key: key.current.key} as StudioReleaseRequest));
  }
  const changed = !!release && release.snapshot.draft !== draft;
  const guidance: Record<string, string> = {
    STUDIO_INLINE_INPUT_CATALOG_REQUIRED: l("载入现有配置副本以固化输入契约，或提供 console.input_catalog_data。", "Load an existing profile copy to freeze its input contract, or supply console.input_catalog_data."),
    STUDIO_RESULT_SCHEMA_REQUIRED: l("为健康检查设置结果 Schema，成功结果需通过证据校验。", "Set a result schema for the health check so successful output can be validated."),
    REVISION_IMAGE_NOT_PINNED: l("请使用固定的 sha256 镜像标识；本地构建可通过现有 Revision CLI 完成。", "Use a pinned sha256 image ID. Local builds use the existing Revision CLI."),
    REVISION_MOUNTS_NOT_PINNED: l("先将受控文件打包到固定镜像；运行时输入挂载需明确登记。", "Package controlled files into a pinned image and explicitly register runtime input mounts."),
    WORK_PROFILE_CHANGED: l("当前配置摘要尚未获得 E2AG 授权，请由管理员审核并更新授权。", "This configuration digest needs E2AG admission by an administrator."),
    WORK_PROFILE_NOT_GRANTED: l("该能力尚未获得 E2AG 授权，请由管理员审核。", "This capability needs E2AG admission by an administrator."),
    STUDIO_POLICY_CHANGED: l("授权策略已变化，请重新提交申请并审批。", "The authorization policy changed. Submit a new request for approval."),
    VERSION_CONFLICT: l("部署或申请版本已变化，请刷新并审阅最新状态。", "The deployment or request version changed. Refresh and review its current state."),
  };
  return <section className="studio-assistant studio-release">
    <h3>{l("审批与发布", "Review and release")}</h3>
    <p>{l("提交固定配置与健康检查输入，审批通过后发布。新版本健康检查通过、旧任务结束后生效。", "Submit a fixed configuration and health-check input for approval. Activation follows a successful health check and completion of old tasks.")}</p>
    <div className="studio-toolbar">
      <label>{l("部署名称", "Deployment name")}<input value={deployment} onChange={e => setDeployment(e.target.value)} disabled={busy} /></label>
      <label>{l("当前部署版本（新建填 0）", "Current deployment version (0 for new)")}<input type="number" min={0} value={version} onChange={e => setVersion(Number(e.target.value))} disabled={busy} /></label>
      <button disabled={busy || !deployment} onClick={() => {setError(""); void deploymentApi.detail(deployment).then(value => setVersion(value.deployment.version)).catch(err => setError(err instanceof RuntimeApiError ? err.code : "REQUEST_FAILED"));}}>{l("读取部署版本", "Read deployment version")}</button>
      <label>{l("健康检查样例 ID", "Health-check sample ID")}<input value={sample} onChange={e => setSample(e.target.value)} disabled={busy || !!payload.trim()} /></label>
    </div>
    <details><summary>{l("使用动态健康检查输入", "Use dynamic health-check input")}</summary><label>JSON<textarea value={payload} onChange={e => setPayload(e.target.value)} disabled={busy} spellCheck={false} /></label></details>
    <button disabled={busy || !deployment || (!sample && !payload.trim())} onClick={() => void prepare()}>{l("提交发布审批", "Request release approval")}</button>
    <div className="studio-toolbar"><label>{l("已保存的发布申请", "Saved release requests")}<select value={selected} onChange={e => setSelected(e.target.value)} disabled={busy}><option value="">{l("选择申请", "Select request")}</option>{release && !items.some(x => x.id === release.id) && <option value={release.id}>{release.snapshot.deployment_id} · {release.state}</option>}{items.map(x => <option key={x.id} value={x.id}>{x.deployment_id} · {x.state} · {x.id.slice(-8)}</option>)}</select></label><button disabled={busy || !selected} onClick={() => void action(() => studioReleaseApi.detail(selected))}>{l("打开申请", "Open request")}</button><button disabled={busy || items.length < 30} onClick={() => setAfter(items[items.length - 1].id)}>{l("下一页", "Next page")}</button><button disabled={busy || !after} onClick={() => setAfter("")}>{l("首页", "First page")}</button></div>
    {error && <p role="alert" className="studio-error"><code>{error}</code>{guidance[error] && ` · ${guidance[error]}`}</p>}
    {release && <div className="studio-release-detail" aria-live="polite">
      <p><strong>{l("申请状态", "Request status")}: {release.state}</strong> · {release.snapshot.deployment_id} · v{release.version}</p>
      <p><code>{release.id}</code></p>
      <p>{l("配置摘要", "Configuration digest")}: <code>{release.snapshot.review.draft_digest}</code></p>
      <p>{l("审批有效期至", "Approval valid until")}: {new Date(release.snapshot.expires_at * 1000).toLocaleString()}</p>
      {changed && <p>{l("编辑区与申请内容不同。发布使用以下申请快照；新改动须另行提交审批。", "The editor differs from this request. Publication uses the snapshot below; new changes need a new review.")}</p>}
      <details><summary>{l("审阅申请配置与差异", "Review request configuration and diff")}</summary><pre>{JSON.stringify(release.snapshot.review.patch, null, 2)}</pre><pre>{release.snapshot.draft}</pre><h4>{l("健康检查输入", "Health-check input")}</h4><pre>{JSON.stringify(release.snapshot.probe.payload.sample, null, 2)}</pre></details>
      <button disabled={busy} onClick={() => {if (window.confirm(l("用申请快照替换编辑区？", "Replace the editor with this request snapshot?"))) restore(release.snapshot.base, release.snapshot.draft);}}>{l("载入申请草案", "Load request draft")}</button>
      {release.detail.reason && <p>{release.detail.reason}</p>}{release.detail.error && <p className="studio-error">{release.detail.error}</p>}
      {release.state === "pending" && <div className="studio-toolbar">
        <label>{l("审批凭据", "Approver credential")}<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} disabled={busy} /></label>
        <label>{l("审批意见", "Review comment")}<input maxLength={2000} value={reason} onChange={e => setReason(e.target.value)} disabled={busy} /></label>
        <button disabled={busy || !token} onClick={() => void action(() => studioReleaseApi.decide(release, "approved", token, reason))}>{l("批准发布", "Approve release")}</button>
        <button disabled={busy || !token} onClick={() => void action(() => studioReleaseApi.decide(release, "denied", token, reason))}>{l("拒绝发布", "Deny release")}</button>
      </div>}
      {release.state === "approved" && <button className="btn-primary" disabled={busy || changed} onClick={() => void action(() => studioReleaseApi.publish(release))}>{l("发布已批准的配置", "Publish approved configuration")}</button>}
      {release.detail.revision_ref && <p>Revision: <code>{release.detail.revision_ref}</code></p>}
      {deployed && <p>{l("部署状态", "Deployment status")}: {deployed.deployment.state} · {l("健康任务", "Health task")}: {probe?.state} {deployed.status.error && <code>{deployed.status.error}</code>}</p>}
      {deployed && <p>{l("当前生效版本", "Active revision")}: <code>{deployed.status.active_revision_id || "—"}</code></p>}
      {probe && <details><summary>{l("健康任务与授权证据", "Health task and authorization evidence")} · {probe.state}</summary><p>Run: <code>{probe.run_id}</code></p>{probe.attempts.map(item => <p key={item.id}>Attempt: <code>{item.id}</code> · {item.state} · {item.error?.code || item.session_id || "—"}</p>)}<pre>{JSON.stringify(probe.authorization, null, 2)}</pre></details>}
      {stale && <p role="status">{l("部署观测过期，正在重连。", "Deployment observation is stale; reconnecting.")}</p>}
    </div>}
  </section>;
}
