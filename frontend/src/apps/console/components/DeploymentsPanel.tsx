import { useEffect, useState } from "react";
import { deploymentApi } from "../../../api/runtime";
import type { DeploymentView } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

const states: Record<string, string> = {pending: "候选版本验证中", ready: "版本已激活", draining: "等待存量任务完成", failed: "版本验证失败", stopped: "部署已停止"};
const revision = (value: string | null) => value ? <code title={value}>{value.slice(0, 16)}…</code> : t("尚未激活");

export default function DeploymentsPanel() {
  const [rows, setRows] = useState<DeploymentView[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<DeploymentView>();
  const [after, setAfter] = useState("");
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]);
        const [list, current] = await Promise.all([deploymentApi.list(after, signal), selected ? deploymentApi.detail(selected, signal) : Promise.resolve(undefined)]);
        if (!controller.signal.aborted) { setRows(list.items); setDetail(current); setStale(false); }
      } catch { if (!controller.signal.aborted) setStale(true); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [after, selected, refresh]);
  async function reconcile(id: string) {
    setBusy(true);
    try { await deploymentApi.reconcile(id); setRefresh(value => value + 1); }
    catch { setStale(true); }
    finally { setBusy(false); }
  }
  const stamp = (value: number) => new Date(value * 1000).toLocaleString(languageTag(), {hour12: false});
  return <section className="tr-card" aria-label={t("版本部署")}>
    <h3>{t("版本部署")}</h3>
    <p className="tr-muted">{t("候选验证通过后等待存量任务完成；验证失败保留当前版本。")}</p>
    {stale && <p role="status" className="tr-notice">{t("部署状态更新失败，当前显示上次信息。")}</p>}
    {!rows.length && !stale && <p className="tr-muted">{t("尚无部署记录。")}</p>}
    {rows.map(row => <details key={row.deployment.id} open={selected === row.deployment.id} onToggle={event => {
      if (event.currentTarget.open) setSelected(row.deployment.id);
      else if (selected === row.deployment.id) setSelected("");
    }} className="tr-scheduler-row">
      <summary><strong>{row.deployment.id}</strong><span>{t(stale ? "观测待更新" : states[row.deployment.state] || row.deployment.state)}</span></summary>
      <dl><dt>{t("运行定义")}</dt><dd>{row.status.definition_id}</dd>
        <dt>{t("当前激活版本")}</dt><dd>{revision(row.status.active_revision_id)}</dd>
        <dt>{t("期望版本")}</dt><dd>{revision(row.deployment.revision_id)}</dd>
        <dt>{t("上一可用版本")}</dt><dd>{revision(row.status.previous_revision_id)}</dd>
        <dt>{t("最近更新")}</dt><dd>{stamp(row.status.updated_at)}</dd></dl>
      {row.status.error && <p className="tr-notice">{t("本次发布未激活")}: <code>{row.status.error}</code></p>}
      {detail?.deployment.id === row.deployment.id && <>
        <p>{t("验证任务")}: {detail.probe?.id} · {detail.probe?.state}</p>
        <ol>{detail.events?.map(event => <li key={event.version}>{stamp(event.created_at)} · {event.type}</li>)}</ol>
      </>}
      <button disabled={busy || stale} onClick={() => void reconcile(row.deployment.id)}>{t("核验发布状态")}</button>
    </details>)}
    <div className="tr-actions">
      {after && <button onClick={() => {setAfter(""); setSelected("");}}>{t("返回最新")}</button>}
      {rows.length === 50 && <button onClick={() => {setAfter(rows[49].deployment.id); setSelected("");}}>{t("更多部署")}</button>}
    </div>
  </section>;
}
