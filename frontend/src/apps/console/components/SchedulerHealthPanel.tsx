import { useEffect, useState } from "react";
import { runtimeApi } from "../../../api/runtime";
import type { SchedulerHealth } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

const states: Record<string,string> = {healthy:"调度循环正常",degraded:"调度循环失败",slow:"调度周期耗时过长",stale:"调度观测过期",stopped:"调度循环已停止",unobserved:"尚无成功周期证据",disabled:"调度功能未启用"};
export default function SchedulerHealthPanel() {
  const [value,setValue]=useState<SchedulerHealth>();const [stale,setStale]=useState(false);
  useEffect(()=>{
    const controller=new AbortController();let timer: ReturnType<typeof setTimeout>;
    async function poll(){
      try {const result=await runtimeApi.schedulerHealth(AbortSignal.any([controller.signal,AbortSignal.timeout(10000)]));if(!controller.signal.aborted){setValue(result);setStale(false);}}
      catch {if(!controller.signal.aborted)setStale(true);}
      if(!controller.signal.aborted)timer=setTimeout(poll,5000);
    }
    void poll();return()=>{controller.abort();clearTimeout(timer);};
  },[]);
  const stamp=(v:number|null|undefined)=>v==null?t("未记录"):new Date(v*1000).toLocaleString(languageTag(),{hour12:false});
  return <section className="tr-card tr-scheduler-health" aria-label={t("调度器健康")}>
    <h3>{t("调度器健康")}</h3>
    <p className="tr-muted">{t("周期完成表示调度检查已返回；具体任务的执行结果见任务详情。")}</p>
    {stale && <p className="tr-notice" role="status">{t("调度观测更新失败，当前显示上次信息。")}</p>}
    {!value && !stale && <p>{t("正在读取运行观测。")}</p>}
    {value?.services.map(s=><details key={s.service} className="tr-scheduler-row">
      <summary><strong>{t(s.service==="run_scheduler"?"任务定时调度":"订阅定时调度")}</strong><span>{t(stale?"观测待更新":states[s.state]||s.state)}</span></summary>
      <dl><dt>{t("最近心跳")}</dt><dd>{stamp(s.evidence?.heartbeat_at)}</dd><dt>{t("最近周期完成")}</dt><dd>{stamp(s.evidence?.completed_at)}</dd><dt>{t("最近周期成功")}</dt><dd>{stamp(s.evidence?.succeeded_at)}</dd><dt>{t("连续周期错误")}</dt><dd>{s.evidence?.consecutive_errors??t("未记录")}</dd><dt>{t("最近周期耗时")}</dt><dd>{s.evidence?.duration_seconds==null?t("未记录"):`${s.evidence.duration_seconds.toFixed(3)} s`}</dd></dl>
      {s.evidence?.error && <code>{s.evidence.error}</code>}
      <p>{t("新鲜实例")}: {stale?t("待确认"):s.fresh_instances} · {t("保留实例")}: {s.instances.length}</p>
      {s.at_limit && <p>{t("实例列表已截断，详情最多返回 100 条。")}</p>}
    </details>)}
  </section>;
}
