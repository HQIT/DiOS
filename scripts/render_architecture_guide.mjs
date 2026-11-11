import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';

const args = process.argv.slice(2);
const option = key => args[args.indexOf(key)+1];
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const depRoot = args.includes('--modules-dir') ? option('--modules-dir') : path.join(root,'node_modules');
const require = createRequire(path.join(depRoot,'__loader.cjs'));
const {marked} = await import(pathToFileURL(require.resolve('marked')).href);
const output = args.includes('--output') ? option('--output') : path.join(root,'docs/architecture/guide-v0.3.html');
const allDocs = [
 ['overview','阅读导航','docs/architecture/README.md'],
 ['design','架构逻辑与图解','docs/architecture/system-design.md'],
 ['agents','开放智能体接入','docs/architecture/agent-interoperability.md'],
 ['capability','智能体能力矩阵','docs/design/agent-integration-capability-matrix.md'],
 ['a2a-survey','A2A 兼容勘察','docs/design/a2a-contract-survey.md'],
 ['e2ag-survey','E2AG 接入与扩展','docs/design/e2ag-agent-integration.md'],
 ['code-review','持续评审接入设计','docs/design/continuous-code-review-assessment.md'],
 ['principles','架构原则','docs/architecture/principles.md'],
 ['engineering','工程规范','docs/engineering/handbook.md'],
 ['trace','需求与验收','docs/architecture/traceability.md'],
 ['plan','计划与下一步','docs/planning/open-agent-plan.md'],
 ['adr-interop','开放接入决议','docs/adr/0004-open-agent-harness-interoperability.md'],
 ['adr-e2ag','E2AG 决议','docs/adr/0005-e2ag-integration-evolution.md'],
 ['review','基线评审','docs/architecture/review-v0.3.md'],
 ['domain-contract','领域与 API 契约','docs/design/domain-api-contract-v1.md'],
 ['capacity-design','容量与 SLO','docs/design/capacity-slo-v1.md'],
 ['durable-adr','持久契约与存储决议','docs/adr/0006-durable-contract-and-storage.md'],
 ['contract-review','详细设计验收','docs/design/review-f25-f26.md'],
 ['auth-design','授权与 E2AG 契约','docs/design/authorization-contract-v1.md'],
 ['auth-adr','授权生命周期决议','docs/adr/0007-workload-authorization-and-e2ag-enforcement.md'],
 ['auth-review','授权设计交付','docs/design/review-f27.md'],
 ['design-template','设计模板','docs/templates/design-spec.md'],
 ['adr-template','ADR 模板','docs/templates/adr.md'],
 ['review-template','评审模板','docs/templates/review.md']
];
const survey = args.includes('--survey');
const contracts = args.includes('--contracts');
const authorization = args.includes('--authorization');
const docs = authorization ? ['auth-design','auth-adr','auth-review'].map(id=>allDocs.find(x=>x[0]===id)) : contracts ? ['domain-contract','capacity-design','durable-adr','contract-review'].map(id=>allDocs.find(x=>x[0]===id)) : survey ? ['code-review','capability','a2a-survey','e2ag-survey'].map(id=>allDocs.find(x=>x[0]===id)) : allDocs;
const ids = new Map(docs.map(([id,,file])=>[path.resolve(root,file),id]));
let sections='';
for (const [id,,file] of docs) {
 const full=path.resolve(root,file);
 const content=fs.readFileSync(full,'utf8');
 const lexer=marked.lexer(content);
 marked.walkTokens(lexer, token=>{
  if(token.type==='link' && !/^(https?:|#|mailto:)/.test(token.href)) {
   const target=path.resolve(path.dirname(full),token.href.split('#')[0]);
   token.href=ids.has(target)?'#'+ids.get(target):pathToFileURL(target).href;
  }
  if(token.type==='image'){
   const imageFile=path.resolve(path.dirname(full),token.href);
   if(!imageFile.endsWith('.svg'))throw new Error('Expected SVG figure');
   let svg=fs.readFileSync(imageFile,'utf8');
   // Unique accessible identifiers when multiple SVGs share a document.
   const prefix=path.basename(imageFile,'.svg');
   svg=svg.replace(/id="(title|desc|arrow)"/g,(_,key)=>'id="'+prefix+'-'+key+'"')
      .replace('aria-labelledby="title desc"','aria-labelledby="'+prefix+'-title '+prefix+'-desc"')
      .replaceAll('url(#arrow)','url(#'+prefix+'-arrow)');
   token.type='html';token.text='<figure>'+svg+'</figure>';
  }
 });
 sections+='<section id="'+id+'">'+marked.parser(lexer)+'</section>';
}
const html=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DiOS 架构与工程基线 v0.3</title>
<style>
section{overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%}
:root{--ink:#172d43;--muted:#52687a;--line:#d8e2ea;--accent:#126d70}
#principles th:nth-child(1){width:72px}#principles td:first-child{white-space:nowrap}#principles th:nth-child(2){width:30%}#principles th:nth-child(3){width:35%}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#edf2f6;color:var(--ink);font:16px/1.85 "Microsoft YaHei","Noto Sans CJK SC",sans-serif}header{background:#172d43;color:white;padding:48px max(32px,calc((100vw - 1120px)/2)) 36px}header p{color:#d6e6f0;margin:8px 0}header h1{font-size:34px;margin:8px 0 14px;line-height:1.4}.eyebrow{letter-spacing:.12em;font-size:14px}nav{background:white;border-bottom:1px solid var(--line);padding:14px 30px;display:flex;gap:12px 24px;flex-wrap:wrap;justify-content:center}nav a{font-size:14px}main{max-width:1184px;padding:32px;margin:auto}section{background:white;padding:38px 44px;margin:0 0 28px;border:1px solid var(--line);border-radius:10px;scroll-margin-top:18px}h1{font-size:28px;line-height:1.5}h2{font-size:23px;margin:36px 0 16px;border-bottom:1px solid var(--line);padding-bottom:10px}h3{font-size:19px;margin-top:26px}p{margin:13px 0}a{color:var(--accent);text-underline-offset:3px}table{width:100%;border-collapse:collapse;font-size:14px;line-height:1.75;margin:22px 0;table-layout:fixed}th,td{padding:12px;vertical-align:top;border:1px solid var(--line);overflow-wrap:anywhere}th{background:#edf4f7;text-align:left}figure{margin:26px -18px;background:white}figure svg{display:block;width:100%;height:auto}code{background:#edf3f6;padding:2px 4px;font-size:.91em;overflow-wrap:anywhere}li{margin:8px 0}blockquote{border-left:4px solid var(--accent);padding-left:18px;margin-left:0}footer{text-align:center;padding:12px 24px 40px;color:var(--muted);font-size:13px}@media(max-width:700px){header{padding:30px 20px}header h1{font-size:28px}main{padding:12px}section{padding:24px 18px}table{font-size:12px}th,td{padding:7px}figure{margin:18px -8px}}@media print{body{background:white}header{padding:15mm}nav{display:none}main{padding:0;max-width:none}section{border:0;padding:7mm;break-before:page}figure, tr{break-inside:avoid}a{color:inherit}}
</style></head><body><header><div class="eyebrow">DiOS · AGENT OS</div><h1>企业级数字员工平台<br>架构逻辑与工程基线</h1><p>运行有边界，工作有归属，交付有依据。</p><p>v0.3 · 2026-11-11 · G0 已接受</p></header><nav>${docs.map(([id,title])=>'<a href="#'+id+'">'+title+'</a>').join('')}</nav><main>${sections}</main><footer>DiOS · 架构基线 v0.3</footer></body></html>`;
let rendered=html;
if(survey)rendered=rendered.replace('· G0 已接受</p>','· 能力评估</p>').replaceAll('DiOS 架构与工程基线 v0.3','DiOS 持续评审接入与智能体能力评估').replace('企业级数字员工平台<br>架构逻辑与工程基线','持续代码评审接入<br>与智能体能力评估').replace('运行有边界，工作有归属，交付有依据。','可靠执行、可核验覆盖、开放智能体接入。').replaceAll('v0.3 · 2026-11-11','2026-11-11').replace('DiOS · 架构基线 v0.3','DiOS · Agent OS');
if(contracts)rendered=rendered.replaceAll('DiOS 架构与工程基线 v0.3','DiOS 领域契约与容量设计 v1').replace('企业级数字员工平台<br>架构逻辑与工程基线','领域与 API 契约<br>容量、SLO 与部署设计').replace('运行有边界，工作有归属，交付有依据。','开放智能体、持久运行与 E2AG 治理的详细设计。').replace('v0.3 · 2026-11-11 · G0 已接受','v1 · 2026-11-11 · P0 设计交付').replace('DiOS · 架构基线 v0.3','DiOS · 详细设计 v1');
if(authorization)rendered=rendered.replaceAll('DiOS 架构与工程基线 v0.3','DiOS 授权与 E2AG 治理契约 v1').replace('企业级数字员工平台<br>架构逻辑与工程基线','身份委托与资源授权<br>E2AG 执行治理契约').replace('运行有边界，工作有归属，交付有依据。','权限随任务收窄，审批绑定动作，撤销约束执行。').replace('v0.3 · 2026-11-11 · G0 已接受','v1 · 2026-11-11 · P0 设计交付').replace('DiOS · 架构基线 v0.3','DiOS · 授权契约 v1');
fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,rendered);
console.log(output);
