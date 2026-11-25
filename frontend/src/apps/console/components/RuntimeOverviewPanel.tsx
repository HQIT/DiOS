import { useEffect, useState } from "react";
import { runtimeApi } from "../../../api/runtime";
import type { Profile, RuntimeOverview } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

const labels: Record<string,string> = {queued:"排队中",retry_wait:"等待重试",reconciliation_required:"等待恢复对账",observation_stale:"观测过期",no_recent_output:"超过 15 分钟无有效输出",process_paused:"执行已暂停",process_exited:"进程已退出，等待对账",execution_unconfirmed:"尚未确认运行",claimed:"已领取",preparing:"准备中",creating_session:"创建会话",waiting_model:"等待模型",streaming:"输出中",validating:"校验成果"};
const outcomes = ["succeeded", "failed", "cancelled"] as const;
const stateLabels = {succeeded:"成功",failed:"失败",cancelled:"已取消"};

export default function RuntimeOverviewPanel({profiles, profileId, onProfileChange}: {profiles: Profile[]; profileId: string; onProfileChange: (value: string) => void}) {
  const [period,setPeriod]=useState<"hour"|"day">("hour");
  const [value,setValue]=useState<RuntimeOverview>(); const [stale,setStale]=useState(false);
  useEffect(()=>{
    const controller=new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setValue(undefined);setStale(false);
    async function poll(){
      try {const result=await runtimeApi.overview(period,profileId,AbortSignal.any([controller.signal,AbortSignal.timeout(10000)]));
        if(!controller.signal.aborted){setValue(result);setStale(false);}}
      catch {if(!controller.signal.aborted)setStale(true);}
      if(!controller.signal.aborted)timer=setTimeout(poll,5000);
    }
    void poll();return()=>{controller.abort();clearTimeout(timer);};
  },[period,profileId]);
  const peak=Math.max(1,...(value?.series.map(x=>x.succeeded+x.failed+x.cancelled)||[]));
  const stamp=(v:number)=>new Date(v*1000).toLocaleString(languageTag(),{timeZone:"UTC",hour12:false});
  const ticks=(v:number)=>new Date(v*1000).toLocaleString(languageTag(),period==="hour"?{timeZone:"UTC",hour:"2-digit",hour12:false}:{timeZone:"UTC",month:"2-digit",day:"2-digit"});
  const alerts=value?.active.items.filter(x=>["observation_stale","no_recent_output","process_exited","execution_unconfirmed","reconciliation_required","retry_wait"].includes(x.diagnosis))||[];
  return <section className="tr-card tr-overview" aria-label={t("任务完成量与运行观测")}>
    <div className="tr-overview-heading"><h3>{t("任务完成量与运行观测")}</h3><div>
      <label>{t("筛选执行配置")}<select aria-label={t("筛选执行配置")} value={profileId} onChange={e=>onProfileChange(e.target.value)}><option value="">{t("全部可见任务")}</option>{profiles.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <label>{t("统计窗口")}<select aria-label={t("统计窗口")} value={period} onChange={e=>setPeriod(e.target.value as "hour"|"day")}><option value="hour">{t("最近 24 小时")}</option><option value="day">{t("最近 30 天")}</option></select></label>
    </div></div>
    <p className="tr-muted">{t("每个逻辑任务仅按首次终态计数；重试与补报不重复计数。时间分桶采用 UTC。")}</p>
    {stale && <p className="tr-notice" role="status">{t("看板更新失败，当前显示上次观测；任务状态待刷新。")}</p>}
    {!value && !stale && <p>{t("正在读取运行观测。")}</p>}
    {value && <>
      {!value.coverage.complete && <p className="tr-notice">{t("部分终态缺少计数证据，当前统计尚不完整。")}</p>}
      <div className="tr-overview-metrics"><span>{t("窗口内成功")}<strong>{value.window_totals.succeeded}</strong></span><span>{t("历史成功")}<strong>{value.history_totals.succeeded}</strong></span><span>{t("排队中")}<strong>{value.states.queued||0}</strong></span><span>{t("实际并发")}<strong>{stale || value.active.actual_concurrency===null ? t("待确认") : value.active.actual_concurrency}</strong></span></div>
      <div className="tr-chart-legend">{outcomes.map(k=><span key={k}><i className={`tr-outcome-${k}`}/>{t(stateLabels[k])} {value.window_totals[k]}</span>)}<small>{t("观测时间")}: {stamp(value.observed_at)} UTC</small></div>
      <div className="tr-chart-scroll"><div className="tr-throughput-chart" role="img" aria-label={t("按时间统计的任务终态数量")}>
        {value.series.map((row,index)=><div className="tr-throughput-column" key={row.start} title={`${stamp(row.start)} UTC · ${outcomes.map(k=>`${t(stateLabels[k])}: ${row[k]}`).join(" · ")}`}>
          <div className="tr-throughput-bar">{outcomes.map(k=><i key={k} className={`tr-outcome-${k}`} style={{height:`${row[k]/peak*100}%`}} />)}</div>
          <small>{index%Math.ceil(value.series.length/6)===0?ticks(row.start):""}</small>
        </div>)}
      </div></div>
      <details><summary>{t("查看统计明细")}</summary><div className="tr-chart-scroll"><table className="tr-throughput-table"><thead><tr><th>UTC</th>{outcomes.map(k=><th key={k}>{t(stateLabels[k])}</th>)}</tr></thead><tbody>{value.series.map(row=><tr key={row.start}><td>{stamp(row.start)}</td>{outcomes.map(k=><td key={k}>{row[k]}</td>)}</tr>)}</tbody></table></div></details>
      {!!alerts.length && <div className="tr-overview-alerts">{alerts.slice(0,5).map(row=><a key={row.id} href={`#console/tasks/${row.id}`}>{t(labels[row.diagnosis]||row.diagnosis)} · {row.id.slice(0,8)}{row.retry_at!==null && ` · ${t("下次重试")}: ${stamp(row.retry_at)} UTC`}</a>)}{alerts.length>5 && <small>{t("更多运行观测请查看任务详情。")}</small>}</div>}
      {value.active.at_limit && <p>{t("运行观测仅包含最早的 200 条未结束任务，实际并发待确认。")}</p>}
    </>}
  </section>;
}
