import type { RunDiagnostics as Diagnostics } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

const phases: Record<string, string> = {claimed:"已领取", preparing:"准备中", creating_session:"创建会话", waiting_model:"等待模型", streaming:"输出中", validating:"校验成果"};
const measurements: Record<string, string> = {lookup_seconds:"授权读取", policy_refresh_seconds:"策略刷新", transaction_wait_seconds:"事务等待", authorization_seconds:"授权检查", before_audit_seconds:"审计前总耗时", opa_request_seconds:"OPA 请求"};
const stamp = (v: number | null) => v === null ? t("未知") : new Date(v * 1000).toLocaleString(languageTag(), {hour12:false});
const duration = (v: number) => `${v.toLocaleString(languageTag(), {maximumFractionDigits: 4})} ${t("秒")}`;

export default function RunDiagnostics({value}: {value?: Diagnostics}) {
  if (!value) return null;
  return <details className="tr-notice tr-diagnostics">
    <summary>{t("执行耗时与诊断")}</summary>
    <p>{t("阶段时间来自控制面接收记录，包含传输和补报延迟。")}</p>
    <p>{t("最近事件")}: {stamp(value.last_event_at)} · {t("最近有效输出")}: {stamp(value.last_output_at)}</p>
    <p>{t("观测状态")}: {t(({fresh:"观测新鲜",stale:"观测过期",final:"已结束",no_worker:"尚未领取"} as Record<string,string>)[value.freshness] || "未知")}</p>
    {value.stages.length === 0 && <p>{t("尚无执行阶段记录。")}</p>}
    <div className="tr-diagnostic-stages">
      {value.stages.map((row,index) => <div key={`${row.attempt_id}-${index}`} className="tr-diagnostic-stage">
        <strong>{t(phases[row.phase] || row.phase)}</strong><small>{t("尝试")} {row.epoch} · {row.attempt_id.slice(0,8)}</small>
        <span>{row.clock_anomaly ? t("时钟异常，耗时未知") : row.duration_seconds !== null ? duration(row.duration_seconds) : row.elapsed_seconds !== null ? `${t("尚未闭合，距开始")} ${duration(row.elapsed_seconds)}` : t("未知")}</span>
        <small>{stamp(row.started_at)}</small>
      </div>)}
    </div>
    {value.attempts_has_more && <p>{t("此处显示最近 20 次尝试。")}</p>}
    <details><summary>{t("E2AG 检查耗时")} · {value.governance.length}</summary>
      <p>{t("单调时钟实测；覆盖已审计检查点，审计落盘和事务提交耗时未计入。")}</p>
      <div className="tr-diagnostic-audits">{value.governance.map(row => <div key={row.id}>
        <strong>{row.reason_codes.join(" · ")}</strong><small>{stamp(row.created_at)} · {row.attempt_id?.slice(0,8)}</small>
        <p>{row.timing ? Object.entries(row.timing).filter(([key]) => key in measurements).map(([key,v]) => `${t(measurements[key])}: ${duration(v as number)}`).join(" · ") : t("未记录耗时")}</p>
      </div>)}</div>
      {value.governance_has_more && <p>{t("仅显示最近 100 条治理记录。")}</p>}
    </details>
  </details>;
}
