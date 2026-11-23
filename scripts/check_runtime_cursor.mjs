const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
import {fileURLToPath} from 'node:url';
import fs from 'node:fs/promises';
const root=process.argv[2] || fileURLToPath(new URL('../workspace/validation-20261120-cursor',import.meta.url));
const tokens=JSON.parse(await fs.readFile(root+'/tokens.json','utf8'));
const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage({locale:"zh-CN",viewport:{width:1440,height:1000}});
 await page.addInitScript(token=>sessionStorage.setItem('dios_access_token',token),tokens.operator);
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
 await page.getByRole('button',{name:'发起评审',exact:true}).waitFor();
 await page.getByLabel('执行 Agent').selectOption('cursor-acp-review');
 await page.getByRole('button',{name:'发起评审',exact:true}).click();
 await page.waitForURL(/console\/tasks\//);
 const runId=page.url().split('/').at(-1);
 try {
   await page.getByLabel('独立审批凭据').waitFor({timeout:180000});
   const panel=page.getByRole('region',{name:'Worker 执行状态'});
   await panel.getByText(/实际并发 1/).waitFor({timeout:12000});
   await page.screenshot({path:root+'/cursor-worker-active.png',fullPage:true});
   const status=await (await page.request.get('http://127.0.0.1:13081/api/os/runtime/workers',
     {headers:{'X-DiOS-Access-Token':tokens.operator}})).json();
   if(status.actual_concurrency!==1 || !status.attempts.some(a=>a.run_id===runId && a.freshness==='fresh'))throw new Error('Real concurrency evidence missing');
   await fs.writeFile(root+'/cursor-worker-active.json',JSON.stringify(status,null,2));
   await page.getByLabel('独立审批凭据').fill(tokens.approver);
   await page.getByRole('button',{name:'批准本次调用',exact:true}).click();
   await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).waitFor({timeout:180000});
   const download=page.waitForEvent('download');
   await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).click();
   await (await download).saveAs(root+'/cursor-report.md');
   const report=await fs.readFile(root+'/cursor-report.md','utf8');
   if(!report.includes('静态评审报告')||!report.includes('items[0]')||report.includes('\uFFFD'))throw new Error('Incorrect report');
   await page.screenshot({path:root+'/console-completed.png',fullPage:true});
   await page.setViewportSize({width:390,height:844});
   await page.screenshot({path:root+'/console-mobile.png',fullPage:true});
   await page.getByRole('region',{name:'Worker 执行状态'}).waitFor();
   const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
   await fs.writeFile(root+'/cursor-browser-check.json',JSON.stringify({runId,approval:true,reportDownload:true,reportEvidence:true,mobileOverflow:overflow,url:page.url()},null,2));
   console.log(JSON.stringify({runId,completed:true,mobileOverflow:overflow}));
 } catch(error) {
   console.log((await page.locator('body').innerText()).slice(-5000));
   await page.screenshot({path:root+'/console-failure.png',fullPage:true});
   throw error;
 }
} finally {await browser.close();}
