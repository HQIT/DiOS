import {useEffect, useState} from "react";
import {t} from "../../../lib/i18n";
import {evidenceApi} from "../../../api/runtime";
import type {EvidencePage, EvidenceRecord, ObjectReference} from "../../../api/runtime";

const sections: Record<string,string> = {attempts:"执行与会话",tools:"工具与审批",artifacts:"成果对象",sources:"知识与来源",usage:"用量报告",quarantine:"迟到报告",transfers:"详情同步"};
const levels: Record<string,string> = {platform_recorded:"平台记录",worker_observed:"Worker 实测",remote_reported:"远端报告",agent_reported:"Agent 报告",operator_declared:"人工登记",estimated:"估算",unknown:"未知"};
const states: Record<string,string> = {succeeded:"成功",failed:"失败",awaiting_approval:"等待审批",prepared:"准备完成",result_unknown:"结果未知",approved:"已批准",consumed:"审批已消费",not_required:"无需审批",denied:"已拒绝",invalidated:"已失效",quarantined:"已隔离",verified:"校验完成",pending:"等待同步"};
function refText(ref: EvidenceRecord["target"]): string {
  if (!ref) return "";
  return "kind" in ref ? `${ref.kind}/${ref.id} · v${ref.version}` : `${ref.namespace}/${ref.key} · ${ref.version}`;
}

export default function RunEvidencePanel({runId}: {runId: string}) {
  const [open,setOpen]=useState(false),[section,setSection]=useState("attempts"),[cursor,setCursor]=useState("");
  const [data,setData]=useState<EvidencePage|null>(null),[items,setItems]=useState<EvidenceRecord[]>([]);
  const [trail,setTrail]=useState<ObjectReference[]>([]),[next,setNext]=useState<string|null>(null);
  const [error,setError]=useState(""),[loading,setLoading]=useState(false),[refresh,setRefresh]=useState(0);
  const target=trail.at(-1);
  useEffect(()=>{
    if(!open)return;
    const controller=new AbortController();setLoading(true);setError("");setItems([]);setNext(null);
    const work=async()=>{
      try {
        if(target){
          const value=await evidenceApi.sources(target,cursor,controller.signal);
          if(controller.signal.aborted)return;
          setItems(value.items.map(x=>({...x,source:target,evidence_level:"operator_declared"})));setNext(value.next_after);
        } else {
          const value=await evidenceApi.page(runId,section,cursor,controller.signal);
          if(controller.signal.aborted)return;
          setData(value);setItems(value.items);setNext(value.next_cursor);
        }
      }catch(e){if(!controller.signal.aborted){setData(null);setError(e instanceof Error?e.message:String(e));}}
      finally{if(!controller.signal.aborted)setLoading(false);}
    };void work();return()=>controller.abort();
  },[open,runId,section,cursor,target,refresh]);
  const choose=(value:string)=>{setSection(value);setCursor("");setTrail([]);setData(null);};
  return <details className="tr-card" onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary>{t("执行与来源证据")}</summary>
    {open&&<>
      <p className="tr-muted">{t("按任务关联版本、执行、工具和成果来源。仅展示已记录关系，Agent 内部活动覆盖未知。")}</p>
      <div className="tr-artifacts">{Object.entries(sections).map(([key,label])=><button key={key} className={key===section?"selected":""} onClick={()=>choose(key)}>{t(label)}</button>)}<button onClick={()=>setRefresh(x=>x+1)}>{t("刷新")}</button></div>
      {data&&!target&&<details><summary>{t("执行版本")} · {data.root.execution.profile_id}</summary>
        <p>{t(data.root.execution.status==="recorded"?"已固定版本":"版本记录缺失")}</p>
        <code className="tr-source">{data.root.execution.revision_ref||data.root.execution.profile_digest}</code>
        {Object.entries(data.root.execution.dependencies).map(([kind,values])=><div key={kind}><strong>{kind}</strong>{values.map((value,index)=><p key={index}><span>{value.name} · {value.version||t("未知")}</span><code className="tr-source">{value.digest||t("未知")}</code></p>)}</div>)}
      </details>}
      {target&&<p><button onClick={()=>{setTrail(value=>value.slice(0,-1));setCursor("");}}>{t("返回上级来源")}</button><code className="tr-source">{refText(target)}</code></p>}
      {loading&&<p role="status">{t("正在读取证据…")}</p>}
      {error&&<p role="alert">{t("证据暂不可用")}: {error}</p>}
      {!loading&&!error&&data?.access==="restricted"&&!target&&<p>{t("当前身份无权读取这类证据。")}</p>}
      {!loading&&!error&&(!data||data.access!=="restricted"||target)&&items.length===0&&<p>{t("本页没有可见记录；无记录不代表活动未发生。")}</p>}
      {items.map((row,index)=><article className="tr-attempt" key={row.id||`${row.attempt_id}-${row.sequence}-${index}`}>
        <span className="tr-badge">{t(levels[row.evidence_level]||"未知")}</span>
        <strong>{row.tool_id||t(row.kind==="quarantine"?"迟到报告":row.kind==="transfer"?"详情同步":row.object_ref?"成果对象":row.relation?"来源关联":row.kind?"用量报告":"执行尝试")}</strong>
        <code className="tr-source">{row.id||row.attempt_id}</code>
        {row.state&&<span>{t(states[row.state]||row.state)}</span>}
        {row.attempt_id&&<small>{t("尝试")}: {row.attempt_id}</small>}
        {row.worker_id&&<small>Worker: {row.worker_id} · {row.worker_instance}</small>}
        {row.session&&<small>{t("会话")}: {row.session.session_id}</small>}
        {row.approval_state&&<small>{t("审批")}: {t(states[row.approval_state]||row.approval_state)}</small>}
        {row.reason&&<small>{t("原因")}: {row.reason}</small>}
        {row.result_evidence&&<small>{t("工具结果证据")}: {t(levels[row.result_evidence]||"未知")}</small>}
        {row.descriptor&&<small className="tr-source">{row.descriptor.digest} · {row.descriptor.size} B</small>}
        {row.source&&<code className="tr-source">{refText(row.source)}</code>}
        {row.target&&<><span>{row.relation} →</span><code className="tr-source">{refText(row.target)}</code>
          {"kind" in row.target&&<button onClick={()=>{setTrail(value=>[...value,row.target as ObjectReference]);setCursor("");}}>{t("继续查看来源")}</button>}</>}
        {row.values&&<pre className="tr-source">{JSON.stringify(row.values,null,2)}</pre>}
      </article>)}
      {next&&<button disabled={loading} onClick={()=>setCursor(next)}>{t("下一页证据")}</button>}
      {cursor&&<button disabled={loading} onClick={()=>setCursor("")}>{t("返回第一页")}</button>}
    </>}
  </details>;
}
