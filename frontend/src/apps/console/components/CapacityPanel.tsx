import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { capacityApi, RuntimeApiError } from "../../../api/runtime";
import type { CapacityLimit, CapacitySelector } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

export default function CapacityPanel() {
  const [rows, setRows] = useState<CapacityLimit[]>([]);
  const [observed, setObserved] = useState(0);
  const [stale, setStale] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [id, setId] = useState("");
  const [kind, setKind] = useState<CapacitySelector["kind"]>("scope");
  const [target, setTarget] = useState("");
  const [maximum, setMaximum] = useState("2");
  const [enabled, setEnabled] = useState(true);
  const [revision, setRevision] = useState(0);
  const [runtime, setRuntime] = useState("");
  const [dispatches, setDispatches] = useState("");
  const [windowSeconds, setWindowSeconds] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const sequence = useRef(0);

  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const seq = ++sequence.current;
      try {
        const value = await capacityApi.status(controller.signal);
        if (controller.signal.aborted || seq !== sequence.current) return;
        setRows(value.items); setObserved(value.observed_at); setStale(false);
      } catch {
        if (!controller.signal.aborted && seq === sequence.current) setStale(true);
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);

  function edit(row?: CapacityLimit) {
    setId(row?.id || ""); setKind(row?.selector.kind || "scope");
    setTarget(row?.selector.kind === "subject" ? row.selector.subject : row?.selector.kind === "profiles" ? row.selector.profiles.join(", ") : "");
    setMaximum(String(row?.max_concurrent ?? 2)); setEnabled(row?.enabled ?? true);
    setRevision(row?.revision || 0); setError(""); setEditing(true);
    setRuntime(String(row?.budget?.max_runtime_seconds ?? ""));
    setDispatches(String(row?.budget?.max_dispatches ?? ""));
    setWindowSeconds(String(row?.budget?.dispatch_window_seconds ?? ""));
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    const selector: CapacitySelector = kind === "scope" ? {kind} : kind === "subject" ? {kind, subject: target.trim()} : {kind, profiles: target.split(/[,，\n]/).map(s => s.trim()).filter(Boolean)};
    setBusy(true); setError("");
    try {
      await capacityApi.save({id: id.trim(), selector, max_concurrent: Number(maximum), enabled, expected_revision: revision,
        budget: {max_runtime_seconds: runtime === "" ? null : Number(runtime), max_dispatches: dispatches === "" ? null : Number(dispatches), dispatch_window_seconds: windowSeconds === "" ? null : Number(windowSeconds)}});
      setEditing(false); setRefresh(v => v + 1);
    } catch (err) {
      setError(err instanceof RuntimeApiError && err.code === "VERSION_CONFLICT"
        ? "额度已被其他操作更新，请重新选择该额度后修改。"
        : err instanceof RuntimeApiError && err.code.startsWith("INVALID_")
          ? "请检查额度标识、选择范围和并发上限。" : "额度保存失败，请检查权限与连接后重试。");
      setRefresh(v => v + 1);
    } finally { setBusy(false); }
  }

  return <details className="tr-capacity">
    <summary>{t("共享执行额度")}{stale ? ` · ${t("观测待更新")}` : ` · ${rows.filter(r => r.enabled).length} ${t("已启用")}`}</summary>
    <p>{t("准备中和结果待确认的任务均占用额度。实际并发见 Worker 观测。")}</p>
    {stale && <p role="status">{t("额度观测暂不可用，请刷新后操作。")}</p>}
    {!!observed && <small>{t("最近观测")}: {new Date(observed * 1000).toLocaleTimeString(languageTag())}</small>}
    <div className="tr-capacity-actions"><button type="button" disabled={busy} onClick={() => setRefresh(v => v + 1)}>{t("刷新")}</button><button type="button" disabled={busy || stale} onClick={() => edit()}>{t("新增额度")}</button></div>
    {!stale && !rows.length && <p>{t("尚未配置共享额度，沿用 Worker 槽位与执行配置限制。")}</p>}
    {rows.map(row => <article className="tr-capacity-row" key={row.id}>
      <div><strong>{row.id}</strong><small>{t(row.selector.kind === "scope" ? "整个控制域" : row.selector.kind === "subject" ? "指定主体" : "运行配置组")}{row.selector.kind === "subject" ? ` · ${row.selector.subject}` : row.selector.kind === "profiles" ? ` · ${row.selector.profiles.join(", ")}` : ""}</small></div>
      <span>{t(row.enabled ? "已启用" : "已停用")} · {t("并发上限")}: {row.max_concurrent}</span>
      <span>{t("占位")}: {row.reserved_attempts} · {t("待确认")}: {row.unresolved_attempts} · {t("排队")}: {row.queued_runs}</span>
      {row.budget?.max_runtime_seconds != null && <span>{t("执行时长上限（秒）")}: {row.budget.max_runtime_seconds}</span>}
      {row.dispatch_rate && <span>{t("窗口内已领取")}: {row.dispatch_rate.used}/{row.dispatch_rate.maximum} · {t("窗口长度（秒）")}: {row.dispatch_rate.window_seconds}{row.enabled && row.dispatch_rate.available_at && <> · {t("最早可再次领取")}: {new Date(row.dispatch_rate.available_at * 1000).toLocaleTimeString(languageTag())}</>}</span>}
      <button type="button" disabled={busy || stale} onClick={() => edit(row)}>{t("修改额度")}</button>
    </article>)}
    {!!rows.length && <small>{t("同一任务可匹配多个额度，排队数量不可累加。")}</small>}
    {editing && <form onSubmit={save} className="tr-capacity-form">
      <fieldset disabled={busy || stale}>
        <label>{t("额度标识")}<input required maxLength={160} value={id} readOnly={revision > 0} onChange={e => setId(e.target.value)} /></label>
        <label>{t("适用范围")}<select aria-label={t("适用范围")} value={kind} onChange={e => {setKind(e.target.value as CapacitySelector["kind"]); setTarget("");}}>
          <option value="scope">{t("整个控制域")}</option><option value="subject">{t("指定主体")}</option><option value="profiles">{t("运行配置组")}</option>
        </select></label>
        {kind !== "scope" && <label>{t(kind === "subject" ? "主体标识" : "配置标识（逗号分隔）")}<input required value={target} onChange={e => setTarget(e.target.value)} /></label>}
        <label>{t("并发上限")}<input type="number" required min={0} max={100000} step={1} value={maximum} onChange={e => setMaximum(e.target.value)} /></label>
        <label>{t("执行时长上限（秒）")}<input type="number" min={1} max={604800} step={1} value={runtime} onChange={e => setRuntime(e.target.value)} /></label>
        <label>{t("窗口内最多领取次数")}<input type="number" required={windowSeconds !== ""} min={1} max={100000} step={1} value={dispatches} onChange={e => setDispatches(e.target.value)} /></label>
        <label>{t("窗口长度（秒）")}<input type="number" required={dispatches !== ""} min={1} max={86400} step={1} value={windowSeconds} onChange={e => setWindowSeconds(e.target.value)} /></label>
        <p>{t("留空表示不设置该预算。时长从首次领取计时，包含重试等待；修改时长只影响尚未领取的任务。领取频率按 Attempt 计数。")}</p>
        <label className="tr-capacity-checkbox"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />{t("启用此额度")}</label>
        <p>{t("上限为 0 时暂停新领取；停用额度会解除该项限制。已有任务自然结束。")}</p>
        <div className="tr-capacity-actions"><button type="submit">{t(busy ? "保存中…" : "保存额度")}</button><button type="button" onClick={() => {setEditing(false); setError("");}}>{t("取消")}</button></div>
      </fieldset>
    </form>}
    {error && <p role="alert">{t(error)}</p>}
  </details>;
}
