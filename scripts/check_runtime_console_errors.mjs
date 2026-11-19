const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
import {fileURLToPath} from 'node:url';
import fs from 'node:fs/promises';
const root=process.argv[2] || fileURLToPath(new URL('../workspace/validation-20261113',import.meta.url));
const tokens=JSON.parse(await fs.readFile(root+'/tokens.json','utf8'));
const browser=await chromium.launch();const checks=[];
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 await page.addInitScript(token=>sessionStorage.setItem('dios_access_token',token),tokens.operator);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
 await page.getByRole('button',{name:'发起评审',exact:true}).click();
 await page.getByLabel('独立审批凭据').waitFor({timeout:30000});
 await page.getByLabel('独立审批凭据').fill('invalid-test');
 await page.getByRole('button',{name:'批准本次调用',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'审批凭据无效'}).waitFor();checks.push('invalid-approver-rejected');
 await page.getByLabel('独立审批凭据').fill(tokens.approver);
 await page.getByRole('button',{name:'拒绝',exact:true}).click();
 await page.locator('.tr-detail .tr-failed').first().waitFor({timeout:30000});checks.push('denied-tool-fails-run');
 if(await page.getByRole('button',{name:'下载 Markdown 报告',exact:true}).count())throw new Error('Old report leaked into new run');
 checks.push('previous-artifact-cleared');
 await page.getByRole('button',{name:'发起评审',exact:true}).click();
 await page.getByLabel('独立审批凭据').waitFor({timeout:30000});
 for(let retry=0;retry<3;retry++){
   const response=page.waitForResponse(r=>r.url().endsWith('/cancel')&&r.request().method()==='POST');
   await page.getByRole('button',{name:'取消任务',exact:true}).click();
   const result=await response;
   if(result.ok())break;
   if(result.status()!==409||(await result.json()).detail!=='VERSION_CONFLICT')throw new Error('Unexpected cancellation rejection');
   await page.getByRole('alert').filter({hasText:'任务状态已更新'}).waitFor();
   checks.push('stale-cancel-version-rejected');
   await page.getByRole('button',{name:'刷新',exact:true}).first().click();await page.waitForTimeout(1000);
 }
 await page.locator('.tr-detail .tr-cancelled').first().waitFor({timeout:30000});checks.push('cancel-confirmed-after-worker-exit');
 const unauthorized=await page.request.get('http://127.0.0.1:13081/api/os/runtime/runs');
 if(unauthorized.status()!==401)throw new Error('Missing operator gate');checks.push('operator-auth-required');
 await page.route('**/api/os/runtime/runs*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'RUNTIME_UNAVAILABLE'})}));
 await page.getByRole('button',{name:'刷新',exact:true}).first().click();
 await page.getByRole('alert').filter({hasText:'运行服务暂不可用'}).first().waitFor();checks.push('service-failure-visible');
 await page.unroute('**/api/os/runtime/runs*');
 for(const tab of ['Agents','Registry','任务与成果']){
   await page.getByRole('button',{name:tab,exact:true}).click();await page.waitForTimeout(300);
 }
 await page.setViewportSize({width:390,height:844});
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw new Error('Mobile overflow');
 checks.push('shared-navigation-and-mobile');
 if(errors.length)throw new Error(errors.join('\n'));
 await fs.writeFile(root+'/browser-errors.json',JSON.stringify({checks,consoleErrors:errors},null,2));
 console.log(JSON.stringify({checks,consoleErrors:errors}));
}finally{await browser.close();}
