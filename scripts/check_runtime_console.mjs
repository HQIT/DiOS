const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
import {fileURLToPath} from 'node:url';
import fs from 'node:fs/promises';
const root=process.argv[2] || fileURLToPath(new URL('../workspace/validation-20261113',import.meta.url));
const tokens=JSON.parse(await fs.readFile(root+'/tokens.json','utf8'));
const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 await page.addInitScript(token=>sessionStorage.setItem('dios_access_token',token),tokens.operator);
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
 await page.getByRole('button',{name:'发起评审',exact:true}).waitFor();
 await page.getByRole('button',{name:'发起评审',exact:true}).click();
 await page.waitForURL(/console\/tasks\//);
 const runId=page.url().split('/').at(-1);
 try {
   await page.getByLabel('独立审批凭据').waitFor({timeout:40000});
   await page.getByLabel('独立审批凭据').fill(tokens.approver);
   await page.getByRole('button',{name:'批准本次调用',exact:true}).click();
   await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).waitFor({timeout:45000});
   const download=page.waitForEvent('download');
   await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).click();
   await (await download).saveAs(root+'/downloaded-report.md');
   const report=await fs.readFile(root+'/downloaded-report.md','utf8');
   if(!report.includes('静态评审报告')||!report.includes('items[0]'))throw new Error('Incorrect report');
   await page.screenshot({path:root+'/console-completed.png',fullPage:true});
   await page.setViewportSize({width:390,height:844});
   await page.screenshot({path:root+'/console-mobile.png',fullPage:true});
   const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
   await fs.writeFile(root+'/browser-check.json',JSON.stringify({runId,approval:true,reportDownload:true,reportEvidence:true,mobileOverflow:overflow,url:page.url()},null,2));
   console.log(JSON.stringify({runId,completed:true,mobileOverflow:overflow}));
 } catch(error) {
   console.log((await page.locator('body').innerText()).slice(-5000));
   await page.screenshot({path:root+'/console-failure.png',fullPage:true});
   throw error;
 }
} finally {await browser.close();}
