// Reads completed local qualification runs through the normal operator login.
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
  await page.goto(base+evidence.revoked.run_id);
  await page.getByPlaceholder('Access Token',{exact:true}).fill(env.DIOS_ACCESS_TOKEN);
  await page.getByRole('button',{name:'进入',exact:true}).click();
  const chain=page.getByTestId('delegation-chain');await chain.waitFor();
  await chain.getByText('授权委派链',{exact:true}).click();
  check('four-real-authority-records',await chain.locator('li').count()===4);
  await chain.getByText('准入代次 1 · 当前代次 2').waitFor();check('captured-and-current-epochs',true);
  await chain.getByText('当前委派链已失效，历史执行记录保留。',{exact:true}).waitFor();check('revocation-explained',true);
  await page.getByLabel('语言',{exact:true}).selectOption('en');
  await chain.getByText('Delegation level 3',{exact:true}).waitFor();check('english-chain',true);
  await page.setViewportSize({width:390,height:844});await chain.scrollIntoViewIfNeeded();
  check('mobile-layout',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:output+'.png',fullPage:true});
  await page.goto(base+evidence.completed.run_id);
  await page.getByRole('button',{name:'Download Markdown report',exact:true}).waitFor();check('completed-report-preserved',true);
  check('no-page-errors',errors.length===0);
  await fs.writeFile(output,JSON.stringify({checks,errors},null,2));
  await context.close();
  console.log(JSON.stringify({checks,errors}));
}finally{await browser.close();}
