import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from '../node_modules/playwright/index.mjs';
const root=process.argv[2],fixture=JSON.parse(await fs.readFile(root+'/ready.json','utf8'));
const browser=await chromium.launch({headless:true});const checks=[],errors=[];
try{
 const page=await browser.newPage({locale:'zh-CN',viewport:{width:1440,height:1000},acceptDownloads:true});page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:13348/dios/#console/tasks/'+fixture.run_id);
 await page.getByPlaceholder('Access Token',{exact:true}).fill('operator-test');await page.getByRole('button',{name:'进入',exact:true}).click();
 const button=page.getByRole('button',{name:'导出脱敏诊断包',exact:true});await button.waitFor();
 const downloaded=page.waitForEvent('download');await button.click();const download=await downloaded;
 assert.equal(await download.failure(),null);await download.saveAs(root+'/browser-export.zip');checks.push('actual-zip-download');
 const failed='**/runtime/runs/*/diagnostic-export';await page.route(failed,route=>route.fulfill({status:503,contentType:'application/json',body:'{"detail":"RUNTIME_UNAVAILABLE"}'}));
 await button.click();await page.getByRole('alert').filter({hasText:'运行服务暂不可用'}).waitFor();assert.equal(await button.isEnabled(),true);checks.push('export-failure-visible-and-retryable');await page.unroute(failed);
 await page.getByLabel('语言',{exact:true}).selectOption('en');const english=page.getByRole('button',{name:'Export redacted diagnostics',exact:true});await english.waitFor();checks.push('english');
 await page.getByLabel('Theme',{exact:true}).selectOption('dark');await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await english.scrollIntoViewIfNeeded();await english.locator('..').screenshot({path:root+'/export-mobile.png'});checks.push('dark-mobile');assert.deepEqual(errors,[]);
 await fs.writeFile(root+'/browser.json',JSON.stringify({passed:true,checks,page_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks:checks.length}));
}finally{await browser.close();}