import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
const root=process.argv[2]||fileURLToPath(new URL('../workspace/validation-20261119-faults',import.meta.url));
const token=JSON.parse(await fs.readFile(root+'/tokens.json','utf8')).operator;
const browser=await chromium.launch();const checks=[];
try {
 const page=await browser.newPage({locale:"zh-CN",viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(t=>sessionStorage.setItem('dios_access_token',t),token);
 const api='http://127.0.0.1:13081/api/os/runtime';const headers={'X-DiOS-Access-Token':token};
 const get=async path=>{const r=await page.request.get(api+path,{headers});if(!r.ok())throw Error('GET '+path+' '+r.status());return r.json();};
 const until=async(fn,timeout=90000)=>{const end=Date.now()+timeout;while(Date.now()<end){const value=await fn();if(value)return value;await page.waitForTimeout(400);}throw Error('Condition timed out');};
 for(const code of ['PROVIDER_RATE_LIMITED','PROVIDER_QUOTA_EXHAUSTED']){
  await fs.writeFile(root+'/fault-mode',code);
  await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
  await page.getByLabel('执行 Agent').selectOption('validation-recovery');
  await page.getByRole('button',{name:'发起任务',exact:true}).click();await page.waitForURL(/console\/tasks\//);
  const runId=page.url().split('/').at(-1);
  const cooling=await until(async()=>{const x=await get('/runs/'+runId);return x.state==='retry_wait'&&x;});
  await page.getByText('冷却待重试',{exact:true}).waitFor();
  await page.getByText(/冷却中，下次可探测/).waitFor();
  await page.screenshot({path:root+'/'+code+'-cooling.png',fullPage:true});
  const samples=await get('/samples');const additional=[];
  for(let n=0;n<2;n++){
   const response=await page.request.post(api+'/runs',{headers,data:{profile_id:'validation-recovery',sample_id:samples.items[0].id,
     goal:'故障恢复后检查代码边界条件',idempotency_key:crypto.randomUUID()}});
   if(!response.ok())throw Error('Submit failed');additional.push((await response.json()).id);
  }
  const before=await get('/workers');if(before.actual_concurrency!==0)throw Error('Cooldown launched work');
  await fs.writeFile(root+'/fault-mode','ok');
  const probe=await until(async()=>{const x=await get('/workers');return x.circuits.some(c=>c.state==='half_open')&&x.actual_concurrency===1&&x;});
  if(probe.attempts.length!==1)throw Error('More than one probe');
  await page.getByText(/单路探测中，确认恢复后放开并发/).waitFor();
  await page.screenshot({path:root+'/'+code+'-probe.png',fullPage:true});
  const concurrent=await until(async()=>{const x=await get('/workers');return x.actual_concurrency===2&&x.circuits.every(c=>c.state==='closed')&&x;});
  if(new Set(concurrent.attempts.map(a=>a.worker_id)).size!==2)throw Error('Independent Workers missing');
  await page.getByText(/实际并发 2/).waitFor();
  const completed=await until(async()=>{const values=await Promise.all([runId,...additional].map(id=>get('/runs/'+id)));return values.every(x=>x.state==='succeeded')&&values;});
  if(completed[0].attempts.length!==2||completed.slice(1).some(x=>x.attempts.length!==1))throw Error('Unexpected duplicate attempt');
  await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).waitFor();
  const artifact=completed[0].artifacts.find(a=>a.kind==='result');
  const download=await page.request.get(api+'/runs/'+runId+'/artifacts/'+artifact.id+'/download?format=markdown',{headers});
  if(!download.ok()||!(await download.text()).includes('静态评审报告'))throw Error('Recovered report download failed');
  checks.push({code,runId,additional,cooling,probe,concurrent,completed});
  console.log(JSON.stringify({code,sameRunRecovered:true,probeConcurrency:probe.actual_concurrency,restoredConcurrency:concurrent.actual_concurrency}));
 }
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:root+'/fault-mobile.png',fullPage:true});
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile overflow');
 if(errors.length)throw Error(errors.join('\n'));
 await fs.writeFile(root+'/fault-browser.json',JSON.stringify({checks,consoleErrors:errors,mobileOverflow:false},null,2));
}finally{await fs.writeFile(root+'/fault-mode','ok');await browser.close();}
