import { useEffect, useRef, useState } from "react";
import { runtimeApi, studioApi, RuntimeApiError } from "../../../api/runtime";
import type { ConfigReview, Profile, RunDetail } from "../../../api/runtime";
import { usePreferences } from "../../../lib/preferences";
import "./configuration-studio.css";

const TEMPLATE = JSON.stringify({id: "new-capability", bridge: "jsonl-v1", governance: {mode: "e2ag"}, launch: {image: "dios-worker-fixture:local", command: ["python", "/opt/dios/fixture_agent.py"], network: "none", memory_bytes: 268435456, cpu_count: 1, pids_limit: 64, read_only_root: true}}, null, 2) + "\n";
const terminal = new Set(["succeeded", "failed", "cancelled"]);

export default function ConfigurationStudio() {
  const {locale} = usePreferences();
  const l = (zh: string, en: string) => locale === "zh" ? zh : en;
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profile, setProfile] = useState("");
  const [assistant, setAssistant] = useState("");
  const [base, setBase] = useState(TEMPLATE);
  const [draft, setDraft] = useState(TEMPLATE);
  const [instruction, setInstruction] = useState("");
  const [review, setReview] = useState<ConfigReview | null>(null);
  const [proposal, setProposal] = useState<{text: string; base: string; review: ConfigReview} | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [observed, setObserved] = useState(true);
  const generation = useRef(0);
  const request = useRef({input: "", key: ""});
  const editor = useRef<HTMLTextAreaElement>(null);
  const candidates = profiles.filter(p => p.input_kind === "dios-config-proposal-v1" && p.governance === "e2ag" && p.dynamic_inputs);

  useEffect(() => {
    const controller = new AbortController();
    runtimeApi.profiles(controller.signal).then(value => {
      if (!controller.signal.aborted) setProfiles(value.items);
    }).catch(() => { if (!controller.signal.aborted) setError("PROFILE_LIST_UNAVAILABLE"); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (base === draft) return;
    const warn = (event: BeforeUnloadEvent) => {event.preventDefault(); event.returnValue = "";};
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [base, draft]);
  const runId = run?.id;
  const runState = run?.state;
  useEffect(() => {
    if (!runId || (runState && terminal.has(runState))) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await runtimeApi.detail(runId!, controller.signal);
        if (!controller.signal.aborted) {setRun(result); setObserved(true);}
      } catch {if (!controller.signal.aborted) setObserved(false);}
      if (!controller.signal.aborted) timer = setTimeout(poll, 3000);
    }
    void poll(); return () => {controller.abort(); clearTimeout(timer);};
  }, [runId, runState]);
  useEffect(() => () => {generation.current++;}, []);

  function edit(text: string) {generation.current++; setDraft(text); setReview(null); setProposal(null);}
  function failure(err: unknown) {setError(err instanceof RuntimeApiError ? err.code : "REQUEST_FAILED");}
  async function load() {
    if (!profile) return;
    if (draft !== base && !window.confirm(l("替换当前草案？可先导出留存。", "Replace this draft? Export it first to keep a copy."))) return;
    setBusy(true); setError(""); const seq = ++generation.current;
    try {const value = await studioApi.source(profile); if (seq === generation.current) {setBase(value.text); edit(value.text); setRun(null);}}
    catch (err) {failure(err);} finally {setBusy(false);}
  }
  async function compare() {
    setBusy(true); setError(""); const seq = ++generation.current;
    try {const value = await studioApi.compare(base, draft); if (seq === generation.current) setReview(value);}
    catch (err) {failure(err);} finally {setBusy(false);}
  }
  async function importFile(file?: File) {
    if (!file) return;
    if (file.size > 65536) {setError("STUDIO_DOCUMENT_TOO_LARGE"); return;}
    edit(await file.text()); setError("");
  }
  function download() {
    const url = URL.createObjectURL(new Blob([draft], {type: "application/json;charset=utf-8"}));
    const link = document.createElement("a"); link.href = url; link.download = "dios-profile-draft.json";
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function suggest() {
    setBusy(true); setError(""); setProposal(null);
    const input = JSON.stringify([assistant, draft, instruction]);
    if (input !== request.current.input) request.current = {input, key: crypto.randomUUID()};
    try {const value = await studioApi.suggest(assistant, draft, instruction, request.current.key); setRun(value); setObserved(true);}
    catch (err) {failure(err);} finally {setBusy(false);}
  }
  async function inspectProposal() {
    if (!run) return;
    setBusy(true); setError(""); const seq = ++generation.current;
    try {
      const value = await studioApi.proposal(run.id, draft);
      if (seq !== generation.current) return;
      if (value.text && value.review.valid) setProposal({text: value.text, base: draft, review: value.review});
      else {setReview(value.review); setError("STUDIO_PROPOSAL_INVALID");}
    } catch (err) {failure(err);} finally {setBusy(false);}
  }
  const diff = proposal?.review || review;
  return <section className="config-studio">
    <header><div><h2>{l("配置工作台", "Configuration Studio")}</h2><p>{l("编辑运行配置，校验并审阅改动，导出 JSON 草案。", "Edit runtime configuration, validate changes and export a JSON draft.")}</p></div><span className="studio-badge">{l("草案", "Draft")}</span></header>
    <div className="studio-toolbar">
      <label>{l("现有配置", "Existing profile")}<select value={profile} onChange={e => setProfile(e.target.value)} disabled={busy}><option value="">{l("选择配置", "Select profile")}</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <button disabled={busy || !profile} onClick={() => void load()}>{l("载入副本", "Load copy")}</button>
      <label className="studio-file">{l("导入草案", "Import draft")}<input type="file" accept=".json,application/json" disabled={busy} onChange={e => {void importFile(e.target.files?.[0]); e.target.value = "";}} /></label>
      <button disabled={busy || !review?.valid || !!proposal} onClick={download}>{l("导出草案", "Export draft")}</button>
    </div>
    {error && <p role="alert" className="studio-error">{l("操作未完成", "Action incomplete")}: <code>{error}</code>{error === "STUDIO_PROPOSAL_STALE" && l("。草案已改变，请针对当前内容重新生成建议。", ". The draft changed. Generate a proposal for the current content.")}</p>}
    <div className="studio-editors">
      <label>{l("基准配置", "Baseline")}<textarea aria-label={l("基准配置", "Baseline")} value={base} readOnly spellCheck={false} /></label>
      <label>{l("当前草案", "Current draft")}<textarea ref={editor} aria-label={l("当前草案", "Current draft")} value={draft} onChange={e => edit(e.target.value)} disabled={busy} spellCheck={false} /></label>
    </div>
    <div className="studio-toolbar"><button className="btn-primary" disabled={busy} onClick={() => void compare()}>{l("校验并比较", "Validate and compare")}</button><small>{l("草案保存在当前页面；导出后可重新导入。运行配置通过正式发布流程生效。", "The draft stays on this page and can be exported and reimported. Runtime changes take effect through the release workflow.")}</small></div>
    {review && <div aria-live="polite"><strong>{review.valid ? l("校验通过", "Validation passed") : l("请修正配置", "Correct the configuration")}</strong>{[...review.base_errors.map(e => ({...e, baseline: true})), ...review.errors.map(e => ({...e, baseline: false}))].map((e, index) => <p className="studio-error" key={index}><code>{e.baseline ? "baseline" : "draft"}{e.path || "/"}</code> · {e.code}{e.keyword && ` · ${e.keyword}: ${JSON.stringify(e.expected)}`}{e.line && ` · ${l("行", "line")} ${e.line}:${e.column}`}</p>)}</div>}
    <section className="studio-assistant"><h3>{l("自然语言建议", "Natural language proposal")}</h3><p>{l("使用已接入的智能体生成配置建议，经 E2AG 授权执行。建议先进入差异审阅。", "Use an admitted agent with E2AG authorization to generate a configuration proposal for review.")}</p>
      <label>{l("建议生成器", "Proposal agent")}<select aria-label={l("建议生成器", "Proposal agent")} value={assistant} onChange={e => setAssistant(e.target.value)} disabled={busy}><option value="">{l("选择智能体", "Select agent")}</option>{candidates.map(p => <option key={p.id} value={p.id}>{p.name}{p.synthetic ? l("（合成测试）", " (synthetic)") : ""}</option>)}</select></label>
      {!candidates.length && <p>{l("尚未接入配置建议智能体。手工编辑与校验可用。", "No configuration proposal agent is registered. Manual editing and validation are available.")}</p>}
      <label>{l("希望如何调整", "Requested change")}<textarea value={instruction} onChange={e => setInstruction(e.target.value)} maxLength={4000} disabled={busy} placeholder={l("例如：将内存上限改为 512 MiB，保留其他设置。", "For example: set the memory limit to 512 MiB, keeping other settings.")} /></label>
      <button disabled={busy || !assistant || !instruction.trim() || (!!run && !terminal.has(run.state))} onClick={() => void suggest()}>{l("生成建议", "Generate proposal")}</button>
      {run && <p role="status"><a href={`#console/tasks/${run.id}`} target="_blank" rel="noreferrer">{l("查看任务与授权证据", "View task and authorization")}</a> · {run.state}{!observed && ` · ${l("观测过期", "Observation stale")}`} <button disabled={busy || run.state !== "succeeded"} onClick={() => void inspectProposal()}>{l("审阅建议", "Review proposal")}</button></p>}
    </section>
    {diff?.valid && <section className="studio-diff"><h3>{proposal ? l("建议差异", "Proposed changes") : l("草案差异", "Draft changes")} · {diff.changes.length}</h3>
      {!diff.changes.length && <p>{l("内容一致。", "No changes.")}</p>}
      {diff.changes.map((change, i) => <article key={i}><strong><code>{change.op} {change.path || "/"}</code></strong>{change.from && <p>← <code>{change.from}</code></p>}<div className="studio-values"><div><small>{l("修改前", "Before")}</small><pre>{JSON.stringify(change.before, null, 2) ?? "—"}</pre></div><div><small>{l("修改后", "After")}</small><pre>{JSON.stringify(change.after, null, 2) ?? "—"}</pre></div></div></article>)}
      {proposal && <div className="studio-toolbar"><button disabled={busy || proposal.base !== draft} onClick={() => {edit(proposal.text);}}>{l("应用到草案", "Apply to draft")}</button><button onClick={() => setProposal(null)}>{l("放弃建议", "Discard proposal")}</button></div>}
      <details><summary>JSON Patch (RFC 6902)</summary><pre>{JSON.stringify(diff.patch, null, 2)}</pre></details>
    </section>}
  </section>;
}
