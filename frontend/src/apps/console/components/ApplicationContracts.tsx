import {useEffect, useState} from "react";
import {applicationApi, RuntimeApiError} from "../../../api/runtime";
import type {ApplicationCompatibility, ApplicationProfile} from "../../../api/runtime";
import {usePreferences} from "../../../lib/preferences";
import "./configuration-studio.css";

export default function ApplicationContracts() {
  const {locale} = usePreferences(); const l = (zh: string, en: string) => locale === "zh" ? zh : en;
  const [rows, setRows] = useState<ApplicationProfile[]>([]), [platform, setPlatform] = useState<Record<string, string>>({});
  const [refresh, setRefresh] = useState(0), [error, setError] = useState("");
  const [revision, setRevision] = useState(""), [draft, setDraft] = useState("");
  const [report, setReport] = useState<ApplicationCompatibility | null>(null), [busy, setBusy] = useState(false);
  const [query, setQuery] = useState<{revision: string; contract?: Record<string, unknown>} | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([applicationApi.list(controller.signal), applicationApi.platform(controller.signal)]).then(([page, contract]) => {
      if (!controller.signal.aborted) {setRows(page.items); setPlatform(contract.interfaces); setError("");}
    }).catch(err => {if (!controller.signal.aborted) setError(err instanceof RuntimeApiError ? err.code : "APPLICATION_CATALOG_UNAVAILABLE");});
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => {
    if (!query) return;
    const controller = new AbortController();
    applicationApi.check(query.revision, query.contract, controller.signal).then(value => {if (!controller.signal.aborted) setReport(value);})
      .catch(err => {if (!controller.signal.aborted) setError(err instanceof RuntimeApiError ? err.code : "APPLICATION_CHECK_UNAVAILABLE");})
      .finally(() => {if (!controller.signal.aborted) setBusy(false);});
    return () => controller.abort();
  }, [query]);
  function check(ref: string, source = "") {
    try {
      const contract = source.trim() ? JSON.parse(source) : undefined;
      if (contract && (typeof contract !== "object" || Array.isArray(contract))) throw new Error();
      setBusy(true); setError(""); setReport(null); setQuery({revision: ref, contract});
    } catch {setError("INVALID_APPLICATION_CONTRACT");}
  }
  return <section className="config-studio application-contracts">
    <header><div><h2>{l("独立应用接入", "Independent applications")}</h2><p>{l("应用声明所需接口与输入、结果契约；DiOS 执行通用兼容校验和治理。", "Applications declare interface, input and result requirements; DiOS validates compatibility and governance.")}</p></div><button onClick={() => setRefresh(x => x + 1)}>{l("刷新登记", "Refresh registrations")}</button></header>
    {error && <p role="alert" className="studio-error">{error}</p>}
    <section className="studio-assistant"><h3>{l("平台接口版本", "Platform interfaces")}</h3><div className="studio-toolbar">{Object.entries(platform).map(([name, version]) => <span key={name}><code>{name}</code> · {version}</span>)}</div></section>
    <section className="studio-assistant"><h3>{l("已启用的应用登记", "Enabled application registrations")}</h3>{!rows.length && <p>{l("当前没有声明应用契约的启用版本。", "No enabled revision declares an application contract.")}</p>}
      {rows.map(row => <article className="studio-release-detail" key={row.id}><h4>{row.application.id} · {row.application.version}</h4><p>{row.name} · {row.id} · {row.governance}</p><p>{l("输入类型", "Input kind")}: <code>{row.input_kind}</code></p><p>Revision: <code>{row.revision_ref}</code></p><p>{l("部署", "Deployment")}: {row.deployment_id || "—"} · {row.deployment_state || "—"}</p><button disabled={busy} onClick={() => {setRevision(row.revision_ref); setDraft(""); check(row.revision_ref);}}>{l("检查登记兼容性", "Check registered compatibility")}</button></article>)}
    </section>
    <section className="studio-assistant"><h3>{l("兼容检查", "Compatibility check")}</h3><p>{l("检查固定 Revision；填写候选契约可预览差异。正式启用沿用受控版本发布。", "Inspect a pinned revision; supply a candidate contract to preview differences. Activation uses governed revision release.")}</p>
      <label>Revision<input value={revision} disabled={busy} onChange={e => {setRevision(e.target.value); setReport(null);}} /></label><label>{l("候选应用契约 JSON（可选）", "Candidate contract JSON (optional)")}<textarea value={draft} onChange={e => {setDraft(e.target.value); setReport(null);}} spellCheck={false} disabled={busy} /></label><button disabled={busy || !revision} onClick={() => check(revision, draft)}>{busy ? l("检查中…", "Checking…") : l("检查兼容性", "Check compatibility")}</button>
      {report && <article className="studio-release-detail"><h4>{report.compatible ? l("契约兼容", "Contract compatible") : l("契约不兼容", "Contract incompatible")}</h4><p>{report.registered_contract ? l("已固定到 Revision", "Pinned to revision") : l("候选契约预览", "Candidate contract preview")} · <code>{report.revision_ref}</code></p>
        {report.issues.map((issue, index) => <p key={index}><code>{issue.code}</code> {issue.interface} {issue.required} {issue.available && `→ ${issue.available}`}</p>)}
        <details><summary>{l("契约与能力详情", "Contract and capability details")}</summary><pre>{JSON.stringify(report, null, 2)}</pre></details>
        <button onClick={() => setDraft(JSON.stringify(report.application, null, 2))}>{l("载入候选契约", "Load candidate contract")}</button>
      </article>}
    </section>
  </section>;
}
