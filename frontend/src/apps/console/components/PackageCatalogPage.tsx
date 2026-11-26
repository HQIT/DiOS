import { useEffect, useState } from "react";
import { packageApi, RuntimeApiError } from "../../../api/runtime";
import type { PackageEntry, PackageFilters, PackagePage, PackageReleases } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";
import "./package-catalog.css";

const initial: PackageFilters = {q: "", kind: "", version: "", admission: ""};
const decisions: Record<string, string> = {pending: "待准入", admitted: "已批准", rejected: "已拒绝", revoked: "已撤销"};
const restrictions: Record<string, string> = {
  E2AG_REQUIRED: "执行时仍需 E2AG 授权",
  DEPENDENCY_CLOSURE_REVIEW_REQUIRED: "发布时核验完整依赖与当前批准版本",
  PACKAGE_DECLARATION_REQUIRED: "缺少来源、许可或能力声明",
  PACKAGE_NOT_ADMITTED: "当前未获准发布",
  PACKAGE_EVIDENCE_UNAVAILABLE: "批准证据已失效，需要重新复核",
};
const reason = (error: unknown) => error instanceof RuntimeApiError ? error.code : "RUNTIME_UNAVAILABLE";
const stamp = (value: number) => new Date(value * 1000).toLocaleString(languageTag(), {hour12: false});
const route = (digest: string) => {window.location.hash = `console/registry/packages${digest ? `/${digest}` : ""}`;};

function Status({item}: {item: PackageEntry}) {
  return <span className={`package-status ${item.admission.state}`}>
    {t(decisions[item.admission.state] || item.admission.state)}
    {item.admission.evidence_valid === false && ` · ${t("证据失效")}`}
  </span>;
}

export default function PackageCatalogPage({selected}: {selected: string}) {
  const [draft, setDraft] = useState(initial);
  const [filters, setFilters] = useState(initial);
  const [after, setAfter] = useState("");
  const [listResult, setListResult] = useState<{key: string; page?: PackagePage; error?: string; updated?: number}>();
  const [detailResult, setDetailResult] = useState<{key: string; item?: PackageEntry; links?: PackageReleases; error?: string}>();
  const [releasePage, setReleasePage] = useState({selected: "", after: ""});
  const releaseAfter = releasePage.selected === selected ? releasePage.after : "";
  const setReleaseAfter = (value: string) => setReleasePage({selected, after: value});
  const [refresh, setRefresh] = useState(0);
  const listKey = JSON.stringify([filters, after, refresh]);
  const detailKey = JSON.stringify([selected, releaseAfter, refresh]);
  const page = listResult?.key === listKey ? listResult.page : undefined;
  const error = listResult?.key === listKey ? listResult.error : undefined;
  const updated = listResult?.key === listKey ? listResult.updated : undefined;
  const detail = detailResult?.key === detailKey ? detailResult.item : undefined;
  const releases = detailResult?.key === detailKey ? detailResult.links : undefined;
  const detailError = detailResult?.key === detailKey ? detailResult.error : undefined;
  useEffect(() => {
    const controller = new AbortController();
    void packageApi.list(filters, after, AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]))
      .then(value => {if (!controller.signal.aborted) setListResult({key: listKey, page: value, updated: Date.now() / 1000});})
      .catch(e => {if (!controller.signal.aborted) setListResult({key: listKey, error: reason(e)});});
    return () => controller.abort();
  }, [filters, after, listKey]);
  useEffect(() => {
    const controller = new AbortController();
    if (selected) {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]);
      void Promise.all([packageApi.detail(selected, signal), packageApi.releases(selected, releaseAfter, signal)])
        .then(([item, links]) => {if (!controller.signal.aborted) setDetailResult({key: detailKey, item, links});})
        .catch(e => {if (!controller.signal.aborted) setDetailResult({key: detailKey, error: reason(e)});});
    }
    return () => controller.abort();
  }, [selected, releaseAfter, detailKey]);
  return <section className="package-catalog" aria-label={t("受控能力包目录")}>
    <header className="registry-page-header">
      <div><h3>{t("受控能力包")}</h3><p>{t("查看固定版本、依赖与准入证据，再选择发布版本。")}</p></div>
      <button onClick={() => setRefresh(v => v + 1)}>{t("刷新目录")}</button>
    </header>
    <form className="package-filters" onSubmit={event => {event.preventDefault(); setFilters({...draft}); setAfter(""); route("");}}>
      <label>{t("包名称")}<input value={draft.q} maxLength={80} onChange={e => setDraft({...draft, q: e.target.value})} /></label>
      <label>{t("包类型")}<select aria-label={t("包类型")} value={draft.kind} onChange={e => setDraft({...draft, kind: e.target.value})}>
        <option value="">{t("全部类型")}</option>{["harness", "skill", "tool", "prompt", "policy"].map(kind => <option key={kind} value={kind}>{kind}</option>)}
      </select></label>
      <label>{t("包版本")}<input value={draft.version} maxLength={160} onChange={e => setDraft({...draft, version: e.target.value})} /></label>
      <label>{t("准入状态")}<select aria-label={t("准入状态")} value={draft.admission} onChange={e => setDraft({...draft, admission: e.target.value})}>
        <option value="">{t("全部状态")}</option>{Object.entries(decisions).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}
      </select></label>
      <button type="submit">{t("应用筛选")}</button>
    </form>
    {updated && <p className="package-updated">{t("目录更新时间")}: {stamp(updated)}</p>}
    {error && <p role="alert">{t(error === "AUTH_OPERATOR_REQUIRED" ? "此目录需要平台管理员权限。" : "目录加载失败，请重试。")} <code>{error}</code></p>}
    {!page && !error && <p role="status">{t("正在加载目录…")}</p>}
    {page && !page.items.length && <p role="status">{t("当前筛选没有能力包。")}</p>}
    <div className="package-grid">
      {page?.items.map(item => <article className="package-card" key={item.digest}>
        <div className="package-card-heading"><h4>{item.name}</h4><Status item={item} /></div>
        <p><strong>{item.version}</strong> · {item.kind} · {item.declaration?.license || t("许可未声明")}</p>
        <p>{t("直接依赖")}: {item.dependencies.map(d => `${d.name} ${d.specifier || "*"}`).join(", ") || t("无")}</p>
        <code className="package-digest">{item.digest}</code>
        <button onClick={() => route(item.digest)}>{t("查看包详情")}</button>
      </article>)}
    </div>
    <div className="package-actions">
      {after && <button onClick={() => setAfter("")}>{t("返回第一页")}</button>}
      {page?.next_after && <button onClick={() => setAfter(page.next_after!)}>{t("下一页能力包")}</button>}
    </div>
    {selected && <section className="package-detail" aria-label={t("能力包详情")}>
      <div className="package-card-heading"><h3>{t("能力包详情")}</h3><button onClick={() => route("")}>{t("关闭详情")}</button></div>
      {detailError && <p role="alert">{t("包详情加载失败，请重试。")} <code>{detailError}</code></p>}
      {!detail && !detailError && <p role="status">{t("正在加载目录…")}</p>}
      {detail && <>
        <h4>{detail.name} · {detail.version}</h4><Status item={detail} />
        <dl><dt>{t("包摘要")}</dt><dd><code>{detail.digest}</code></dd>
          <dt>{t("来源地址")}</dt><dd>{detail.declaration?.source.uri || "—"}</dd>
          <dt>{t("来源版本")}</dt><dd><code>{detail.declaration?.source.revision || "—"}</code></dd>
          <dt>{t("许可证")}</dt><dd>{detail.declaration?.license || t("许可未声明")}</dd>
          <dt>{t("声明能力")}</dt><dd>{detail.declaration?.capabilities.join(", ") || "—"}</dd>
          <dt>{t("直接依赖")}</dt><dd>{detail.dependencies.map(d => `${d.name} ${d.specifier || "*"}`).join(", ") || t("无")}</dd>
          <dt>{t("批准记录版本")}</dt><dd>{detail.admission.version}</dd>
          <dt>{t("最近复核")}</dt><dd>{detail.admission.reviewed_at ? stamp(detail.admission.reviewed_at) : "—"}</dd>
        </dl>
        <h4>{t("使用条件")}</h4><ul>{detail.limitations.map(code => <li key={code}>{t(restrictions[code] || code)}</li>)}</ul>
        <h4>{t("复核证据")}</h4>
        {!Object.keys(detail.checks || {}).length && <p>{t("尚无复核证据。")}</p>}
        {Object.entries(detail.checks || {}).map(([check, value]) => <p key={check}><strong>{check}</strong> · {t(value.result === "passed" ? "通过" : "未通过")}<br /><code>{value.evidence.kind}:{value.evidence.id}@{value.evidence.version}</code></p>)}
        <h4>{t("包文件")}</h4>{detail.files?.map(file => <p key={file.path}>{file.path} · {file.size} B<br /><code>{file.digest}</code></p>)}
        <h4>{t("关联执行版本")}</h4>
        {releases?.items.map(item => <p key={item.id}>{item.definition_id} · #{item.sequence}<br /><code>{item.id}</code></p>)}
        {!releases?.items.length && <p>{t(releases?.next_after ? "当前页没有关联版本，可继续查询。" : "当前页没有关联执行版本。")}</p>}
        <div className="package-actions">{releaseAfter && <button onClick={() => setReleaseAfter("")}>{t("返回首批关联版本")}</button>}
          {releases?.next_after && <button onClick={() => setReleaseAfter(releases.next_after!)}>{t("继续查询关联版本")}</button>}</div>
      </>}
    </section>}
  </section>;
}
