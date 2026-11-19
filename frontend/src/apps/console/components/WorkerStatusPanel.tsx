import { useEffect, useState } from "react";
import { runtimeApi } from "../../../api/runtime";
import type { WorkerStatus } from "../../../api/runtime";

export default function WorkerStatusPanel() {
  const [status, setStatus] = useState<WorkerStatus>();
  const [stale, setStale] = useState(false);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await runtimeApi.workers(controller.signal);
        if (!controller.signal.aborted) { setStatus(value); setStale(false); }
      } catch { if (!controller.signal.aborted) setStale(true); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  const sorted = [...(status?.workers || [])].sort((a, b) => b.heartbeat_at - a.heartbeat_at);
  const online = status?.workers.filter(w => status.observed_at - w.heartbeat_at <= 15).length;
  return <section className="tr-workers" aria-label="Worker 执行状态">
    <div className="tr-worker-heading"><strong>执行节点</strong>
      <span>{!status || stale ? "观测待更新" : `在线 ${online} · 实际并发 ${status.actual_concurrency ?? "待确认"}`}</span>
    </div>
    {stale && <p role="status">节点观测暂不可用，当前保留最近一次信息。</p>}
    {status && !sorted.length && <p>暂无已注册 Worker。任务将在节点连接后领取。</p>}
    {sorted.slice(0, 6).map(w => {
      const fresh = !stale && status!.observed_at - w.heartbeat_at <= 15;
      const active = status!.attempts.filter(a => a.worker_id === w.id);
      return <div className="tr-worker-row" key={w.id}>
        <div><strong>{w.host}</strong><small title={w.id}>{w.id}</small></div>
        <span>{!fresh ? "观测过期" : w.draining ? "排空中" : active.length ? "执行中" : "在线待命"}</span>
        <span>最近心跳 {new Date(w.heartbeat_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</span>
        <div>{active.map(a => <a key={a.run_id} href={`#console/tasks/${a.run_id}`}>查看任务</a>)}</div>
      </div>;
    })}
    {sorted.length > 6 && <small>显示最近活动的 6 个 Worker，共 {sorted.length} 个历史登记。</small>}
  </section>;
}
