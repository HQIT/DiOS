// Real browser + Docker Worker + E2AG + authenticated knowledge provider.
// Uses operator-owned demo configuration only; no customer data or model calls.
import fs from 'node:fs/promises';
const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
const root=process.argv[2];if(!root)throw new Error('Provide private demo directory');
const tokens=JSON.parse(await fs.readFile(root+'/tokens.json','utf8'));
const browser=await chromium.launch({headless:true});const evidence=[];
try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(t=>sessionStorage.setItem('dios_access_token',t),tokens.operator);
 for(const key of ['sales','construction']) {
  await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
  await page.getByLabel('执行 Agent').selectOption('evidence-'+key);
  await page.getByLabel('业务任务输入').selectOption(key+'-evidence-demo');
  if(await page.getByLabel('业务任务输入').locator('option').count()!==1)throw new Error('Cross-profile input leakage');
  await page.getByRole('button',{name:'发起资料核查',exact:true}).click();await page.waitForURL(/tasks\//);
  const runId=page.url().split('/').at(-1);
  await page.getByLabel('独立审批凭据').waitFor({timeout:60000});
  const before=await (await page.request.get('http://127.0.0.1:13081/api/os/runtime/runs/'+runId,{headers:{'X-DiOS-Access-Token':tokens.operator}})).json();
  if(before.authorization.mode!=='e2ag'||!before.attempts[0].session_id)throw new Error('Missing governed session');
  await page.getByLabel('独立审批凭据').fill(tokens.approver);
  await page.getByRole('button',{name:'批准本次调用',exact:true}).click();
  await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).waitFor({timeout:60000});
  const download=page.waitForEvent('download');await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).click();
  await (await download).saveAs(root+'/'+key+'-report.md');
  const report=await fs.readFile(root+'/'+key+'-report.md','utf8');
  if(!report.includes('待业务人员复核')||!report.includes('demo-v1')||!report.includes('工程样例')||report.includes('RESTRICTED')||report.includes('\uFFFD'))throw new Error('Report provenance invalid');
  await page.screenshot({path:root+'/'+key+'-completed.png',fullPage:true});
  await page.reload();await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).waitFor();
  evidence.push({key,runId,approval:true,report:true,historyReload:true,url:page.url()});
 }
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:root+'/business-mobile.png',fullPage:true});
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw new Error('Mobile overflow');
 await page.getByLabel('执行 Agent').selectOption('validation-review');
 await page.getByRole('button',{name:'发起评审',exact:true}).waitFor();
 if(await page.getByLabel('评审样例').locator('option').count()!==2)throw new Error('Legacy inputs changed');
 if(errors.length)throw new Error(errors.join('\n'));
 await fs.writeFile(root+'/business-browser.json',JSON.stringify({evidence,mobileOverflow:false,legacySelection:true,pageErrors:errors},null,2));
 console.log(JSON.stringify({evidence,mobileOverflow:false,legacySelection:true}));
} finally {await browser.close();}
