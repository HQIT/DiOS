// Native development SSO: submit one qualification task, or inspect its history.
// The external driver controls the test approver and native worker lifecycle.
import fs from 'node:fs/promises';
import {chromium} from '../node_modules/playwright/index.mjs';
const [fixturePath,output,runId]=process.argv.slice(2);
const fixture=JSON.parse(await fs.readFile(fixturePath,'utf8'));
const base=new URL(fixture.base);
if(!['localhost','127.0.0.1'].includes(base.hostname))throw Error('Loopback fixture required');
const browser=await chromium.launch({headless:true});
const checks=[],errors=[];
function check(name,value){if(!value)throw Error(name);checks.push(name);}
try{
  const context=await browser.newContext({locale:'zh-CN',viewport:{width:1440,height:1000}});
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'dios/#console/tasks');
  await page.getByRole('link',{name:'企业登录',exact:true}).click();
  await page.locator('#username').fill(fixture.username);await page.locator('#password').fill(fixture.password);await page.locator('#kc-login').click();
  await page.getByLabel('工作目标',{exact:true}).waitFor();
  if(!runId){
    await page.getByLabel('工作目标',{exact:true}).fill('审批资格变更：旧审批失效验证');
    const accepted=page.waitForResponse(r=>r.url().endsWith('/api/os/runtime/runs')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'发起任务',exact:true}).click();
    const response=await accepted;check('native-browser-submit',response.status()===202);
    await fs.writeFile(output,JSON.stringify({run:await response.json(),checks,errors},null,2));
  }else{
    await page.goto(base+'dios/#console/tasks/'+runId);
    await page.getByText('审批已失效',{exact:true}).waitFor();check('chinese-invalidated-label',true);
    await page.getByText('审批资格已撤销，请重新申请并审批本次调用。',{exact:true}).waitFor();check('actionable-reason',true);
    check('no-dispatch-or-approval',await page.getByRole('button',{name:/^(批准本次调用|派发本次调用)$/}).count()===0);
    await page.getByLabel('语言',{exact:true}).selectOption('en');
    await page.getByText('Approval invalidated',{exact:true}).waitFor();check('english-invalidated-label',true);
    await page.setViewportSize({width:390,height:844});
    check('mobile-layout',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.getByText('Approval invalidated',{exact:true}).scrollIntoViewIfNeeded();
    await page.screenshot({path:output+'.png',fullPage:true});
    check('no-page-errors',errors.length===0);
    await fs.writeFile(output,JSON.stringify({run_id:runId,checks,errors},null,2));
    await page.getByRole('button',{name:'Sign out',exact:true}).click();
  }
  console.log(JSON.stringify({checks,errors}));
}finally{await browser.close();}
