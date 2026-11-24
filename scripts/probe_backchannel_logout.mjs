// Native development SSO qualification. The external driver verifies execution
// ownership and supplies independent approval; all credentials stay in fixture files.
import fs from 'node:fs/promises';
import {chromium} from '../node_modules/playwright/index.mjs';
const [fixturePath, output, completedEvidence] = process.argv.slice(2);
const fixture = JSON.parse(await fs.readFile(fixturePath, 'utf8'));
const base = new URL(fixture.base);
if (!['localhost', '127.0.0.1'].includes(base.hostname)) throw Error('Loopback fixture required');
const browser = await chromium.launch({headless: true});
const checks = [], errors = [];
function check(name, value) { if (!value) throw Error(name); checks.push(name); }
async function waitFile(suffix) {
  for (let i=0;i<180;i++) {
    if (await fs.stat(output+suffix).catch(()=>null)) return;
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw Error('Driver did not reach '+suffix);
}
async function login() {
  const context=await browser.newContext({locale:'zh-CN',viewport:{width:1440,height:1000}});
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'dios/#console/tasks');
  await page.getByRole('link',{name:'企业登录',exact:true}).click();
  await page.locator('#username').fill(fixture.username);await page.locator('#password').fill(fixture.password);await page.locator('#kc-login').click();
  await page.getByLabel('工作目标',{exact:true}).waitFor();
  return {context,page};
}
async function submit(name) {
  const {context,page}=await login();
  await page.getByLabel('工作目标',{exact:true}).fill(name);
  const accepted=page.waitForResponse(r=>r.url().endsWith('/api/os/runtime/runs')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'发起任务',exact:true}).click();const response=await accepted;
  check('submit-'+name,response.status()===202);
  return {context,page,run:await response.json()};
}
try {
  let a,b;
  if(completedEvidence){
    const evidence=JSON.parse(await fs.readFile(completedEvidence,'utf8'));const viewer=await login();
    a={run:{id:evidence.runs.revoked}};b={...viewer,run:{id:evidence.runs.peer}};
  }else{
    a=await submit('标准注销：运行中会话 A');await fs.writeFile(output+'.a.json',JSON.stringify(a.run));
    b=await submit('标准注销：独立会话 B');
    await fs.writeFile(output+'.pending.json',JSON.stringify({a:a.run,b:b.run}));
    await waitFile('.logout');
    await a.page.getByRole('button',{name:'退出登录',exact:true}).click();await a.page.getByRole('link',{name:'企业登录',exact:true}).waitFor();
    await fs.writeFile(output+'.logged-out','true');
    await waitFile('.event');
  }
  // Both sessions belong to the same principal. B can inspect A's preserved history.
  await b.page.goto(base+'dios/#console/tasks/'+a.run.id);await b.page.getByTestId('source-logout').waitFor();
  check('real-logout-notice', (await b.page.getByTestId('source-logout').innerText()).includes('身份来源已注销'));
  check('old-source-observation-hidden',await b.page.getByTestId('source-check').count()===0);
  await b.page.getByLabel('语言',{exact:true}).selectOption('en');
  check('english-logout-notice',(await b.page.getByTestId('source-logout').innerText()).includes('identity session has logged out'));
  await b.page.getByText('Unfinished approval (history)',{exact:true}).waitFor();
  check('revoked-approval-is-history',await b.page.getByRole('button',{name:'Approve this call',exact:true}).count()===0);
  await b.page.setViewportSize({width:390,height:844});await b.page.getByTestId('source-logout').scrollIntoViewIfNeeded();
  check('mobile-layout',await b.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await b.page.screenshot({path:output+'.mobile.png',fullPage:true});
  await b.page.goto(base+'dios/#console/tasks/'+b.run.id);
  await b.page.getByRole('button',{name:'Download Markdown report',exact:true}).waitFor({timeout:90000});
  check('peer-report-visible',true);check('no-page-errors',errors.length===0);
  await fs.writeFile(output,JSON.stringify({mode:completedEvidence?'native-history-observation':'live-submission-and-logout',checks,errors,runs:{a:a.run.id,b:b.run.id}},null,2));
  await b.page.getByRole('button',{name:'Sign out',exact:true}).click();
  console.log(JSON.stringify({checks,errors}));
} finally {await browser.close();}
