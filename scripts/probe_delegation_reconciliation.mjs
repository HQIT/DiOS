// Read actual reconciled history using the normal development operator login.
import fs from 'node:fs/promises';
import {chromium} from '../node_modules/playwright/index.mjs';
const [environmentPath,evidencePath,output]=process.argv.slice(2);
const env=JSON.parse(await fs.readFile(environmentPath,'utf8'));
const evidence=JSON.parse(await fs.readFile(evidencePath,'utf8'));
const browser=await chromium.launch({headless:true});const checks=[],errors=[];
function check(name,value){if(!value)throw Error(name);checks.push(name);}
try{
  const context=await browser.newContext({locale:'zh-CN',viewport:{width:1440,height:1000}});
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  const base='http://127.0.0.1:13082/dios/#console/tasks/';
  await page.goto(base+evidence.revoked);
  await page.getByPlaceholder('Access Token',{exact:true}).fill(env.DIOS_ACCESS_TOKEN);
  await page.getByRole('button',{name:'进入',exact:true}).click();
  const chain=page.getByTestId('delegation-chain');await chain.waitFor();await chain.getByText('授权委派链',{exact:true}).click();
  const source=page.getByTestId('delegation-source-check');await source.waitFor();
  check('real-revoked-issuer-status',(await source.innerText()).includes('来源已失效'));
  check('last-observed-time',(await source.innerText()).includes('最近检查：'));
  check('revoked-source-does-not-show-pending-retry',!(await source.innerText()).includes('下次检查'));
  await page.getByLabel('语言',{exact:true}).selectOption('en');
  check('english-issuer-status',(await source.innerText()).includes('Issuer identity check: Source invalidated'));
  await page.setViewportSize({width:390,height:844});await source.scrollIntoViewIfNeeded();
  check('mobile-layout',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:output+'.png',fullPage:true});
  await page.goto(base+evidence.completed);await page.getByRole('button',{name:'Download Markdown report',exact:true}).waitFor();
  check('completed-report-preserved',true);check('no-page-errors',errors.length===0);
  await fs.writeFile(output,JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({checks,errors}));
}finally{await browser.close();}
