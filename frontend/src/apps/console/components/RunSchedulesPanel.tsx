import { useEffect, useRef, useState } from "react";
import { scheduleApi, RuntimeApiError } from "../../../api/runtime";
import type { RunSchedule, ScheduledOccurrence } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

const labels: Record<string, string> = {pending: "待派发", buffered: "合并等待", running: "执行已接收", succeeded: "执行完成", dead_letter: "待处理", discarded: "已跳过", expired: "超出补发窗口"};
const date = (value: number) => new Date(value * 1000).toLocaleString(languageTag(), {hour12: false, timeZoneName: "short"});
function errorText(error: unknown) {
  if (error instanceof RuntimeApiError) {
    if (error.code === "VERSION_CONFLICT") return t("配置已更新，请刷新后重新操作。");
    return t("操作未完成：{0}", [error.code]);
  }
  return t("连接中断，已保留页面信息。请重试。");
}
function Occurrence({item}: {item: ScheduledOccurrence}) {
  return <div className="tr-schedule-occurrence">
    <strong>{t(labels[item.state] || item.state)}</strong> · {date(item.first_due)}
    {item.last_due !== item.first_due && <> — {date(item.last_due)}</>}
    <span> · {item.trigger_count === null ? t("窗口外范围") : t("合并 {0} 次触发", [item.trigger_count])}</span>
    {item.run_id && <> · <a href={`#console/tasks/${item.run_id}`}>{t("查看原任务")}</a></>}
    {item.error && <p className="tr-notice">{item.error}</p>}
    {item.retry_at && <p>{t("· 下次重试")}{date(item.retry_at)}</p>}
  </div>;
}
function ScheduleRow({row, changed}: {row: RunSchedule; changed: () => void}) {
  const [reason, setReason] = useState(""); const [busy, setBusy] = useState(false);
  const [error, setError] = useState(""); const [show, setShow] = useState(false);
  const [history, setHistory] = useState<ScheduledOccurrence[]>([]);
  const [before, setBefore] = useState<string>(); const [next, setNext] = useState<string | null>(null);
  const keyRef = useRef<{content: string; key: string} | null>(null);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    if (!show) return () => controller.abort();
    async function poll() {
      try {
        const result = await scheduleApi.history(row.id, before, controller.signal);
        if (!controller.signal.aborted) {setHistory(result.items); setNext(result.next_cursor); setError("");}
      } catch (error) {if (!controller.signal.aborted) setError(errorText(error));}
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    }
    void poll(); return () => {controller.abort(); clearTimeout(timer);};
  }, [row.id, row.version, show, before]);
  async function act(action: "pause" | "resume" | "reauthorize" | "retry" | "discard") {
    if (busy || !reason.trim()) return;
    setBusy(true); setError("");
    try {
      if (action === "retry" || action === "discard") {
        if (!row.active) return;
        const content = JSON.stringify([row.id, row.active.id, action, reason.trim()]);
        if (keyRef.current?.content !== content) keyRef.current = {content, key: crypto.randomUUID()};
        await scheduleApi.resolve(row, row.active, action, reason.trim(), keyRef.current.key);
      } else await scheduleApi.control(row, action, reason.trim());
      keyRef.current = null; changed();
    } catch (error) {setError(errorText(error)); changed();}
    finally {setBusy(false);}
  }
  return <article className="tr-schedule" data-schedule-id={row.id}>
    <div className="tr-section-title"><h4>{row.template.goal}</h4><span className="tr-badge">{t(row.enabled ? "已启用" : "已暂停")}</span></div>
    <p>{row.template.profile_id} · <code>{row.expression}</code> UTC</p>
    <p className="tr-muted">{t("下次触发：")}{row.enabled ? date(row.next_due) : t("已暂停")} · {t("服务身份：")}{row.binding.actor}</p>
    <details><summary>{t("授权绑定")}</summary><p>{t("主体：")}{row.binding.subject}</p><p>{t("策略：")}{row.binding.policy_id}</p><code className="tr-schedule-digest">{row.binding.policy_digest}</code></details>
    {row.active && <Occurrence item={row.active}/>}
    {row.buffered && <Occurrence item={row.buffered}/>}
    <label>{t("操作原因")}<input value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} disabled={busy}/></label>
    <div className="tr-actions">
      <button disabled={busy || !reason.trim()} onClick={() => void act(row.enabled ? "pause" : "resume")}>{t(row.enabled ? "暂停定时" : "恢复定时")}</button>
      <button disabled={busy || !reason.trim()} onClick={() => void act("reauthorize")}>{t("按当前服务策略重新授权")}</button>
      {row.active?.state === "dead_letter" && <>
        {!row.active.run_id && <button disabled={busy || !reason.trim()} onClick={() => void act("retry")}>{t("重试派发")}</button>}
        <button disabled={busy || !reason.trim()} onClick={() => void act("discard")}>{t("跳过本轮")}</button>
      </>}
      <button onClick={() => setShow(!show)}>{t(show ? "收起触发历史" : "触发历史")}</button>
    </div>
    <p className="tr-muted">{t("重新授权会采用当前服务身份与权限，跳过尚未接收的旧触发；已有任务保留原授权。")}</p>
    {error && <p className="tr-error" role="alert">{error}</p>}
    {show && <div>{!history.length && <p>{t("暂无触发记录")}</p>}{history.map(item => <Occurrence key={item.id} item={item}/>)}
      <div className="tr-actions">{before && <button onClick={() => setBefore(undefined)}>{t("返回最新")}</button>}{next && <button onClick={() => setBefore(next)}>{t("更早的触发")}</button>}</div>
    </div>}
  </article>;
}
export default function RunSchedulesPanel({refresh}: {refresh: number}) {
  const [items, setItems] = useState<RunSchedule[]>([]); const [error, setError] = useState("");
  const [enabled, setEnabled] = useState<boolean>(); const [observed, setObserved] = useState<number>();
  const [before, setBefore] = useState<string>(); const [next, setNext] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await scheduleApi.list(before, controller.signal);
        if (!controller.signal.aborted) {setItems(result.items); setNext(result.next_cursor); setEnabled(result.scheduler_enabled); setObserved(result.observed_at); setError("");}
      } catch (error) {if (!controller.signal.aborted) setError(errorText(error));}
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    }
    void poll(); return () => {controller.abort(); clearTimeout(timer);};
  }, [refresh, revision, before]);
  return <section className="tr-card" aria-label={t("定时任务")}>
    <div className="tr-section-title"><h3>{t("定时任务")}</h3><button onClick={() => setRevision(v => v + 1)}>{t("刷新定时任务")}</button></div>
    <p className="tr-muted">{t("上一轮结束后再执行，积压触发合并为一轮，补发窗口为 24 小时。")}</p>
    {observed && <p className="tr-muted">{t("最近更新：")}{date(observed)}</p>}
    {enabled === false && <p className="tr-notice">{t("定时调度器尚未启用，配置已保存。")}</p>}
    {error && <p className="tr-error" role="alert">{error} {t("当前保留的是上次取得的信息。")}</p>}
    {observed && !items.length && !error && <p>{t("在发起任务时选择定时执行，即可创建计划。")}</p>}
    {items.map(row => <ScheduleRow key={row.id} row={row} changed={() => setRevision(v => v + 1)}/>)}
    <div className="tr-actions">{before && <button onClick={() => setBefore(undefined)}>{t("返回最新")}</button>}{next && <button onClick={() => setBefore(next)}>{t("更早的计划")}</button>}</div>
  </section>;
}
