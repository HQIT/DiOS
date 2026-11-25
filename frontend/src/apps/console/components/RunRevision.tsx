import { useEffect, useState } from "react";
import { revisionApi } from "../../../api/runtime";
import type { RevisionCompatibility } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

const labels: Record<string, string> = {accepted: "兼容声明已接受", review_required: "兼容性待复核", unobserved: "尚未观测", pinned: "受控镜像已固定", supported: "支持", unsupported: "不支持", "pinned-image": "受控镜像", declared: "远端声明", opaque: "实现不可见"};
export default function RunRevision({revision, deployment, operator}: {revision?: string | null; deployment?: string | null; operator: boolean}) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState<RevisionCompatibility>();
  const [stale, setStale] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    setValue(undefined); setStale(false);
    if (!revision || !operator || !open) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const current = await revisionApi.compatibility(revision!, AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]));
        if (!controller.signal.aborted) {setValue(current); setStale(false);}
      } catch {if (!controller.signal.aborted) setStale(true);}
      if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
    }
    void poll(); return () => {controller.abort(); clearTimeout(timer);};
  }, [revision, operator, open, refresh]);
  if (!revision) return null;
  return <details className="tr-revision" onToggle={event => setOpen(event.currentTarget.open)} data-testid="run-revision">
    <summary>{t("执行版本")}</summary>
    <dl><dt>{t("任务固定版本")}</dt><dd><code>{revision}</code></dd>
      {deployment && <><dt>{t("关联部署")}</dt><dd><code>{deployment}</code></dd></>}
      {value && <><dt>{t("实现可见性")}</dt><dd>{t(labels[value.implementation_visibility] || value.implementation_visibility)}</dd>
        {value.implementation_digest && <><dt>{t("执行镜像摘要")}</dt><dd><code>{value.implementation_digest}</code></dd></>}
        <dt>{t("配置回退")}</dt><dd>{t(labels[value.configuration_rollback] || value.configuration_rollback)}</dd>
        <dt>{t("实现回退")}</dt><dd>{t(labels[value.implementation_rollback] || value.implementation_rollback)}</dd>
        <dt>{t("模型实现")}</dt><dd>{t(labels[value.model_implementation_visibility] || value.model_implementation_visibility)}</dd>
        <dt>{t("兼容状态")}</dt><dd>{t(labels[value.state] || value.state)}</dd>
        {value.qualification && <><dt>{t("远端报告版本")}</dt><dd>{value.qualification.snapshot.reported_version || t("未知")}</dd>
          <dt>{t("声明观测时间")}</dt><dd>{new Date(value.qualification.observed_at * 1000).toLocaleString(languageTag(), {hour12:false})}</dd></>}
      </>}
    </dl>
    {(stale || value?.qualification?.observed_stale) && <p role="status" className="tr-muted">{t("版本信息待更新，任务固定版本保持不变。")}</p>}
    {value?.qualification?.error && <p className="tr-muted"><code>{value.qualification.error}</code></p>}
    {operator && <button onClick={() => setRefresh(n => n+1)}>{t("刷新版本信息")}</button>}
  </details>;
}
