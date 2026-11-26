import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from '../node_modules/playwright/index.mjs';
const root=process.argv[2],fixture=JSON.parse(await fs.readFile(root+'/ready.json','utf8'));
const browser=await chromium.launch({headless:true});const checks=[],errors=[];
try{
 const page=await browser.newPage({locale:'zh-CN',viewport:{width:1440,height:1000}});page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:13348/dios/#console/tasks/'+fixture.run_id);
 await page.getByPlaceholder('Access Token',{exact:true}).fill('operator-test');await page.getByRole('button',{name:'进入',exact:true}).click();
 await page.getByText('执行与来源证据',{exact:true}).click();
 const panel=page.locator('details').filter({has:page.locator('summary').getByText(/^(执行与来源证据|Execution and source evidence)$/)});
 await panel.getByText('session-one',{exact:false}).waitFor();checks.push('attempt-session');
 await panel.getByRole('button',{name:'工具与审批',exact:true}).click();
 await panel.getByText('结果未知',{exact:true}).waitFor();await panel.getByText('等待审批',{exact:true}).waitFor();checks.push('approval-unknown-separated');
 await panel.getByRole('button',{name:'知识与来源',exact:true}).click();
 await panel.getByText('人工登记',{exact:true}).waitFor();await panel.getByText('file/'+fixture.source.id+' · v1',{exact:true}).waitFor();checks.push('pinned-source-version');
 await panel.getByRole('button',{name:'继续查看来源',exact:true}).click();await panel.getByText('knowledge/manual · v1',{exact:true}).waitFor();checks.push('follow-source');
 await panel.scrollIntoViewIfNeeded();await panel.screenshot({path:root+'/evidence-zh.png'});
 await page.getByLabel('语言',{exact:true}).selectOption('en');await page.getByText('Operator declared',{exact:true}).waitFor();checks.push('english');
 await page.getByLabel('Theme',{exact:true}).selectOption('dark');await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);checks.push('dark-mobile');
 await panel.scrollIntoViewIfNeeded();await panel.screenshot({path:root+'/evidence-mobile.png'});assert.deepEqual(errors,[]);
 await fs.writeFile(root+'/browser.json',JSON.stringify({passed:true,checks,page_errors:errors},null,2)+'\n');console.log(JSON.stringify({passed:true,checks:checks.length}));
}finally{await browser.close();}
