import fs from 'node:fs/promises';
const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
const root=process.argv[2];if(!root)throw Error('Provide private validation directory');
const tokens=JSON.parse(await fs.readFile(root+'/tokens.json','utf8'));
const live=JSON.parse(await fs.readFile(root+'/live.json','utf8'));
const browser=await chromium.launch({headless:true});const checks=[];
try {
 const page=await browser.newPage({locale:'zh-CN',viewport:{width:1440,height:1000}});const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
 await page.getByPlaceholder('Access Token',{exact:true}).fill(tokens.a);
 await page.getByRole('button',{name:'进入',exact:true}).click();
 await page.getByLabel('执行 Agent').waitFor();
 if(await page.getByLabel('执行 Agent').locator('option').count()!==1)throw Error('Unfiltered profile catalog');
 if(await page.locator('.tr-workers').count())throw Error('Global Worker details exposed');
 checks.push('principal-login','profile-filter','global-worker-panel-hidden');
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks/'+live.run_id);
 await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).waitFor();
 const detail=await page.request.get('http://127.0.0.1:13081/api/os/runtime/runs/'+live.run_id,{headers:{Authorization:'Bearer '+tokens.a}});
 if((await detail.json()).authorization.subject!=='principal:a')throw Error('Work subject missing');
 if(await page.getByLabel('独立审批凭据').count())throw Error('Owner received approval controls');
 await page.screenshot({path:root+'/principal-task.png',fullPage:true});checks.push('owned-report','approval-controls-filtered');
 await page.setViewportSize({width:390,height:844});if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile overflow');checks.push('mobile-layout');
 const denied=await page.request.get('http://127.0.0.1:13081/api/os/runtime/runs/'+live.run_id,{headers:{Authorization:'Bearer '+tokens.b}});
 if(denied.status()!==404)throw Error('Cross-principal lookup exposed');checks.push('cross-principal-denied');
 if(errors.length)throw Error(errors.join('\n'));
 await fs.writeFile(root+'/browser.json',JSON.stringify({run_id:live.run_id,checks,pageErrors:errors},null,2));console.log(JSON.stringify({checks,pageErrors:errors}));
} finally {await browser.close();}
